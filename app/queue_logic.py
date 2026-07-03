"""Queue-Parsing, Anzeige-Aufbereitung, Umsortieren und Priority-IP-Logik.

Kernidee Reordering:
ComfyUI bietet kein natives "verschiebe Job X an Position Y". Die Pending-
Queue ist aber ein Heap, sortiert nach dem Feld `number` (Float), und
POST /prompt akzeptiert sowohl eine explizite `number` als auch eine
client-seitige `prompt_id`. Umsortieren heißt deshalb: bewegte Jobs löschen
(POST /queue {"delete": [...]}) und mit GLEICHER prompt_id und passender
`number` neu einreihen.

Damit das auch bei langen Queues und Remote-Hosts (RunPod) schnell bleibt,
wird nicht die komplette Queue neu eingereiht, sondern nur die minimal
nötige Menge Jobs: Die Jobs auf der längsten aufsteigenden Teilfolge der
aktuellen Nummern bleiben stehen, die übrigen bekommen eine Nummer ZWISCHEN
ihren neuen Nachbarn (Floats machen's möglich). Ein einzelner Drag-and-Drop
kostet so 2 HTTP-Requests statt 2·Queue-Länge. Erst wenn die Float-Lücken
nach sehr vielen Moves aufgebraucht sind, wird einmal komplett neu
durchnummeriert.

Weil nur Nummern unterhalb des Server-Zählers vergeben werden, sortieren
sich parallel neu eintreffende Jobs (die vom Server höhere Nummern bekommen)
korrekt dahinter ein. Weil die prompt_ids erhalten bleiben, funktioniert das
Job-Tracking der einreichenden Clients (WebSocket wartet auf die prompt_id)
weiter.

Der laufende Job wird nie angefasst: {"delete": ...} wirkt nur auf pending.
Gegen das Rennen "Job startet genau zwischen Abfrage und Löschen" schützt
ein Re-Check nach dem Löschen: Was noch existiert (= läuft inzwischen),
wird nicht erneut eingereiht.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import time
from dataclasses import dataclass

from .comfy import ComfyClient, ComfyError

log = logging.getLogger("cqm.queue")

# Namespace-Schlüssel für unsere Metadaten in extra_data. Wandert mit dem
# Job durch Queue und History und überlebt so auch einen Backend-Neustart.
META_KEY = "cqm"


@dataclass
class QueueItem:
    number: float
    prompt_id: str
    prompt: dict
    extra_data: dict
    status: str  # "running" | "pending"


# ---------------------------------------------------------------------------
# Parsing / Aufbereitung
# ---------------------------------------------------------------------------

def parse_queue(raw: dict) -> tuple[list[QueueItem], list[QueueItem]]:
    """Antwort von GET /queue in (running, pending) zerlegen.

    Pending wird nach `number` sortiert — das ist die tatsächliche
    Ausführungsreihenfolge (Heap-Ordnung).
    """
    running = _parse_items(raw.get("queue_running"), "running")
    pending = _parse_items(raw.get("queue_pending"), "pending")
    pending.sort(key=lambda it: (it.number, it.prompt_id))
    return running, pending


def _parse_items(entries: list | None, status: str) -> list[QueueItem]:
    out: list[QueueItem] = []
    for e in entries or []:
        # Einträge sind Tupel (number, prompt_id, prompt, extra_data, ...);
        # defensiv parsen, falls sich das Format zwischen Versionen ändert.
        if not isinstance(e, (list, tuple)) or len(e) < 3:
            continue
        extra = e[3] if len(e) > 3 and isinstance(e[3], dict) else {}
        prompt = e[2] if isinstance(e[2], dict) else {}
        out.append(QueueItem(float(e[0]), str(e[1]), prompt, extra, status))
    return out


# String-Inputs, die einen Prompt-Text enthalten können — Reihenfolge =
# Priorität. Deckt CLIPTextEncode (text), Wan (positive_prompt), Flux
# (t5xxl/clip_l), SDXL (text_g/text_l) sowie Primitive-/String-Nodes ab.
_TEXT_KEYS = ("text", "positive_prompt", "t5xxl", "clip_l", "text_g", "text_l",
              "prompt", "string", "value")

# Link-Inputs, die bevorzugt Richtung Text-Encoder führen (Conditioning-Kette).
_FOLLOW_FIRST = ("conditioning", "positive", "text", "clip")


def _is_link(v: object) -> bool:
    """Node-Verknüpfung im API-Format: [node_id, output_index]."""
    return isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], (str, int))


def _resolve_text(prompt: dict, node_id: object, visited: set[str], depth: int = 0) -> str | None:
    """Von einem Node aus rückwärts den ersten nichtleeren Prompt-Text finden.

    Läuft Conditioning-Ketten hoch (FluxGuidance, ControlNetApply,
    LTXVConditioning, ...) und folgt auch verlinkten Text-Inputs zu
    Primitive-/String-Nodes.
    """
    node_id = str(node_id)
    if depth > 12 or node_id in visited:
        return None
    visited.add(node_id)
    node = prompt.get(node_id)
    if not isinstance(node, dict):
        return None
    inputs = node.get("inputs") or {}

    for key in _TEXT_KEYS:
        v = inputs.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    link_keys = sorted(
        (k for k, v in inputs.items() if _is_link(v)),
        key=lambda k: k not in _FOLLOW_FIRST,
    )
    for k in link_keys:
        found = _resolve_text(prompt, inputs[k][0], visited, depth + 1)
        if found:
            return found
    return None


def _upstream_ids(prompt: dict, node_id: object, out: set[str], depth: int = 0) -> None:
    """Alle Node-IDs einsammeln, die (transitiv) in node_id münden."""
    node_id = str(node_id)
    if depth > 12 or node_id in out:
        return
    out.add(node_id)
    node = prompt.get(node_id)
    if not isinstance(node, dict):
        return
    for v in (node.get("inputs") or {}).values():
        if _is_link(v):
            _upstream_ids(prompt, v[0], out, depth + 1)


def extract_display_name(prompt: dict) -> str:
    """Menschlich lesbaren Namen aus dem Workflow ziehen.

    1. Graph-basiert: den "positive"-Eingang eines Samplers/Conditioning-Nodes
       (KSampler, CFGGuider, LTXVConditioning, ...) rückwärts zum Text-Encoder
       verfolgen — das ist der tatsächliche Positiv-Prompt.
    2. Fallback ohne "positive"-Eingang (z. B. Flux/BasicGuider): alle
       Text-Nodes einsammeln, dabei alles ausschließen, was in einem
       "negative"-Eingang mündet. Sonst gewinnt regelmäßig das lange,
       statische Negativ-Boilerplate und alle Jobs zeigen denselben Text.
    3. Sonst: Modellname, zuletzt die Node-Anzahl.
    """
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        link = (node.get("inputs") or {}).get("positive")
        if _is_link(link):
            text = _resolve_text(prompt, link[0], set())
            if text:
                return text[:160]

    negative_upstream: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        link = (node.get("inputs") or {}).get("negative")
        if _is_link(link):
            _upstream_ids(prompt, link[0], negative_upstream)

    candidates: list[tuple[int, int, str]] = []
    for nid, node in prompt.items():
        if not isinstance(node, dict) or str(nid) in negative_upstream:
            continue
        inputs = node.get("inputs") or {}
        for key in _TEXT_KEYS:
            v = inputs.get(key)
            if isinstance(v, str) and v.strip():
                is_clip = 0 if "CLIPTextEncode" in str(node.get("class_type", "")) else 1
                candidates.append((is_clip, -len(v), v.strip()))
                break
    if candidates:
        candidates.sort()
        return candidates[0][2][:160]

    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        for key in ("ckpt_name", "unet_name", "model_name"):
            val = inputs.get(key)
            if isinstance(val, str) and val:
                return val
    return f"Workflow ({len(prompt)} Nodes)"


def item_view(item: QueueItem, position: int, rules: dict[str, str]) -> dict:
    """QueueItem in das JSON-Format für API/Dashboard umwandeln."""
    meta = item.extra_data.get(META_KEY) or {}
    ip = meta.get("ip")

    # Zeitstempel: bevorzugt unser eigener (übersteht Reordering), sonst
    # ComfyUIs create_time (Millisekunden, wird beim Re-Enqueue erneuert).
    submitted_at = meta.get("submitted_at")
    if submitted_at is None:
        ct = item.extra_data.get("create_time")
        if isinstance(ct, (int, float)):
            submitted_at = ct / 1000.0

    return {
        "prompt_id": item.prompt_id,
        "status": item.status,
        "position": position,
        "ip": ip,
        "submitted_at": submitted_at,
        "name": extract_display_name(item.prompt),
        "node_count": len(item.prompt),
        "priority_rule": rules.get(ip) if ip else None,
    }


# ---------------------------------------------------------------------------
# Priority-IP-Regeln
# ---------------------------------------------------------------------------

def desired_priority_order(pending: list[QueueItem], rules: dict[str, str]) -> list[str]:
    """Zielreihenfolge nach Priority-Regeln berechnen.

    Stabile Sortierung in drei Klassen: front-IPs < neutrale < back-IPs.
    Innerhalb einer Klasse bleibt die bestehende Reihenfolge (auch manuell
    per Drag-and-Drop gesetzte) unverändert — dadurch konvergiert das
    Enforcement und es entstehen keine Endlos-Umsortierungen.
    """
    def weight(item: QueueItem) -> int:
        ip = (item.extra_data.get(META_KEY) or {}).get("ip")
        mode = rules.get(ip) if ip else None
        return {"front": 0, "back": 2}.get(mode, 1)

    return [it.prompt_id for it in sorted(pending, key=weight)]


# ---------------------------------------------------------------------------
# Umsortieren
# ---------------------------------------------------------------------------

# Minimaler Nummern-Abstand beim Einsortieren zwischen zwei bestehende Jobs.
# Wird er unterschritten (Float-Präzision nach sehr vielen Moves an derselben
# Stelle), wird einmal die komplette Pending-Queue neu durchnummeriert.
MIN_GAP = 1e-6

# Obergrenze paralleler Re-Enqueues, um Remote-Hosts nicht zu fluten.
MAX_PARALLEL_REQUEUES = 6


async def apply_order(client: ComfyClient, desired_ids: list[str]) -> dict:
    """Pending-Queue eines Hosts in die gewünschte Reihenfolge bringen.

    `desired_ids` darf unvollständig oder veraltet sein: Unbekannte IDs werden
    ignoriert, nicht genannte pending Jobs hinten (in bisheriger Reihenfolge)
    angehängt. Bewegt nur die minimal nötige Menge Jobs (siehe Modul-Docstring)
    und gibt einen Report zurück, was tatsächlich passiert ist.
    """
    raw = await client.get_queue()
    _, pending = parse_queue(raw)

    by_id = {it.prompt_id: it for it in pending}
    current = [it.prompt_id for it in pending]

    order = [pid for pid in desired_ids if pid in by_id]
    order += [pid for pid in current if pid not in set(order)]

    if len(order) < 2 or order == current:
        return {"changed": False, "requeued": [], "failed": []}

    moves = _plan_moves(order, by_id)
    if moves is None:
        # Float-Lücken aufgebraucht: alle Jobs bewegen, alte Nummern
        # aufsteigend sortiert in Zielreihenfolge neu verteilen. Damit bleiben
        # wir unter dem Server-Zähler und kollidieren nicht mit parallel
        # eintreffenden neuen Jobs.
        log.info("%s: Nummern-Lücken aufgebraucht, nummeriere Queue komplett neu", client.name)
        fresh = sorted(it.number for it in pending)
        moves = [(by_id[pid], num) for pid, num in zip(order, fresh)]

    return await _move_items(client, moves)


def _plan_moves(order: list[str], by_id: dict[str, QueueItem]) -> list[tuple[QueueItem, float]] | None:
    """Minimalen Bewegungsplan [(Job, neue Nummer)] berechnen.

    Jobs, deren Nummern entlang der Zielreihenfolge bereits aufsteigen
    (längste aufsteigende Teilfolge), bleiben stehen. Für jeden Block
    dazwischen werden Nummern zwischen den stehenbleibenden Nachbarn vergeben.
    None, wenn die Float-Abstände dafür nicht mehr reichen.
    """
    numbers = {pid: by_id[pid].number for pid in order}
    keep = _lis_ids(order, numbers)

    moves: list[tuple[QueueItem, float]] = []
    i, n = 0, len(order)
    while i < n:
        if order[i] in keep:
            i += 1
            continue
        j = i
        while j < n and order[j] not in keep:
            j += 1
        # Block [i, j) bewegt sich; links (i-1) und rechts (j) stehen Keeper
        # oder der Queue-Rand.
        lo = numbers[order[i - 1]] if i > 0 else None
        hi = numbers[order[j]] if j < n else None
        nums = _numbers_between(lo, hi, j - i)
        if nums is None:
            return None
        moves.extend((by_id[order[i + k]], nums[k]) for k in range(j - i))
        i = j
    return moves


def _numbers_between(lo: float | None, hi: float | None, count: int) -> list[float] | None:
    """`count` streng aufsteigende Nummern im offenen Intervall (lo, hi)."""
    if lo is None and hi is None:  # praktisch unerreichbar (Keeper existiert immer)
        return [float(k) for k in range(count)]
    if lo is None:
        # Block ganz vorne: einfach unterhalb des ersten Bleibenden.
        return [hi - (count - k) for k in range(count)]
    if hi is None:
        # Block ganz hinten: innerhalb von (lo, lo+1) bleiben, damit parallel
        # neu eintreffende Jobs (Server-Zähler > alle bisherigen Nummern)
        # weiterhin dahinter landen.
        step = 1.0 / (count + 1)
    else:
        step = (hi - lo) / (count + 1)
        if step < MIN_GAP:
            return None
    return [lo + step * (k + 1) for k in range(count)]


def _lis_ids(order: list[str], numbers: dict[str, float]) -> set[str]:
    """IDs einer längsten streng aufsteigenden Teilfolge (Patience Sorting)."""
    seq = [numbers[pid] for pid in order]
    keys: list[float] = []    # kleinster Endwert einer Teilfolge der Länge k+1
    tail_idx: list[int] = []  # Index des Elements, das keys[k] zuletzt setzte
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(keys, v)
        if k == len(keys):
            keys.append(v)
            tail_idx.append(i)
        else:
            keys[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k > 0 else -1
    ids: set[str] = set()
    i = tail_idx[-1] if tail_idx else -1
    while i != -1:
        ids.add(order[i])
        i = prev[i]
    return ids


async def _move_items(client: ComfyClient, moves: list[tuple[QueueItem, float]]) -> dict:
    """Geplante Moves ausführen: löschen, Re-Check, parallel neu einreihen."""
    await client.delete_pending([item.prompt_id for item, _ in moves])

    # Re-Check: Was jetzt noch existiert, ist zwischenzeitlich in den
    # Running-Zustand gewechselt und darf nicht doppelt eingereiht werden.
    running2, pending2 = parse_queue(await client.get_queue())
    still_present = {it.prompt_id for it in running2 + pending2}

    sem = asyncio.Semaphore(MAX_PARALLEL_REQUEUES)

    async def repost(item: QueueItem, number: float) -> str:
        payload = {
            "prompt": item.prompt,
            "extra_data": item.extra_data,  # enthält client_id + unsere Metadaten
            "prompt_id": item.prompt_id,
            "number": number,
        }
        async with sem:
            resp = await client.post_prompt(payload)
        if resp.get("prompt_id") != item.prompt_id:
            # Ältere ComfyUI-Versionen ignorieren die client-seitige prompt_id
            # und vergeben eine neue — Queue-Reihenfolge stimmt trotzdem, nur
            # das Client-Tracking verliert den Bezug.
            log.warning(
                "%s: Server hat prompt_id ersetzt (%s -> %s); ComfyUI-Version "
                "unterstützt vermutlich keine client-seitigen prompt_ids",
                client.name, item.prompt_id, resp.get("prompt_id"),
            )
        return item.prompt_id

    todo = [(item, num) for item, num in moves if item.prompt_id not in still_present]
    results = await asyncio.gather(
        *(repost(item, num) for item, num in todo), return_exceptions=True
    )

    requeued: list[str] = []
    failed: list[dict] = []
    for (item, _), res in zip(todo, results):
        if isinstance(res, BaseException):
            # Sollte praktisch nie passieren (der Prompt war vorher gültig).
            # Falls doch, den Job nicht stillschweigend verlieren, sondern melden.
            log.error("%s: Re-Enqueue von %s fehlgeschlagen: %s", client.name, item.prompt_id, res)
            failed.append({"prompt_id": item.prompt_id, "error": str(res)})
        else:
            requeued.append(res)

    return {"changed": True, "requeued": requeued, "failed": failed}


def build_prompt_meta(ip: str) -> dict:
    """Metadaten-Objekt, das der Proxy beim Einreihen in extra_data ablegt."""
    return {"ip": ip, "submitted_at": time.time()}
