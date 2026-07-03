# ComfyUI Queue Manager

Web-Dashboard zur Verwaltung der Queues mehrerer ComfyUI-Instanzen (lokal +
remote, z. B. RunPod), auf die mehrere Clients gleichzeitig zugreifen.

**Features**

- Queue-Übersicht (running + pending) pro Host: Prompt-Text, Client-IP,
  Zeitstempel, Position, Status
- IP-Tracking über einen eingebauten Reverse-Proxy (ComfyUI selbst speichert
  keine Client-IPs)
- Drag-and-Drop-Umsortierung der Pending-Queue (SortableJS), umgesetzt in der
  echten ComfyUI-Queue — laufende Jobs bleiben unberührt
- Priority-IPs: konfigurierbare Regeln „immer nach vorne" / „immer nach
  hinten", automatisch durchgesetzt bei jeder Queue-Änderung, persistiert in
  `config.json`, editierbar im Dashboard
- Jobs löschen, laufende Jobs unterbrechen, Queue leeren
- Offline-Hosts werden klar markiert statt das Dashboard zu blockieren

---

## Wie es funktioniert

### IP-Tracking (Proxy)

ComfyUI speichert nirgendwo, von welcher IP ein Prompt kam. Deshalb schicken
die Clients ihre Requests nicht direkt an ComfyUI, sondern an den Proxy des
Queue Managers:

```
statt   http://gpu-server:8188          (direkt)
nutzen  http://manager:8189/proxy/local  (durch den Proxy)
```

Der Proxy reicht **alle** Requests unverändert durch (auch die normale
ComfyUI-Weboberfläche und der WebSocket `/ws` funktionieren durch ihn
hindurch). Nur `POST /prompt` wird abgefangen: Client-IP + Zeitstempel werden
in `extra_data["cqm"]` eingebettet. Diese Metadaten wandern mit dem Job durch
Queue und History — sie überleben also auch einen Neustart des Managers.

Jobs, die an ComfyUI vorbei direkt eingereiht werden, erscheinen im Dashboard
mit IP „unbekannt".

### Umsortieren

ComfyUI bietet kein natives Reordering. Die Pending-Queue ist aber ein Heap,
sortiert nach dem Feld `number` (Float), und `POST /prompt` akzeptiert eine
explizite `number` sowie eine client-seitige `prompt_id`. Umsortieren läuft
deshalb so:

1. Pending-Einträge einlesen (`GET /queue`)
2. Minimal nötige Menge bewegter Jobs bestimmen: Jobs, deren Nummern entlang
   der Zielreihenfolge bereits aufsteigen (längste aufsteigende Teilfolge),
   bleiben stehen — bei einem einzelnen Drag ist genau 1 Job zu bewegen
3. Nur diese Jobs löschen (`POST /queue {"delete": [...]}`) und mit derselben
   `prompt_id` und einer `number` **zwischen** den neuen Nachbarn neu
   einreihen — der laufende Job wird nie unterbrochen

Ein Drag-and-Drop kostet damit auch bei hunderten Einträgen nur zwei
HTTP-Requests statt 2·Queue-Länge (wichtig bei Remote-Hosts wie RunPod).
Erst wenn die Float-Lücken nach sehr vielen Moves an derselben Stelle
aufgebraucht sind, wird einmalig die komplette Queue neu durchnummeriert.

Job-IDs bleiben erhalten (Client-Tracking funktioniert weiter) und parallel
neu eintreffende Jobs sortieren sich korrekt hinter die umsortierten. Ein
Re-Check nach dem Löschen fängt das Rennen ab, dass ein Job genau in dem
Moment zu laufen beginnt.

Das Dashboard rendert außerdem inkrementell: Eine Host-Karte wird nur neu
aufgebaut, wenn sich ihr Inhalt tatsächlich geändert hat — auch lange Queues
bleiben so beim Pollen und Draggen flüssig.

### Grenzen / Hinweise

- Client-seitige `prompt_id`s braucht eine halbwegs aktuelle ComfyUI-Version
  (im lokalen v0.26-Quellcode verifiziert). Ältere Versionen vergeben beim
  Reorder neue IDs — die Reihenfolge stimmt trotzdem, nur das Tracking des
  einreichenden Clients verliert den Bezug (wird im Log gewarnt).
- ComfyUIs eigenes `create_time` wird beim Re-Enqueue erneuert; der Manager
  zeigt deshalb bevorzugt den eigenen Zeitstempel aus `extra_data["cqm"]`.
- Sensible `extra_data`-Schlüssel (z. B. Auth-Tokens für API-Nodes) werden
  von ComfyUI aus `GET /queue` herausgefiltert und gehen beim Umsortieren
  verloren — Workflows mit API-Nodes also nicht umsortieren.
