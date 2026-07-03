"""Dünner asynchroner HTTP-Client für die ComfyUI-API eines einzelnen Hosts.

Verwendete Endpunkte (verifiziert gegen den ComfyUI-Quellcode, server.py):

  GET  /queue                     -> {"queue_running": [...], "queue_pending": [...]}
                                     Einträge sind Tupel (number, prompt_id, prompt,
                                     extra_data, outputs_to_execute); die Pending-Queue
                                     ist ein Heap, sortiert nach `number`.
  POST /prompt                    -> Einreihen. Akzeptiert neben "prompt" auch
                                     "extra_data", "client_id", "number" (explizite
                                     Priorität), "front" und eine client-seitige
                                     "prompt_id" (UUID). Letzteres erlaubt uns,
                                     beim Umsortieren die IDs zu erhalten.
  POST /queue {"delete": [ids]}   -> Entfernt NUR pending Einträge (laufende Jobs
                                     bleiben unberührt). {"clear": true} leert alles.
  POST /interrupt {"prompt_id"}   -> Unterbricht gezielt (oder global ohne ID).
  GET  /history                   -> Abgeschlossene Jobs inkl. extra_data.
"""
from __future__ import annotations

from typing import Any

import httpx


class ComfyError(Exception):
    """Fehler bei der Kommunikation mit einer ComfyUI-Instanz."""


class ComfyClient:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        # Kurzer Connect-Timeout, damit ein offliner Host das Polling nicht blockiert.
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=4.0, read=30.0, write=30.0, pool=30.0),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ComfyError(f"{self.name}: {e.__class__.__name__}: {e}") from e
        if resp.status_code >= 400:
            raise ComfyError(f"{self.name}: HTTP {resp.status_code} bei {path}: {resp.text[:500]}")
        return resp

    async def get_queue(self) -> dict:
        return (await self._request("GET", "/queue")).json()

    async def get_history(self, max_items: int = 64) -> dict:
        return (await self._request("GET", "/history", params={"max_items": max_items})).json()

    async def post_prompt(self, payload: dict) -> dict:
        """Reiht einen Prompt ein; payload wird 1:1 an ComfyUI durchgereicht."""
        return (await self._request("POST", "/prompt", json=payload)).json()

    async def delete_pending(self, prompt_ids: list[str]) -> None:
        if prompt_ids:
            await self._request("POST", "/queue", json={"delete": prompt_ids})

    async def clear_pending(self) -> None:
        await self._request("POST", "/queue", json={"clear": True})

    async def interrupt(self, prompt_id: str | None = None) -> None:
        body = {"prompt_id": prompt_id} if prompt_id else {}
        await self._request("POST", "/interrupt", json=body)
