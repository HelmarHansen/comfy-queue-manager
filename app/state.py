"""Zentraler Zustand: ComfyUI-Clients, Polling und Priority-Enforcement.

Ein Hintergrund-Task pollt alle konfigurierten Hosts im eingestellten
Intervall, hält den letzten bekannten Zustand im Speicher (fürs Dashboard)
und setzt dabei die Priority-IP-Regeln durch. Pro Host gibt es ein
asyncio.Lock, damit sich Poller und manuelle Aktionen (Drag-and-Drop-Reorder,
Löschen) nicht gegenseitig in die Queue funken.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .comfy import ComfyClient, ComfyError
from .config import AppConfig
from .queue_logic import apply_order, desired_priority_order, item_view, parse_queue

log = logging.getLogger("cqm.state")


class StateStore:
    def __init__(self, get_config: Callable[[], AppConfig]):
        self._get_config = get_config
        self.clients: dict[str, ComfyClient] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.states: dict[str, dict] = {}
        self._poll_task: asyncio.Task | None = None

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.sync_clients()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for client in self.clients.values():
            await client.close()

    def sync_clients(self) -> None:
        """Clients an die aktuelle Host-Liste der Config angleichen."""
        cfg = self._get_config()
        wanted = {h.name: h.url for h in cfg.hosts}
        for name in list(self.clients):
            if name not in wanted or self.clients[name].base_url != wanted[name]:
                client = self.clients.pop(name)
                asyncio.get_event_loop().create_task(client.close())
                self.states.pop(name, None)
        for name, url in wanted.items():
            if name not in self.clients:
                self.clients[name] = ComfyClient(name, url)
                self.locks.setdefault(name, asyncio.Lock())

    # -- Zugriff ------------------------------------------------------------

    def client(self, name: str) -> ComfyClient | None:
        return self.clients.get(name)

    def lock(self, name: str) -> asyncio.Lock:
        return self.locks.setdefault(name, asyncio.Lock())

    def snapshot(self) -> dict:
        cfg = self._get_config()
        return {
            "hosts": [self.states.get(h.name, _offline_state(h.name, h.url, "noch nicht abgefragt"))
                      for h in cfg.hosts],
            "priority_ips": [{"ip": r.ip, "mode": r.mode} for r in cfg.priority_ips],
            "poll_interval_seconds": cfg.poll_interval_seconds,
        }

    # -- Polling & Enforcement ----------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            cfg = self._get_config()
            await asyncio.gather(
                *(self.refresh_host(h.name) for h in cfg.hosts),
                return_exceptions=True,
            )
            await asyncio.sleep(max(0.5, cfg.poll_interval_seconds))

    async def refresh_host(self, name: str) -> dict:
        """Queue eines Hosts abfragen, Regeln durchsetzen, Zustand cachen."""
        cfg = self._get_config()
        client = self.client(name)
        host_cfg = cfg.host_by_name(name)
        if client is None or host_cfg is None:
            raise KeyError(name)

        async with self.lock(name):
            try:
                raw = await client.get_queue()
                running, pending = parse_queue(raw)

                # Priority-IPs durchsetzen: Nur umsortieren, wenn die aktuelle
                # Reihenfolge von der Zielreihenfolge abweicht (stabile
                # Sortierung -> konvergiert, keine Dauerschleife).
                rules = cfg.rules_by_ip()
                if cfg.enforce_priority_rules and rules and len(pending) > 1:
                    desired = desired_priority_order(pending, rules)
                    if desired != [it.prompt_id for it in pending]:
                        log.info("%s: Priority-Regeln greifen, sortiere um", name)
                        await apply_order(client, desired)
                        running, pending = parse_queue(await client.get_queue())

                state = {
                    "name": name,
                    "url": host_cfg.url,
                    "online": True,
                    "error": None,
                    "running": [item_view(it, i, rules) for i, it in enumerate(running)],
                    "pending": [item_view(it, i + 1, rules) for i, it in enumerate(pending)],
                    "updated_at": time.time(),
                }
            except ComfyError as e:
                # Host nicht erreichbar: klarer Status statt Absturz. Nur beim
                # Wechsel online -> offline warnen, nicht bei jedem Poll.
                if self.states.get(name, {}).get("online") is not False:
                    log.warning("%s nicht erreichbar: %s", name, e)
                state = _offline_state(name, host_cfg.url, str(e))

        if state["online"] and self.states.get(name, {}).get("online") is False:
            log.info("%s ist wieder erreichbar", name)
        self.states[name] = state
        return state


def _offline_state(name: str, url: str, error: str) -> dict:
    return {
        "name": name,
        "url": url,
        "online": False,
        "error": error,
        "running": [],
        "pending": [],
        "updated_at": time.time(),
    }
