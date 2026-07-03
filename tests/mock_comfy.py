"""Mock-ComfyUI-Server für Tests ohne echte GPU-Instanz.

Repliziert die für den Queue Manager relevante Semantik von ComfyUIs
server.py/execution.py:

  * Pending-Queue als Heap, sortiert nach `number`
  * POST /prompt mit number/front/prompt_id/extra_data (inkl. create_time)
  * POST /queue mit delete/clear (wirkt nur auf pending)
  * POST /interrupt (beendet den "laufenden" Job)
  * Ein Job "läuft" immer, sobald einer da ist (wird nie fertig — simuliert
    eine beschäftigte GPU), damit Running/Pending-Trennung testbar ist.

Start:  python tests/mock_comfy.py [port]
"""
from __future__ import annotations

import heapq
import sys
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Mock ComfyUI")

queue: list[tuple] = []      # Heap: (number, prompt_id, prompt, extra_data, outputs)
running: list[tuple] = []    # max. 1 Eintrag
counter = 0.0                # entspricht self.number in ComfyUI


def promote() -> None:
    """Wie der Worker-Thread: nächsten Job starten, wenn keiner läuft."""
    if not running and queue:
        running.append(heapq.heappop(queue))


@app.get("/queue")
async def get_queue() -> dict:
    return {"queue_running": list(running), "queue_pending": list(queue)}


@app.post("/prompt")
async def post_prompt(request: Request):
    global counter
    data = await request.json()

    if "number" in data:
        number = float(data["number"])
    else:
        number = counter
        if data.get("front"):
            number = -number
        counter += 1

    if "prompt" not in data:
        return JSONResponse({"error": {"type": "no_prompt"}, "node_errors": {}}, status_code=400)

    prompt_id = data.get("prompt_id") or str(uuid.uuid4())
    extra = data.get("extra_data", {})
    if "client_id" in data:
        extra["client_id"] = data["client_id"]
    extra["create_time"] = int(time.time() * 1000)

    heapq.heappush(queue, (number, prompt_id, data["prompt"], extra, []))
    counter = max(counter, number + 1)
    promote()
    return {"prompt_id": prompt_id, "number": number, "node_errors": {}}


@app.post("/queue")
async def post_queue(request: Request):
    global queue
    data = await request.json()
    if data.get("clear"):
        queue = []
    for pid in data.get("delete", []):
        queue = [item for item in queue if item[1] != pid]
        heapq.heapify(queue)
    return JSONResponse({}, status_code=200)


@app.post("/interrupt")
async def post_interrupt(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    pid = data.get("prompt_id")
    if not pid or (running and running[0][1] == pid):
        running.clear()
        promote()
    return JSONResponse({}, status_code=200)


@app.get("/history")
async def get_history() -> dict:
    return {}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8288
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
