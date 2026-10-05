"""Tailscale, as the Phone page sees it: is the private HTTPS route ready?

Read-only. It runs `tailscale status --json` and `tailscale serve status
--json` and never changes anything - setting up Serve stays a command you
run yourself (`tailscale serve --bg 8766`), because it changes Tailscale's
configuration.

The one thing it insists on: VPA's listener must be reachable from YOUR
devices only. If Tailscale Funnel (which publishes a port to the whole
internet) is on for the address that reaches VPA, that is reported loudly.

Results are cached for 20 s: the Phone page polls every few seconds and the
CLI takes a moment to answer.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

_CACHE: dict = {"t": 0.0, "port": None, "v": None}
TTL = 20.0


def _exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base and (Path(base) / "Tailscale" / "tailscale.exe").is_file():
            return str(Path(base) / "Tailscale" / "tailscale.exe")
    return None


def _run(exe: str, *args) -> dict | None:
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=6,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return None


def read_status(status: dict | None, serve: dict | None, port: int) -> dict:
    """The verdict, from the two JSON answers (separate so tests can feed
    them in). state: missing | stopped | no-https | not-served | ready | funnel."""
    if status is None:
        return {"state": "stopped", "installed": True}
    if status.get("BackendState") != "Running":
        return {"state": "stopped", "installed": True,
                "detail": str(status.get("BackendState") or "not running")}
    me = status.get("Self") or {}
    name = str(me.get("DNSName") or "").rstrip(".")
    certs = [str(c).rstrip(".") for c in (status.get("CertDomains") or [])]
    out = {"installed": True, "name": name, "ip": (me.get("TailscaleIPs") or [None])[0],
           "https_url": f"https://{name}" if name else None,
           "peers": [{"name": p.get("HostName"), "os": p.get("OS"), "online": bool(p.get("Online"))}
                     for p in (status.get("Peer") or {}).values()]}
    if not name or name not in certs:
        return dict(out, state="no-https")
    web = (serve or {}).get("Web") or {}
    handler = (web.get(f"{name}:443") or {}).get("Handlers", {}).get("/") or {}
    target = str(handler.get("Proxy") or "")
    funnel = bool(((serve or {}).get("AllowFunnel") or {}).get(f"{name}:443"))
    ours = target.rstrip("/") in (f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                                  f"127.0.0.1:{port}", f"localhost:{port}", str(port))
    out["serve_target"] = target or None
    if funnel and ours:
        return dict(out, state="funnel")
    if not ours:
        return dict(out, state="not-served")
    return dict(out, state="ready")


def check(port: int) -> dict:
    """Tailscale's view, cached for TTL seconds."""
    now = time.time()
    if _CACHE["v"] is not None and _CACHE["port"] == port and now - _CACHE["t"] < TTL:
        return _CACHE["v"]
    exe = _exe()
    if not exe:
        v = {"state": "missing", "installed": False}
    else:
        st = _run(exe, "status", "--json")
        sv = _run(exe, "serve", "status", "--json") if st else None
        v = read_status(st, sv, port)
    v["command"] = f"tailscale serve --bg {port}"
    _CACHE.update(t=now, port=port, v=v)
    return v
