"""Stars from phones: the inbox between the phone listener and the database.

The phone is otherwise read-only. Starring is the one thing it can ask the
laptop to do, and the listener still never opens the database
(tests/check_phone.py enforces that): it only drops the request here, in a
small file beside data/phone.json. The process that supervises phone sync
(phone.py - the app, or the background listener's supervisor) picks the
requests up within seconds and applies them with starred.apply_phone_ops,
which touches the `starred` table and nothing else.

    queue   requests received, not yet applied:
            {id, k, on, at, dev, name, got}
              id    the phone's random id for the tap - a resend of the same
                    tap (a sync cut off mid-answer) is recognised, not doubled
              k     the car's key, as the phone's copy names it
              on    star (true) or unstar (false) - a state, not a toggle,
                    so applying one twice changes nothing the second time
              at    when it was tapped, laptop-local time (the phone sends
                    epoch ms; a clock far in the future is clamped to now)
    done    what became of each request, kept 60 days so the phone that
            sent it can ask: r = applied | already | kept | gone, the
            time it was decided, and any note an unstar removed
    backup  the database backup made before the FIRST change a phone ever
            made - the moment a new kind of write begins

Standard library and phonecfg only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import phonecfg

FORMAT = "vpa-phone-stars/1"
MAX_OPS = 25                 # per request (the body limit is 4 KB)
MAX_QUEUE = 1000             # waiting at once, every phone together
KEEP_DAYS = 60               # how long an outcome can still be asked for
KEEP_DONE = 3000
MAX_AGE_DAYS = 180           # a tap older than this is refused
_ID = re.compile(r"^[A-Za-z0-9]{8,40}$")
_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")


class QueueFull(Exception):
    pass


def path() -> Path:
    return phonecfg.SETTINGS.with_name("phone_stars.json")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fmt(d: datetime) -> str:
    return d.isoformat(sep=" ", timespec="seconds")


def _read(strict: bool) -> dict:
    """The file, or an empty one if there is none. strict: a file that is
    there but unreadable RAISES - rewriting it from nothing would lose
    requests not yet applied."""
    p = path()
    empty = {"format": FORMAT, "queue": [], "done": [], "backup": None}
    try:
        d = json.loads(phonecfg.read_text_retry(p))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        if strict:
            raise OSError(f"{p.name} is unreadable - not changing it")
        return empty
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        if strict:
            raise OSError(f"{p.name} is not a {FORMAT} file - not changing it")
        return empty
    return {"format": FORMAT,
            "queue": [o for o in d.get("queue") or [] if isinstance(o, dict)],
            "done": [o for o in d.get("done") or [] if isinstance(o, dict)],
            "backup": d.get("backup")}


def _write(d: dict) -> None:
    p = path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    phonecfg._replace(tmp, p)


def clean_op(o) -> dict | None:
    """One request from a phone, checked field by field; None if anything
    is off. Only these four fields are ever kept from what a phone sends."""
    if not isinstance(o, dict):
        return None
    i, k, on, at = o.get("id"), o.get("k"), o.get("on"), o.get("at")
    if not (isinstance(i, str) and _ID.match(i) and isinstance(k, str) and _KEY.match(k)
            and isinstance(on, bool) and isinstance(at, (int, float))
            and not isinstance(at, bool)):
        return None
    try:
        when = datetime.fromtimestamp(at / 1000).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None
    now = _now()
    if when < now - timedelta(days=MAX_AGE_DAYS):
        return None
    if when > now + timedelta(minutes=5):        # a phone clock running fast
        when = now
    return {"id": i, "k": k, "on": on, "at": _fmt(when)}


def receive(device: dict, ops, ask) -> dict:
    """The listener's half: queue new requests from one paired phone, and
    answer what became of earlier ones it asks about (its own only)."""
    ops = ops if isinstance(ops, list) else []
    ask = [a for a in (ask if isinstance(ask, list) else []) if isinstance(a, str)][:200]
    if len(ops) > MAX_OPS:
        raise ValueError(f"at most {MAX_OPS} changes at a time")
    clean = [clean_op(o) for o in ops]
    if any(c is None for c in clean):
        raise ValueError("a change was not understood")
    accepted = []
    with phonecfg._locked(lock=path().with_name(path().name + ".lock")):
        d = _read(strict=True)
        seen = {o.get("id") for o in d["queue"]} | {o.get("id") for o in d["done"]}
        new = [c for c in clean if c["id"] not in seen]
        if new and len(d["queue"]) + len(new) > MAX_QUEUE:
            raise QueueFull("the laptop has too many changes waiting - try later")
        got = _fmt(_now())
        for c in new:
            d["queue"].append(dict(c, dev=device.get("id"), name=device.get("name"), got=got))
        if new:
            _write(d)
        accepted = [c["id"] for c in clean]          # resends count as received
    mine = {o["id"]: o for o in d["done"] if o.get("dev") == device.get("id")}
    results = {i: {k: mine[i].get(k) for k in ("r", "decided", "key", "note", "why")}
               for i in ask if i in mine}
    waiting = [i for i in ask if i not in mine and any(
        q.get("id") == i for q in d["queue"])]
    return {"accepted": accepted, "results": results, "waiting": waiting}


def pending() -> list[dict]:
    """Requests waiting to be applied, oldest tap first."""
    return sorted(_read(strict=False)["queue"], key=lambda o: (o.get("at") or "", o.get("got") or ""))


def needs_backup() -> bool:
    return not _read(strict=False).get("backup")


def settle(results: list[dict], backup: str | None = None) -> None:
    """The supervisor's half, after the database is committed: move each
    applied request from the queue to the done list. A crash before this
    runs only means the requests are applied again - each is a state, so
    the second time changes nothing."""
    with phonecfg._locked(lock=path().with_name(path().name + ".lock")):
        d = _read(strict=True)
        ids = {r["id"] for r in results}
        by_id = {o.get("id"): o for o in d["queue"]}
        d["queue"] = [o for o in d["queue"] if o.get("id") not in ids]
        for r in results:
            q = by_id.get(r["id"], {})
            d["done"].append({"id": r["id"], "k": q.get("k", r.get("k")), "on": q.get("on"),
                              "at": q.get("at"), "dev": q.get("dev"), "name": q.get("name"),
                              "r": r["r"], "key": r.get("key"), "note": r.get("note"),
                              "why": r.get("why"), "decided": r["decided"]})
        cut = _fmt(_now() - timedelta(days=KEEP_DAYS))
        d["done"] = [o for o in d["done"] if (o.get("decided") or "") >= cut][-KEEP_DONE:]
        if backup and not d.get("backup"):
            d["backup"] = backup
        _write(d)


def recent(n: int = 8) -> list[dict]:
    """The latest outcomes, newest first - for the laptop's Phone page."""
    d = _read(strict=False)
    return [{k: o.get(k) for k in ("k", "key", "on", "at", "name", "r", "note", "decided")}
            for o in reversed(d["done"][-n:])]


def waiting_count() -> int:
    return len(_read(strict=False)["queue"])
