"""End-to-End-Smoke-Test gegen Backend + Mock-ComfyUI.

Erwartet:
  * Mock-ComfyUI auf 127.0.0.1:8288  (tests/mock_comfy.py)
  * Queue Manager auf 127.0.0.1:8189 mit Test-Config (Host "mock",
    poll_interval 0.5 s) — siehe tests/run_smoke.sh

Simuliert mehrere Clients über X-Forwarded-For-Header und prüft:
IP-Tracking, Drag-and-Drop-Reorder (API), Priority-IP-Enforcement,
Löschen, Interrupt und Offline-Erkennung.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("CQM_BASE", "http://127.0.0.1:8189")
WORKFLOW = {  # minimaler "Workflow" mit erkennbarem Prompt-Text
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "PLATZHALTER"}},
    "2": {"class_type": "KSampler", "inputs": {"seed": 1}},
}

passed = 0


def check(name: str, cond: bool, info: str = "") -> None:
    global passed
    status = "OK " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({info})" if info and not cond else ""))
    if not cond:
        sys.exit(1)
    passed += 1


def submit(client: httpx.Client, ip: str, text: str) -> str:
    wf = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": text}},
          "2": {"class_type": "KSampler", "inputs": {"seed": 1}}}
    r = client.post(f"{BASE}/proxy/mock/prompt",
                    json={"prompt": wf},
                    headers={"X-Forwarded-For": ip})
    r.raise_for_status()
    return r.json()["prompt_id"]


def get_mock_state(client: httpx.Client) -> dict:
    r = client.post(f"{BASE}/api/refresh")
    r.raise_for_status()
    return next(h for h in r.json()["hosts"] if h["name"] == "mock")


def pending_ids(state: dict) -> list[str]:
    return [it["prompt_id"] for it in state["pending"]]


def main() -> None:
    c = httpx.Client(timeout=10)

    print("1. Jobs über den Proxy einreihen (3 simulierte Client-IPs)")
    a = submit(c, "10.0.0.1", "ein Astronaut auf einem Pferd")
    b = submit(c, "10.0.0.2", "eine Katze im Regen")
    d = submit(c, "10.0.0.3", "cyberpunk stadt bei nacht")
    e = submit(c, "10.0.0.1", "portrait, studio licht")

    s = get_mock_state(c)
    check("Host online", s["online"] is True)
    check("Job A läuft (Mock simuliert belegte GPU)",
          [it["prompt_id"] for it in s["running"]] == [a])
    check("Pending-Reihenfolge = Einreihungsreihenfolge", pending_ids(s) == [b, d, e])
    check("IP wurde beim Einreihen erfasst",
          [it["ip"] for it in s["pending"]] == ["10.0.0.2", "10.0.0.3", "10.0.0.1"])
    check("Prompt-Text wird extrahiert",
          s["pending"][0]["name"] == "eine Katze im Regen")
    check("Zeitstempel vorhanden",
          all(isinstance(it["submitted_at"], float) for it in s["pending"]))

    print("2. Manuelles Umsortieren (wie Drag-and-Drop im Dashboard)")
    r = c.post(f"{BASE}/api/hosts/mock/reorder", json={"order": [e, d, b]})
    r.raise_for_status()
    report = r.json()["report"]
    check("Reorder meldet Erfolg", report["changed"] and not report["failed"])
    s = get_mock_state(c)
    check("Neue Reihenfolge aktiv", pending_ids(s) == [e, d, b])
    check("prompt_ids blieben beim Reorder erhalten", set(pending_ids(s)) == {b, d, e})
    check("IP-Metadaten überleben das Reorder",
          [it["ip"] for it in s["pending"]] == ["10.0.0.1", "10.0.0.3", "10.0.0.2"])
    check("Laufender Job wurde nicht angefasst",
          [it["prompt_id"] for it in s["running"]] == [a])

    print("3. Priority-IP-Regeln (front/back) mit automatischem Enforcement")
    r = c.put(f"{BASE}/api/priority-ips", json=[
        {"ip": "10.0.0.2", "mode": "front"},
        {"ip": "10.0.0.1", "mode": "back"},
    ])
    r.raise_for_status()
    time.sleep(1.5)  # Poller-Enforcement abwarten (Intervall 0.5 s)
    s = get_mock_state(c)
    check("front-IP steht vorne, back-IP hinten", pending_ids(s) == [b, d, e],
          f"ist: {pending_ids(s)}")
    check("Regel wird im Item angezeigt", s["pending"][0]["priority_rule"] == "front")

    print("4. Neuer Job einer back-IP bleibt hinter neutralen Jobs")
    f = submit(c, "10.0.0.1", "noch ein portrait")   # back-IP
    g = submit(c, "10.0.0.4", "landschaft, nebel")   # neutral
    time.sleep(1.5)
    s = get_mock_state(c)
    check("Enforcement sortiert back-IP-Jobs hinter neue neutrale",
          pending_ids(s) == [b, d, g, e, f], f"ist: {pending_ids(s)}")

    print("5. Löschen & Interrupt")
    c.post(f"{BASE}/api/hosts/mock/delete", json={"ids": [d]}).raise_for_status()
    s = get_mock_state(c)
    check("Job gelöscht", d not in pending_ids(s))

    c.post(f"{BASE}/api/hosts/mock/interrupt", json={"prompt_id": a}).raise_for_status()
    s = get_mock_state(c)
    check("Interrupt: nächster Job läuft",
          s["running"] and s["running"][0]["prompt_id"] == b)

    print("6. Offline-Host wird sauber gemeldet")
    r = c.post(f"{BASE}/api/refresh")
    down = next(h for h in r.json()["hosts"] if h["name"] == "down")
    check("Offline-Host: online=false + Fehlermeldung",
          down["online"] is False and down["error"])

    print(f"\nAlle {passed} Checks bestanden ✓")


if __name__ == "__main__":
    main()
