"""Skalierungstest: Drag-and-Drop in einer langen Queue muss billig sein.

Reiht viele Jobs ein (Standard: 300), verschiebt per Reorder-API genau einen
Job weit nach vorne und prüft direkt in der Mock-ComfyUI-Queue, dass NUR
dieser eine Job eine neue `number` bekommen hat — alle anderen Jobs wurden
nicht angefasst (kein Löschen + Neu-Einreihen der ganzen Queue mehr).

Erwartet wie der Smoke-Test einen laufenden Mock (tests/mock_comfy.py) und
Manager (CQM_BASE, Standard http://127.0.0.1:8189).
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("CQM_BASE", "http://127.0.0.1:8189")
MOCK = os.environ.get("CQM_MOCK", "http://127.0.0.1:8288")
N_JOBS = int(os.environ.get("CQM_SCALE_JOBS", "300"))


def check(name: str, cond: bool, info: str = "") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  ({info})" if info and not cond else ""))
    if not cond:
        sys.exit(1)


def mock_pending_numbers(c: httpx.Client) -> dict[str, float]:
    """prompt_id -> number, direkt aus der Mock-Queue (ungefiltert)."""
    q = c.get(f"{MOCK}/queue").json()
    return {item[1]: float(item[0]) for item in q["queue_pending"]}


def main() -> None:
    c = httpx.Client(timeout=60)

    # Saubere Ausgangslage: keine Priority-Regeln, leere Queue.
    c.put(f"{BASE}/api/priority-ips", json=[]).raise_for_status()
    c.post(f"{BASE}/api/hosts/mock/clear").raise_for_status()

    print(f"1. {N_JOBS} Jobs über den Proxy einreihen")
    t0 = time.monotonic()
    ids = []
    for i in range(N_JOBS):
        r = c.post(
            f"{BASE}/proxy/mock/prompt",
            json={"prompt": {"1": {"class_type": "CLIPTextEncode",
                                   "inputs": {"text": f"job {i}"}}}},
            headers={"X-Forwarded-For": f"10.1.{i % 8}.{i % 250}"},
        )
        r.raise_for_status()
        ids.append(r.json()["prompt_id"])
    print(f"   eingereiht in {time.monotonic() - t0:.1f}s (Job 0 läuft, Rest pending)")

    pending = ids[1:]  # ids[0] läuft im Mock sofort
    before = mock_pending_numbers(c)
    check(f"{len(pending)} Jobs pending", set(before) == set(pending))

    print("2. Einen Job von weit hinten an Position 3 ziehen (Reorder-API)")
    moved = pending[-10]
    order = [p for p in pending if p != moved]
    order.insert(2, moved)

    t0 = time.monotonic()
    r = c.post(f"{BASE}/api/hosts/mock/reorder", json={"order": order})
    r.raise_for_status()
    dt = time.monotonic() - t0
    report = r.json()["report"]

    after = mock_pending_numbers(c)
    changed = [pid for pid in pending if before.get(pid) != after.get(pid)]
    new_order = [pid for pid, _ in sorted(after.items(), key=lambda kv: kv[1])]

    check("Reorder erfolgreich", report["changed"] and not report["failed"])
    check("Nur der gezogene Job wurde neu eingereiht", changed == [moved],
          f"neu eingereiht: {len(changed)} Jobs")
    check("Genau 1 Re-Enqueue im Report", report["requeued"] == [moved],
          f"requeued: {len(report['requeued'])}")
    check("Zielreihenfolge stimmt", new_order == order)
    check(f"Reorder schnell (<1s, war {dt:.2f}s)", dt < 1.0, f"{dt:.2f}s")

    print("3. Job ganz nach hinten ziehen")
    tail = new_order[5]
    order2 = [p for p in new_order if p != tail] + [tail]
    before2 = after
    c.post(f"{BASE}/api/hosts/mock/reorder", json={"order": order2}).raise_for_status()
    after2 = mock_pending_numbers(c)
    changed2 = [pid for pid in order2 if before2.get(pid) != after2.get(pid)]
    check("Auch hier nur 1 Job bewegt", changed2 == [tail],
          f"bewegt: {len(changed2)}")
    check("Neue Nummer bleibt unter dem Server-Zähler (Fractional-Trick)",
          after2[tail] < max(before2.values()) + 1)

    print("\nSkalierungstest bestanden ✓")


if __name__ == "__main__":
    main()
