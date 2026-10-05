"""Phone sync: settings, paired phones and pairing codes.

Shared by the laptop app (which changes the settings) and the phone listener
(which checks tokens and pairs phones), which may be two separate processes -
the listener can run in the background with the app closed. So everything
lives in one small file, data/phone.json, and every change is a
read-modify-write under a lock file, written atomically.

Standard library only, and no route to the database: the phone side of the
app is built so that nothing it can be asked to do reaches vpa.db.

    enabled      phones may sync at all
    lan          also listen on the home network (plain http; for trying it
                 out - offline use needs the HTTPS address, see phone.py)
    background   keep the listener running with the app closed (starts with
                 Windows)
    devices      paired phones: a name and the SHA-256 of their token - the
                 token itself only ever exists on the phone
    pair         the pairing code on screen right now, if any (hashed),
                 with its expiry and wrong guesses so far
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "data" / "phone.json"
OUT = ROOT / "output" / "phone"          # the phone copy (snapshot) lives here
FORMAT = "vpa-phone/2"                   # snapshot layout (phonebuild.FORMAT)
APP_ID = "vpa-phone"
DEFAULT_PORT = 8766
PAIR_SECONDS = 600                       # a pairing code lasts ten minutes
PAIR_TRIES = 5                           # ...or five wrong guesses
MAX_DEVICES = 10

DEFAULTS = {"enabled": False, "lan": False, "background": False,
            "port": DEFAULT_PORT, "devices": [], "pair": None,
            "https_url": None}       # the Tailscale address, when Serve is ready


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ storage
@contextmanager
def _locked(timeout: float = 8.0):
    """Cross-process lock: a lock file created exclusively. One left behind
    by a crashed process is taken over after 15 s (no change takes that
    long)."""
    lock = SETTINGS.with_name(SETTINGS.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 15:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("phone settings are busy - try again")
            time.sleep(0.03)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def _replace(src: Path, dst: Path, tries: int = 40) -> None:
    """os.replace, retried: on Windows it is refused while anything has the
    target open - a reader in another thread or process, or OneDrive syncing
    the file (data/ lives in OneDrive). Those holds last milliseconds."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.025 * (1 + i // 8))


def read_text_retry(path: Path, tries: int = 40) -> str:
    """read_text, retried while Windows briefly refuses it: the moment a
    writer's os.replace lands, or OneDrive touching the file. Treating that
    moment as "no file" made a paired phone's sync read as unpaired."""
    for i in range(tries):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.025 * (1 + i // 8))
    raise OSError(path)


def load() -> dict:
    """The settings, with defaults filled in. Unreadable means defaults:
    phone sync off and nothing paired - the safe way to fail."""
    s = dict(DEFAULTS)
    try:
        d = json.loads(read_text_retry(SETTINGS))
        if isinstance(d, dict):
            s.update({k: d[k] for k in DEFAULTS if k in d})
    except (OSError, ValueError):
        pass
    s["devices"] = [d for d in (s.get("devices") or []) if isinstance(d, dict)]
    return s


def update(fn) -> dict:
    """Apply fn(settings) under the lock and write the result atomically.
    Returns the settings as written. fn's return value, if not None, is
    stored under the key "_result" of the returned dict (not saved)."""
    with _locked():
        s = load()
        res = fn(s)
        s.pop("_result", None)
        tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        _replace(tmp, SETTINGS)
    if res is not None:
        s = dict(s, _result=res)
    return s


def set_options(**opts) -> dict:
    """enabled / lan / background. Turning sync off also ends any pairing."""
    def f(s):
        for k in ("enabled", "lan", "background"):
            if k in opts and opts[k] is not None:
                s[k] = bool(opts[k])
        if not s["enabled"]:
            s["pair"] = None
    return update(f)


# ------------------------------------------------------------------ the copy
def read_meta() -> dict | None:
    """output/phone/meta.json - which snapshot is current (phone.py writes it)."""
    try:
        m = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) and m.get("file") else None
    except (OSError, ValueError):
        return None


def snapshot_path(meta: dict) -> Path:
    name = str(meta.get("file", ""))
    if "/" in name or "\\" in name or not name.startswith("snapshot-"):
        raise ValueError("bad snapshot name in meta.json")
    return OUT / name


