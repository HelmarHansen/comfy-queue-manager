"""FastAPI-App: Dashboard-API, Priority-IP-Verwaltung und ComfyUI-Proxy.

Routen-Überblick:
  GET  /                                   Dashboard (statisches Frontend)
  GET  /api/state                          Zustand aller Hosts (aus dem Poller-Cache)
  POST /api/refresh                        alle Hosts sofort neu abfragen
  POST /api/hosts/{name}/reorder           Pending-Queue umsortieren {"order": [ids]}
  POST /api/hosts/{name}/delete            Pending-Einträge löschen {"ids": [ids]}
  POST /api/hosts/{name}/interrupt         laufenden Job unterbrechen {"prompt_id"?}
  POST /api/hosts/{name}/clear             Pending-Queue leeren
  GET  /api/priority-ips                   Priority-IP-Regeln lesen
  PUT  /api/priority-ips                   Regeln komplett ersetzen (persistiert)
  ALLE /proxy/{name}/{pfad}                Reverse-Proxy zu ComfyUI (IP-Tracking)
  WS   /proxy/{name}/ws                    WebSocket-Durchleitung
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import proxy
from .comfy import ComfyClient, ComfyError
from .config import AppConfig, PriorityRule, load_config, save_config
from .queue_logic import apply_order
from .state import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx loggt sonst jeden einzelnen Poll-Request als INFO-Zeile.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Config einmal beim Start laden; Änderungen (Priority-IPs) laufen über
# save_config und ersetzen dieses Objekt.
_config: AppConfig = load_config()


def get_config() -> AppConfig:
    return _config


store = StateStore(get_config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()
    yield
    await store.stop()


app = FastAPI(title="ComfyUI Queue Manager", lifespan=lifespan)


def _client_or_404(name: str) -> ComfyClient:
    client = store.client(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"Unbekannter Host: {name}")
    return client


# ---------------------------------------------------------------------------
# Dashboard-API
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def api_state() -> dict:
    return store.snapshot()


@app.post("/api/refresh")
async def api_refresh() -> dict:
    cfg = get_config()
    await asyncio.gather(
        *(store.refresh_host(h.name) for h in cfg.hosts), return_exceptions=True
    )
    return store.snapshot()


class ReorderBody(BaseModel):
    order: list[str] = Field(description="prompt_ids der Pending-Queue in Zielreihenfolge")


@app.post("/api/hosts/{name}/reorder")
async def api_reorder(name: str, body: ReorderBody) -> dict:
    client = _client_or_404(name)
    try:
        async with store.lock(name):
            report = await apply_order(client, body.order)
    except ComfyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    state = await store.refresh_host(name)
    return {"report": report, "state": state}


class DeleteBody(BaseModel):
    ids: list[str]


@app.post("/api/hosts/{name}/delete")
async def api_delete(name: str, body: DeleteBody) -> dict:
    client = _client_or_404(name)
    try:
        async with store.lock(name):
            await client.delete_pending(body.ids)
    except ComfyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"state": await store.refresh_host(name)}


class InterruptBody(BaseModel):
    prompt_id: str | None = None


@app.post("/api/hosts/{name}/interrupt")
async def api_interrupt(name: str, body: InterruptBody) -> dict:
    client = _client_or_404(name)
    try:
        async with store.lock(name):
            await client.interrupt(body.prompt_id)
    except ComfyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"state": await store.refresh_host(name)}


@app.post("/api/hosts/{name}/clear")
async def api_clear(name: str) -> dict:
    client = _client_or_404(name)
    try:
        async with store.lock(name):
            await client.clear_pending()
    except ComfyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"state": await store.refresh_host(name)}


# ---------------------------------------------------------------------------
# Priority-IP-Verwaltung (persistiert in config.json)
# ---------------------------------------------------------------------------

class RuleModel(BaseModel):
    ip: str
    mode: str  # "front" | "back"


@app.get("/api/priority-ips")
async def api_get_rules() -> list[dict]:
    return [{"ip": r.ip, "mode": r.mode} for r in get_config().priority_ips]


@app.put("/api/priority-ips")
async def api_put_rules(rules: list[RuleModel]) -> list[dict]:
    global _config
    seen: dict[str, str] = {}
    for r in rules:
        ip = r.ip.strip()
        if not ip:
            raise HTTPException(status_code=400, detail="Leere IP")
        if r.mode not in ("front", "back"):
            raise HTTPException(status_code=400, detail=f"Ungültiger Modus: {r.mode}")
        seen[ip] = r.mode  # letzte Regel pro IP gewinnt

    _config.priority_ips = [PriorityRule(ip=ip, mode=mode) for ip, mode in seen.items()]
    save_config(_config)

    # Neue Regeln sofort durchsetzen, nicht erst beim nächsten Poll.
    asyncio.create_task(api_refresh())
    return [{"ip": r.ip, "mode": r.mode} for r in _config.priority_ips]


# ---------------------------------------------------------------------------
# Proxy (IP-Tracking) — Clients zeigen hierauf statt direkt auf ComfyUI
# ---------------------------------------------------------------------------

@app.websocket("/proxy/{name}/ws")
async def proxy_ws(websocket: WebSocket, name: str) -> None:
    client = store.client(name)
    if client is None:
        await websocket.close(code=4404)
        return
    await proxy.bridge_websocket(websocket, client.base_url)


@app.api_route(
    "/proxy/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_http(name: str, path: str, request: Request) -> Response:
    return await proxy.forward_http(_client_or_404(name), request, path)


# ---------------------------------------------------------------------------
# Statisches Frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
