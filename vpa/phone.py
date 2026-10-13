"""Phone sync, laptop side: the phone copy, and keeping it current.

The phone carries its own read-only copy of the data (a "snapshot") so it can
be searched at a car yard with no signal and the laptop asleep. The laptop's
job is:

  1. build that copy - every car, every sighting, the forecasts and mileage
     fits - from the database, READ-ONLY (store.read_frame: SQLite itself
     refuses writes on that connection);
  2. rebuild it whenever the data changes (any import, undo, edit or merge,
     from the app, the watch folder or the command line), within seconds;
  3. hand it to paired phones when they ask (phonesrv.py), and only when it
     changed: the phone sends the version it holds and gets "no change" back.

Files, all in output/phone/ (regenerable - deleting the folder is safe):

    snapshot-<version>.json.gz   the copy; the newest two are kept, so a phone
                                 mid-download when a rebuild lands still gets
                                 a whole file
    meta.json                    which snapshot is current, its counts, size
                                 and hash - written AFTER the snapshot, so it
                                 never names a file that is not complete
    forecast_cache.json          forecasts reused between builds (as the
                                 workbook does with viewer_cache.json)

The version is a hash of the data, the starred list, the snapshot layout and
the analysis code, so it changes exactly when what the phone would show
changes - and a build with nothing new writes nothing at all.

Who runs the listener: the app while it is open, or - with "keep running
when VPA is closed" - a background process started with Windows
(python -m vpa phone-serve). supervise() decides, in whichever process calls
it, so there is only ever one.

Stars from phones: the listener queues them in phoneinbox.py without
touching the database; the same supervising process applies them
(starred.apply_phone_ops - the starred table only) on its next tick, and the
database change rebuilds the copy as any other change does.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from . import phonecfg
from .store import DEFAULT_DB

TICK = 4.0                     # seconds between checks for a changed database
SETTLE = 2.5                   # ...and how long it must stay unchanged first
BUILD_TIMEOUT = 3600           # a first build of a large database takes minutes
STARTUP_NAME = "VPA phone sync.lnk"

read_meta = phonecfg.read_meta
snapshot_path = phonecfg.snapshot_path


# ------------------------------------------------------------------ building
def build_snapshot(db_path=DEFAULT_DB, force: bool = False) -> dict:
    """Bring the phone copy up to date by running phonebuild in a CHILD
    process (python -m vpa phone-copy). Separate because the build runs every
    selection through the page code with the analysis frame cached in place -
    a patch that must never be visible to the app's own requests - and
    because minutes of pandas work belong outside the app's process.
    Returns the meta, with "built" True if a new copy was written."""
    cmd = [sys.executable, "-m", "vpa", "--db", str(db_path), "phone-copy",
           "--out", str(phonecfg.OUT)] + (["--force"] if force else [])
    r = subprocess.run(cmd, cwd=str(phonecfg.ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=BUILD_TIMEOUT,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for line in (r.stdout or "").splitlines():
        print(line)
    if r.returncode == 3:
        return dict(read_meta() or {}, built=False, busy=True)
    if r.returncode != 0:
        tail = [x for x in (r.stderr or "").strip().splitlines() if x.strip()][-1:]
        print(r.stderr or "")
        raise RuntimeError(tail[0] if tail else f"phone-copy exited {r.returncode}")
    m = read_meta()
    if m is None:
        raise RuntimeError("phone-copy finished but wrote no meta.json")
    return dict(m, built="copy written" in (r.stdout or ""))


# ------------------------------------------------------------------ running
class _Host:
    """What this process is doing for phone sync: listening (or not), and
    the last build. One per process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.server = None            # phonesrv server while listening here
        self.bound = None             # (lan, port) it listens with
        self.role = None              # "app" | "service"
        self.db = None
        self.status = "off"           # off | here | elsewhere | blocked | error
        self.error = None
        self.last_stat = None
        self.changed_at = None
        self.building = False
        self.build_lock = threading.Lock()
        self.last_build = None        # {"at", "seconds", "built", "error"}
        self.last_spawn = 0.0
        self.stamp = None             # code version this process started on
        self.star_error = None        # the last failure applying phone stars
        self.thread = None
        self.stop_evt = threading.Event()


