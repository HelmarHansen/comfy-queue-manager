"""Reverse-Proxy vor ComfyUI mit IP-Tracking.

ComfyUI speichert die Client-IP eines Requests nirgendwo persistent. Deshalb
schicken die Clients ihre Requests nicht direkt an ComfyUI, sondern an
diesen Proxy (http://<manager>:8189/proxy/<hostname>/...). Der Proxy

  * reicht alle Requests unverändert an die jeweilige ComfyUI-Instanz durch
    (die normale ComfyUI-Weboberfläche funktioniert also durch ihn hindurch),
  * fängt POST /prompt ab und schreibt die Client-IP plus Zeitstempel in
    extra_data["cqm"] — diese Metadaten wandern mit dem Job durch Queue und
    History und sind damit auch nach einem Backend-Neustart noch da,
  * leitet den WebSocket /ws durch, damit Live-Updates der ComfyUI-Oberfläche
    (Fortschritt, Vorschau) weiter ankommen.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .comfy import ComfyClient, ComfyError
from .queue_logic import META_KEY, build_prompt_meta

log = logging.getLogger("cqm.proxy")

# Header, die ein Proxy nicht weiterreichen darf (bzw. die httpx selbst setzt).
_SKIP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "te", "trailers",
    "transfer-encoding", "upgrade", "proxy-authenticate", "proxy-authorization",
}
_SKIP_RESPONSE_HEADERS = _SKIP_REQUEST_HEADERS | {"content-encoding"}


def client_ip(request: Request) -> str:
    """Client-IP ermitteln; X-Forwarded-For gewinnt (falls ein weiterer
    Proxy/Load-Balancer davor hängt), sonst die Socket-Adresse."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unbekannt"


def inject_meta(body: bytes, ip: str) -> bytes:
    """IP + Zeitstempel in extra_data des /prompt-Bodys einbetten."""
    data = json.loads(body)
    extra = data.setdefault("extra_data", {})
    extra[META_KEY] = build_prompt_meta(ip)
    return json.dumps(data).encode("utf-8")


async def forward_http(client: ComfyClient, request: Request, path: str) -> Response:
    """Beliebigen HTTP-Request an die ComfyUI-Instanz durchreichen."""
    body = await request.body()

    # /prompt abfangen: hier entsteht der Queue-Eintrag, hier muss die IP rein.
    if request.method == "POST" and path.strip("/") == "prompt" and body:
        try:
            body = inject_meta(body, client_ip(request))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("POST /prompt mit nicht-JSON-Body, reiche unverändert weiter")

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQUEST_HEADERS
    }

    try:
        resp = await client.http.request(
            request.method,
            "/" + path.lstrip("/"),
            params=request.query_params,
            content=body,
            headers=headers,
        )
    except ComfyError:
        raise
    except Exception as e:  # httpx-Transportfehler -> sauberer 502
        return Response(
            content=json.dumps({"error": f"Host {client.name} nicht erreichbar: {e}"}),
            status_code=502,
            media_type="application/json",
        )

    resp_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)


async def bridge_websocket(ws: WebSocket, upstream_base_url: str) -> None:
    """WebSocket-Verbindung zwischen Client und ComfyUI in beide Richtungen pumpen."""
    scheme = "wss" if upstream_base_url.startswith("https") else "ws"
    upstream_url = scheme + upstream_base_url[upstream_base_url.index("://"):] + "/ws"
    if ws.url.query:
        upstream_url += "?" + ws.url.query  # clientId etc. weiterreichen

    await ws.accept()
    try:
        async with websockets.connect(upstream_url, max_size=None) as upstream:

            async def client_to_upstream() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client() -> None:
                async for data in upstream:
                    if isinstance(data, str):
                        await ws.send_text(data)
                    else:
                        await ws.send_bytes(data)

            # Sobald eine Seite endet, die andere abbrechen.
            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, websockets.ConnectionClosed)):
                    raise exc
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        log.warning("WebSocket-Bridge zu %s beendet: %s", upstream_url, e)
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass  # bereits geschlossen