- `X-Forwarded-For` wird vertraut, falls gesetzt. Wenn der Manager direkt
  im Netz hängt (kein Load-Balancer davor), können Clients den Header
  theoretisch fälschen.

---

## Installation & Start

```bash
cd comfy-queue-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

Dashboard: **http://localhost:8189**

### Konfiguration (`config.json`)

```json
{
  "hosts": [
    { "name": "local",  "url": "http://127.0.0.1:8188" },
    { "name": "runpod", "url": "https://DEIN-POD-ID-8188.proxy.runpod.net" }
  ],
  "priority_ips": [
    { "ip": "192.168.1.50", "mode": "front" }
  ],
  "poll_interval_seconds": 3.0,
  "enforce_priority_rules": true,
  "listen": { "host": "0.0.0.0", "port": 8189 }
}
```

- `hosts`: beliebig viele ComfyUI-Instanzen (Name = URL-Slug, URL inkl. Port)
- `priority_ips`: wird auch vom Dashboard aus editiert und hierher persistiert
- Alternativer Config-Pfad über Umgebungsvariable `CQM_CONFIG`

### RunPod anbinden

1. Pod mit ComfyUI starten, Port 8188 als HTTP-Port exponieren.
2. Die öffentliche URL des Pods eintragen, z. B.
   `https://abc123xyz-8188.proxy.runpod.net` (RunPod-Proxy) oder
   `http://<public-ip>:<mapped-port>` (direktes TCP-Mapping).
3. Manager neu starten. Der Host taucht im Dashboard auf; ist der Pod
   gestoppt, wird er als offline markiert.

Damit die IP-Herkunft auch für den RunPod-Host erfasst wird, reichen die
Clients ihre Prompts über `http://<manager>:8189/proxy/runpod` ein.

### Clients umstellen

Überall dort, wo bisher die ComfyUI-URL stand, die Proxy-URL verwenden:

- **ComfyUI-Weboberfläche:** einfach `http://<manager>:8189/proxy/local/`
  im Browser öffnen (statt `http://gpu-server:8188`).
- **Eigene Skripte/API-Clients:** Basis-URL auf
  `http://<manager>:8189/proxy/<hostname>` setzen — alle Endpunkte
  (`/prompt`, `/queue`, `/history`, `/view`, `/ws`, …) werden durchgereicht.

---

## API-Überblick (Manager)

| Methode | Pfad | Zweck |
| --- | --- | --- |
| GET | `/api/state` | Zustand aller Hosts (aus dem Poller-Cache) |
| POST | `/api/refresh` | Alle Hosts sofort neu abfragen |
| POST | `/api/hosts/{name}/reorder` | `{"order": [prompt_ids]}` — Pending-Queue umsortieren |
| POST | `/api/hosts/{name}/delete` | `{"ids": [...]}` — Pending-Einträge löschen |
| POST | `/api/hosts/{name}/interrupt` | `{"prompt_id"?}` — laufenden Job stoppen |
| POST | `/api/hosts/{name}/clear` | Pending-Queue leeren |
| GET/PUT | `/api/priority-ips` | Priority-Regeln lesen / komplett ersetzen |
| ALLE | `/proxy/{name}/…` | Reverse-Proxy zu ComfyUI (inkl. WebSocket `/ws`) |

## Tests

Ohne echte GPU-Instanz, gegen einen Mock-ComfyUI-Server mit identischer
Queue-Semantik:

```bash
# Terminal 1: Mock-ComfyUI
.venv/bin/python tests/mock_comfy.py 8288

# Terminal 2: Manager mit Test-Config (Host "mock", Polling 0.5 s)
CQM_CONFIG=tests/test-config.json .venv/bin/python run.py

# Terminal 3: 17 End-to-End-Checks
.venv/bin/python tests/smoke_test.py

# optional: Skalierungstest (300 Jobs, Drag = genau 1 Re-Enqueue)
.venv/bin/python tests/scale_test.py

# ohne Server: Unit-Tests der Prompt-Namens-Extraktion
.venv/bin/python tests/name_test.py
```

Beide Tests akzeptieren `CQM_BASE` (Manager-URL), falls der Manager auf
einem anderen Port läuft.

## Projektstruktur

```
app/
  config.py       Config laden/speichern (Hosts, Priority-IPs)
  comfy.py        HTTP-Client für die ComfyUI-API eines Hosts
  queue_logic.py  Queue-Parsing, Reordering, Priority-Logik
  state.py        Poller, Zustands-Cache, Enforcement
  proxy.py        Reverse-Proxy mit IP-Tracking + WebSocket-Bridge
  main.py         FastAPI-Routen
static/           Dashboard (Vanilla JS + SortableJS)
tests/            Mock-ComfyUI + Smoke-Test
config.json       Konfiguration (Hosts, Regeln, Ports)
```