HOST = _Host()


def _ping(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1.0) as r:
            d = json.loads(r.read().decode())
            return d if d.get("app") == phonecfg.APP_ID else None
    except Exception:
        return None


def _db_stat(db):
    """What a rebuild is triggered by: the database, and the photo list
    beside it (a photo backfill changes the copy with no database write)."""
    from . import details, photos
    try:
        st = Path(db).stat()
        return (st.st_mtime_ns, st.st_size, photos.stat(db), details.stat(db))
    except OSError:
        return None


def _rebuild(reason: str, force=False):
    h = HOST
    if not h.build_lock.acquire(blocking=False):
        return                                   # one build at a time
    h.building = True
    t0 = time.time()
    try:
        m = build_snapshot(h.db, force=force)
        h.last_build = {"at": datetime.now().isoformat(timespec="seconds"),
                        "seconds": round(time.time() - t0, 2),
                        "built": bool(m.get("built")), "reason": reason,
                        "error": None}
        if m.get("built"):
            print(f"phone copy rebuilt ({reason}): {m['counts']['vehicles']} cars, "
                  f"{m['bytes'] // 1024} KB, {m['seconds']} s")
    except Exception as e:
        traceback.print_exc()
        h.last_build = {"at": datetime.now().isoformat(timespec="seconds"),
                        "seconds": round(time.time() - t0, 2), "built": False,
                        "reason": reason, "error": f"{type(e).__name__}: {e}"}
    finally:
        h.building = False
        h.build_lock.release()


def _listen(lan: bool, port: int) -> str | None:
    """Start the listener here. Returns an error, or None."""
    from . import phonesrv
    try:
        HOST.server = phonesrv.start(port, lan)
        HOST.bound = (lan, port)
        return None
    except OSError as e:
        HOST.server, HOST.bound = None, None
        return str(e)


def _unlisten():
    if HOST.server is not None:
        try:
            HOST.server.shutdown()
            HOST.server.server_close()
        except Exception:
            pass
    HOST.server, HOST.bound = None, None


def _spawn_background(force: bool = False):
    """Start the background listener (pythonw -m vpa phone-serve)."""
    if not force and time.time() - HOST.last_spawn < 60:
        return
    HOST.last_spawn = time.time()
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    try:
        subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "vpa", "--db", str(HOST.db),
                          "phone-serve"], cwd=str(phonecfg.ROOT),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as e:
        HOST.error = f"could not start the background listener: {e}"


def supervise(allow_build: bool = True) -> None:
    """One tick: listen here or not, start the background listener if it
    should be running and is not, and rebuild the copy if the data changed.
    Never raises - phone sync must not be able to take the app down.
    Builds happen only on the supervising thread (allow_build), never inside
    a request from the laptop page."""
    h = HOST
    try:
        s = phonecfg.load()
        here_wanted = s["enabled"] and ((h.role == "service") == s["background"])
        want = (s["lan"], int(s["port"]))
        if h.server is not None and (not here_wanted or h.bound != want):
            _unlisten()
        if here_wanted and h.server is None:
            other = _ping(want[1])
            if other:
                h.status, h.error = "elsewhere", None
            else:
                err = _listen(*want)
                h.status, h.error = ("here", None) if err is None else \
                    ("blocked", f"port {want[1]} is in use by another program ({err})")
        elif not here_wanted:
            if not s["enabled"]:
                h.status, h.error = "off", None
            else:                                  # the other process's job
                h.status = "elsewhere" if _ping(want[1]) else "starting"
                if h.role == "app" and s["background"] and h.status == "starting":
                    _spawn_background()
        if h.server is not None and allow_build:
            _apply_phone_stars()
            _note_https(s)
            st = _db_stat(h.db)
            if st != h.last_stat:
                h.last_stat, h.changed_at = st, time.time()
            elif h.changed_at is not None and time.time() - h.changed_at >= SETTLE:
                h.changed_at = None
                _rebuild("data changed" if h.last_build else "start")
    except Exception as e:
        h.status, h.error = "error", f"{type(e).__name__}: {e}"
        traceback.print_exc()