# ------------------------------------------------------------------ pairing
def start_pairing() -> dict:
    """A fresh six-digit code, shown on the laptop and typed on the phone.
    Only its hash is stored; it lasts ten minutes or five wrong guesses."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    exp = time.time() + PAIR_SECONDS

    def f(s):
        if not s["enabled"]:
            return "off"
        if len(s["devices"]) >= MAX_DEVICES:
            return "full"
        s["pair"] = {"code": _sha(code), "expires": exp, "tries": 0}
    s = update(f)
    if s.get("_result") == "off":
        return {"ok": False, "error": "turn phone sync on first"}
    if s.get("_result") == "full":
        return {"ok": False, "error": f"{MAX_DEVICES} phones are paired already - "
                                      f"remove one first"}
    return {"ok": True, "code": code, "expires": exp, "seconds": PAIR_SECONDS}


def cancel_pairing() -> dict:
    update(lambda s: s.__setitem__("pair", None))
    return {"ok": True}


def redeem(code: str, name: str) -> dict:
    """The phone's half of pairing. Right code: a new device and its token
    (returned once, never stored). Wrong code: one of five tries used."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[:6]
    name = " ".join(str(name or "").split())[:40] or "Phone"
    token = secrets.token_urlsafe(32)

    def f(s):
        p = s.get("pair")
        if not s["enabled"] or not isinstance(p, dict):
            return "none"
        if time.time() > float(p.get("expires", 0)):
            s["pair"] = None
            return "expired"
        if len(code) != 6 or not hmac.compare_digest(_sha(code), str(p.get("code"))):
            p["tries"] = int(p.get("tries", 0)) + 1
            if p["tries"] >= PAIR_TRIES:
                s["pair"] = None
                return "voided"
            return "wrong"
        dev = {"id": secrets.token_hex(4), "name": name, "token": _sha(token),
               "paired": _now(), "last_seen": None, "last_version": None}
        s["devices"].append(dev)
        s["pair"] = None
        return dev
    r = update(f).get("_result")
    if isinstance(r, dict):
        return {"ok": True, "token": token, "device": {"id": r["id"], "name": r["name"]}}
    return {"ok": False, "error": {
        "none": "no pairing code is showing on the laptop - press Pair a phone there first",
        "expired": "that code has expired - press Pair a phone on the laptop again",
        "voided": "too many wrong codes - press Pair a phone on the laptop again",
        "wrong": "that is not the code on the laptop - check it and try again",
    }[r]}


def device_for(token: str, settings: dict | None = None) -> dict | None:
    """The paired phone this token belongs to, or None. Constant-time
    comparison against every device, so timing says nothing."""
    if not token or len(token) > 200:
        return None
    s = settings or load()
    if not s["enabled"]:
        return None
    h = _sha(token)
    hit = None
    for d in s["devices"]:
        if hmac.compare_digest(h, str(d.get("token", ""))):
            hit = d
    return hit


def seen(device_id: str, version: str | None) -> None:
    """Record that a phone synced. Written at most once a minute per phone
    unless it took a new version, so syncing never churns the file."""
    s = load()
    d = next((d for d in s["devices"] if d.get("id") == device_id), None)
    if d is None:
        return
    if d.get("last_version") == version and d.get("_t", 0) > time.time() - 60:
        return

    def f(s):
        for d in s["devices"]:
            if d.get("id") == device_id:
                d["last_seen"] = _now()
                d["_t"] = time.time()
                if version:
                    d["last_version"] = version
    try:
        update(f)
    except (TimeoutError, OSError):
        pass                         # a missed "last seen" must never fail a sync


def remove_device(device_id: str | None = None, everything: bool = False) -> dict:
    def f(s):
        before = len(s["devices"])
        s["devices"] = [] if everything else \
            [d for d in s["devices"] if d.get("id") != device_id]
        return before - len(s["devices"])
    n = update(f).get("_result") or 0
    return {"ok": bool(n), "removed": n,
            **({} if n else {"error": "no such phone"})}


def public(s: dict | None = None) -> dict:
    """The settings without secrets - for the laptop page."""
    s = s or load()
    p = s.get("pair")
    live = isinstance(p, dict) and time.time() < float(p.get("expires", 0))
    return {"enabled": s["enabled"], "lan": s["lan"], "background": s["background"],
            "port": s["port"],
            "devices": [{k: d.get(k) for k in ("id", "name", "paired", "last_seen",
                                               "last_version")} for d in s["devices"]],
            "pairing": {"expires": p["expires"], "tries": p.get("tries", 0)} if live else None}
