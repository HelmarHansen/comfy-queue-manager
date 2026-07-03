"""Laden und Speichern der Konfiguration (Hosts, Priority-IPs, Polling).

Die Konfiguration liegt bewusst getrennt vom Code in einer JSON-Datei.
Standardpfad: <Projektwurzel>/config.json, überschreibbar über die
Umgebungsvariable CQM_CONFIG (nützlich für Tests oder mehrere Setups).

Priority-IP-Regeln werden vom Dashboard aus editiert und über save_config()
zurück in dieselbe Datei geschrieben (atomar, damit ein Absturz beim
Schreiben die Config nicht zerstört).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.json"
CONFIG_PATH = Path(os.environ.get("CQM_CONFIG", _DEFAULT_PATH))

# Hostnamen landen in URLs (/proxy/<name>/...), deshalb nur einfache Slugs.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_write_lock = threading.Lock()


@dataclass
class HostConfig:
    """Eine ComfyUI-Instanz (lokal oder remote, z. B. RunPod)."""

    name: str  # eindeutiger Kurzname, wird in URLs verwendet
    url: str   # Basis-URL inkl. Port, z. B. http://127.0.0.1:8188


@dataclass
class PriorityRule:
    """Regel für eine Client-IP.

    mode = "front": Jobs dieser IP stehen immer vor allen anderen pending Jobs.
    mode = "back":  Jobs dieser IP werden immer ans Ende geschoben.
    """

    ip: str
    mode: str  # "front" | "back"


@dataclass
class AppConfig:
    hosts: list[HostConfig] = field(default_factory=list)
    priority_ips: list[PriorityRule] = field(default_factory=list)
    poll_interval_seconds: float = 3.0
    enforce_priority_rules: bool = True
    listen_host: str = "0.0.0.0"
    listen_port: int = 8189

    def host_by_name(self, name: str) -> HostConfig | None:
        for h in self.hosts:
            if h.name == name:
                return h
        return None

    def rules_by_ip(self) -> dict[str, str]:
        """IP -> Modus, für schnelle Lookups bei der Queue-Auswertung."""
        return {r.ip: r.mode for r in self.priority_ips}


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))

    hosts = []
    for h in raw.get("hosts", []):
        name = str(h["name"]).strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"Ungültiger Hostname {name!r} (nur a-z, 0-9, -, _ erlaubt)")
        hosts.append(HostConfig(name=name, url=str(h["url"]).rstrip("/")))

    rules = [
        PriorityRule(ip=str(r["ip"]).strip(), mode=str(r.get("mode", "front")))
        for r in raw.get("priority_ips", [])
    ]

    listen = raw.get("listen", {})
    return AppConfig(
        hosts=hosts,
        priority_ips=rules,
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 3.0)),
        enforce_priority_rules=bool(raw.get("enforce_priority_rules", True)),
        listen_host=str(listen.get("host", "0.0.0.0")),
        listen_port=int(listen.get("port", 8189)),
    )


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Config atomar zurückschreiben (Temp-Datei + os.replace)."""
    data = {
        "hosts": [{"name": h.name, "url": h.url} for h in cfg.hosts],
        "priority_ips": [{"ip": r.ip, "mode": r.mode} for r in cfg.priority_ips],
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "enforce_priority_rules": cfg.enforce_priority_rules,
        "listen": {"host": cfg.listen_host, "port": cfg.listen_port},
    }
    with _write_lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