def _apply_phone_stars() -> None:
    """Apply what phones asked for since the last tick. Runs only where the
    listener runs, on the supervising thread. On any failure the requests
    stay queued and are tried again next tick - nothing is lost or
    half-done (the database part is one transaction, and each request is a
    state, so applying it twice is harmless)."""
    from . import phoneinbox, starred
    from .store import Store
    h = HOST
    try:
        ops = phoneinbox.pending()
        if not ops:
            return
        backup = None
        if phoneinbox.needs_backup():
            # the first change a phone has ever made: a new kind of write
            with Store(h.db) as st:
                backup = str(st.backup())
            print(f"phone stars: backed up the database first -> {backup}")
        res = starred.apply_phone_ops(h.db, ops)
        phoneinbox.settle(res, backup=backup)
        h.star_error = None
        tally = {}
        for r in res:
            tally[r["r"]] = tally.get(r["r"], 0) + 1
        print(f"{time.strftime('%H:%M:%S')} phone stars: "
              + ", ".join(f"{n} {k}" for k, n in sorted(tally.items())))
    except Exception as e:
        h.star_error = f"{type(e).__name__}: {e}"
        traceback.print_exc()


def _note_https(s: dict) -> None:
    """Keep settings["https_url"] = the Tailscale address while Serve points
    at this listener (None otherwise), so the listener can tell a phone on
    the plain-http address where the secure one is. Written only on change."""
    from . import tailnet
    t = tailnet.check(int(s["port"]))
    url = t.get("https_url") if t.get("state") == "ready" else None
    if url != s.get("https_url"):
        try:
            phonecfg.update(lambda st: st.__setitem__("https_url", url))
        except TimeoutError:
            pass


def _code_stamp() -> int:
    """Newest modification time across VPA's own code. The background
    listener runs for days; when an update changes this, it hands over to a
    fresh copy of itself, so it never keeps serving yesterday's code."""
    pkg = Path(__file__).resolve().parent
    try:
        return max((p.stat().st_mtime_ns for p in pkg.rglob("*")
                    if p.suffix in (".py", ".js", ".html", ".css")), default=0)
    except OSError:
        return HOST.stamp or 0


def _respawn():
    """Stop listening FIRST (so the new copy finds the port free and does
    not mistake this one for "already running"), then start the new copy."""
    _unlisten()
    _spawn_background(force=True)


def _loop():
    while not HOST.stop_evt.wait(TICK if HOST.server is not None else TICK * 1.5):
        supervise()
        if HOST.role == "service":
            s = phonecfg.load()
            if not (s["enabled"] and s["background"]):
                print("background phone sync switched off - exiting")
                _unlisten()
                return
            if _code_stamp() != HOST.stamp:
                print("VPA was updated - restarting the background listener on the new code")
                _respawn()
                return


def start(db_path=DEFAULT_DB, role: str = "app") -> None:
    """Begin supervising in this process (a daemon thread). Idempotent."""
    with HOST.lock:
        if HOST.thread is not None and HOST.thread.is_alive():
            return
        HOST.db, HOST.role = str(db_path), role
        HOST.stamp = _code_stamp()
        HOST.stop_evt.clear()
        supervise()
        HOST.thread = threading.Thread(target=_loop, name="vpa-phone", daemon=True)
        HOST.thread.start()


def stop() -> None:
    HOST.stop_evt.set()
    _unlisten()


def rebuild_now(db_path=DEFAULT_DB) -> dict:
    """The laptop page's Rebuild button. Builds in this process whoever is
    listening - the file is what is served, so any process may refresh it."""
    if HOST.db is None:
        HOST.db = str(db_path)
    _rebuild("rebuild button", force=True)
    return {"ok": not (HOST.last_build or {}).get("error"),
            "build": HOST.last_build, "meta": read_meta()}


def serve_forever(db_path=DEFAULT_DB) -> int:
    """python -m vpa phone-serve: the background listener."""
    s = phonecfg.load()
    if not (s["enabled"] and s["background"]):
        print("phone sync is not set to run in the background - nothing to do")
        return 0
    if _ping(int(s["port"])):
        print("phone sync is already running")
        return 0
    start(db_path, role="service")
    print(f"phone sync listening on port {s['port']} (lan: {s['lan']})")
    try:
        while HOST.thread.is_alive():
            HOST.thread.join(1.0)
    except KeyboardInterrupt:
        pass
    stop()
    return 0


# ------------------------------------------------------------------ the page
def lan_addresses() -> list[str]:
    """This laptop's IPv4 addresses a phone could reach (no loopback)."""
    ips = set()
    try:                                   # the address of the default route;
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # sends nothing
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / \
        "Start Menu" / "Programs" / "Startup"


def set_autostart(on: bool) -> str | None:
    """Add or remove the Startup shortcut for the background listener.
    Returns an error, or None."""
    lnk = startup_folder() / STARTUP_NAME
    if not on:
        try:
            lnk.unlink(missing_ok=True)
            return None
        except OSError as e:
            return str(e)
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    ico = phonecfg.ROOT / "assets" / "vpa.ico"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{pyw if pyw.exists() else exe}';"
          f"$s.Arguments='-m vpa phone-serve';"
          f"$s.WorkingDirectory='{phonecfg.ROOT}';"
          f"$s.IconLocation='{ico},0';"
          f"$s.Description='VPA - lets your phone sync while the app is closed';$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return None
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not add the Startup shortcut: {e}"


def apply_options(db_path, enabled=None, lan=None, background=None,
                  phone_stars=None) -> dict:
    """The laptop page's switches. Background on/off also adds/removes the
    Startup shortcut; the change takes effect on the next tick (seconds)."""
    if background is not None:
        err = set_autostart(bool(background) and (enabled is not False)
                            and (enabled or phonecfg.load()["enabled"]))
        if err:
            return {"ok": False, "error": err}
    s = phonecfg.set_options(enabled=enabled, lan=lan, background=background,
                             phone_stars=phone_stars)
    if not s["enabled"]:
        set_autostart(False)
    if HOST.db is None:
        HOST.db = str(db_path)
    if HOST.role is not None:
        supervise(allow_build=False)
    return {"ok": True, **ui_state(db_path)}


def _named(db_path, rows: list[dict]) -> list[dict]:
    """Give each logged phone star the car's name (read-only lookup)."""
    from .store import connect_readonly
    ks = {r.get("key") or r.get("k") for r in rows}
    names = {}
    if ks:
        try:
            con = connect_readonly(db_path)
            try:
                for k in ks:
                    v = con.execute("SELECT raw_title, year, make, model, variant FROM vehicles "
                                    "WHERE key = ?", (k,)).fetchone()
                    if v:
                        names[k] = v["raw_title"] or " ".join(
                            str(x) for x in (v["year"], v["make"], v["model"], v["variant"]) if x)
            finally:
                con.close()
        except Exception:
            pass
    return [dict(r, title=names.get(r.get("key") or r.get("k"))) for r in rows]


def ui_state(db_path=DEFAULT_DB) -> dict:
    s = phonecfg.load()
    pub = phonecfg.public(s)
    meta = read_meta()
    port = int(s["port"])
    status = HOST.status
    if HOST.role is None:                       # this process is not supervising
        status = ("elsewhere" if _ping(port) else "starting") if s["enabled"] else "off"
    from . import phoneinbox, tailnet
    return {"ok": True, **pub, "status": status, "error": HOST.error,
            "tailnet": tailnet.check(port),
            "here": HOST.server is not None, "role": HOST.role,
            "autostart": (startup_folder() / STARTUP_NAME).exists(),
            "meta": meta, "build": HOST.last_build, "building": HOST.building,
            "lan_urls": [f"http://{ip}:{port}" for ip in lan_addresses()] if s["lan"] else [],
            "local_url": f"http://127.0.0.1:{port}",
            "stars": {"recent": _named(db_path, phoneinbox.recent()),
                      "waiting": phoneinbox.waiting_count(), "error": HOST.star_error},
            "format": phonecfg.FORMAT}
