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
"""
from __future__ import annotations

import gzip
import hashlib
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

KEEP_SNAPSHOTS = 2
TICK = 4.0                     # seconds between checks for a changed database
SETTLE = 2.5                   # ...and how long it must stay unchanged first
STARTUP_NAME = "VPA phone sync.lnk"

_COLS_V = ["k", "src", "id", "url", "make", "model", "variant", "year", "body",
           "type", "state", "seller", "title", "first", "last"]
_COLS_S = ["v", "d", "p", "km"]
_COLS_F = ["name", "make", "model", "variant", "method", "n", "n_months", "level",
           "direction", "m1", "f1", "lo1", "hi1", "m2", "f2", "lo2", "hi2",
           "m3", "f3", "lo3", "hi3", "note", "km_fit", "km_pct_10k", "km_median",
           "km_slope"]


# ------------------------------------------------------------------ building
def _starred(db_path) -> list:
    from .store import connect_readonly
    conn = connect_readonly(db_path)
    try:
        return [[r["key"], r["starred_at"], r["note"]] for r in conn.execute(
            "SELECT key, starred_at, note FROM starred ORDER BY starred_at")]
    finally:
        conn.close()


def _code_sig() -> str:
    from .viewer_xlsx import _code_signature
    h = hashlib.sha1(_code_signature().encode())
    h.update(Path(__file__).read_bytes())
    return h.hexdigest()[:12]


read_meta = phonecfg.read_meta
snapshot_path = phonecfg.snapshot_path


def _num(v, nd=0):
    try:
        if v is None or v != v:          # None or NaN
            return None
        return round(float(v), nd) if nd else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _txt(v):
    return None if v is None or v != v or v == "" else str(v)


def build_snapshot(db_path=DEFAULT_DB, force: bool = False, progress=None) -> dict:
    """Bring the phone copy up to date. Returns its meta, with "built": True
    if a new snapshot was written and False if the current one already
    matched the data. Reads the database read-only; writes only under
    output/phone/."""
    from . import links, viewer_xlsx
    from .store import read_frame

    t0 = time.time()
    raw = read_frame(db_path)
    starred = _starred(db_path)
    data_fp = viewer_xlsx._fingerprint(raw) if not raw.empty else "empty"
    vh = hashlib.sha1(f"{phonecfg.FORMAT}|{data_fp}|{_code_sig()}|".encode())
    vh.update(json.dumps(starred).encode())
    version = vh.hexdigest()[:16]
    old = read_meta()
    if not force and old and old.get("version") == version \
            and snapshot_path(old).is_file():
        return dict(old, built=False)

    # --- forecasts and mileage fits, reusing unchanged ones
    fc_rows = []
    if not raw.empty:
        from . import analyze
        df = viewer_xlsx.analysis_frame(raw)
        entries = viewer_xlsx._forecast_entries(df)
        phonecfg.OUT.mkdir(parents=True, exist_ok=True)
        cache = viewer_xlsx._FcCache(None, path=phonecfg.OUT / "forecast_cache.json")
        todo = [e for e in entries if not cache.has(e[2])]
        done = 0
        for name, sub, digest in entries:
            if cache.has(digest):
                pair = cache.get(digest)
            else:
                row = viewer_xlsx._forecast_row(name, sub)
                fit = analyze.fit_mileage(analyze.prep(sub)) if row else {}
                pair = [row, {k: viewer_xlsx._plain(fit.get(k)) for k in
                              ("usable", "pct_per_10k_km", "median_odo", "slope_per_km")}]
                cache.put(digest, pair)
                done += 1
                if progress:
                    progress(done / max(1, len(todo)), f"Forecasts {done} of {len(todo)}")
            row, fit = pair
            if row is None:
                continue
            first = sub.iloc[0]
            is_line = name == str(first["model_line"])
            fc_rows.append(
                [name, _txt(first["make"]), _txt(first["model"]),
                 None if is_line else _txt(first["variant"])]
                + [row[1], row[2], row[3], _num(row[4]), row[5]]
                + [row[6], _num(row[7]), _num(row[8]), _num(row[9]),
                   row[10], _num(row[11]), _num(row[12]), _num(row[13]),
                   row[14], _num(row[15]), _num(row[16]), _num(row[17])]
                + [row[18] or None, bool(fit.get("usable")),
                   _num(fit.get("pct_per_10k_km"), 2), _num(fit.get("median_odo")),
                   fit.get("slope_per_km") if fit.get("usable") else None])
        cache.save()

    # --- cars: identity from each car's latest sighting; sightings by car
    vehicles, sightings, index = [], [], {}
    if not raw.empty:
        r = raw.sort_values(["key", "capture_date"], kind="stable")
        last = r.groupby("key", sort=False).tail(1)
        for row in last.itertuples(index=False):
            index[row.key] = len(vehicles)
            vehicles.append([
                row.key, _txt(row.source), _txt(row.listing_id),
                links.ad_url(row.url, row.source) if isinstance(row.url, str) else None,
                _txt(row.make), _txt(row.model), _txt(row.variant), _num(row.year),
                _txt(row.body_type), _txt(row.vehicle_type), _txt(row.state),
                _txt(row.seller_type), _txt(row.raw_title),
                _txt(row.first_seen), _txt(row.last_seen)])
        for row in r.itertuples(index=False):
            sightings.append([index[row.key], row.capture_date.strftime("%Y-%m-%d"),
                              _num(row.price_aud), _num(row.odometer_km)])

    built = datetime.now().isoformat(timespec="seconds")
    days = sorted({s[1] for s in sightings})
    counts = {"vehicles": len(vehicles), "sightings": len(sightings),
              "priced": sum(1 for s in sightings if s[2] is not None),
              "forecasts": len(fc_rows), "starred": len(starred),
              "first_day": days[0] if days else None,
              "last_day": days[-1] if days else None}
    snap = {"format": phonecfg.FORMAT, "version": version, "built": built,
            "data_fp": data_fp, "counts": counts,
            "vehicles": {"cols": _COLS_V, "rows": vehicles},
            "sightings": {"cols": _COLS_S, "rows": sightings},
            "forecasts": {"cols": _COLS_F, "rows": fc_rows, "horizon_months": 3,
                          "note": "mileage-adjusted to each line's median odometer "
                                  "when km_fit is true"},
            "starred": {"cols": ["k", "at", "note"], "rows": starred}}
    body = gzip.compress(json.dumps(snap, separators=(",", ":")).encode("utf-8"),
                         compresslevel=9, mtime=0)

    out = phonecfg.OUT
    out.mkdir(parents=True, exist_ok=True)
    name = f"snapshot-{version}.json.gz"
    tmp = out / f"{name}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_bytes(body)
    os.replace(tmp, out / name)
    meta = {"format": phonecfg.FORMAT, "version": version, "built": built,
            "file": name, "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(), "data_fp": data_fp,
            "counts": counts, "seconds": round(time.time() - t0, 2)}
    tmp = out / f"meta.json.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(tmp, out / "meta.json")
    # keep the newest few, never the one just named
    olds = sorted(out.glob("snapshot-*.json.gz"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    for p in olds[KEEP_SNAPSHOTS:]:
        if p.name != name:
            try:
                p.unlink()
            except OSError:
                pass
    return dict(meta, built_now=True, built=True)


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
    try:
        st = Path(db).stat()
        return (st.st_mtime_ns, st.st_size)
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


def _spawn_background():
    """Start the background listener (pythonw -m vpa phone-serve)."""
    if time.time() - HOST.last_spawn < 60:
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
            st = _db_stat(h.db)
            if st != h.last_stat:
                h.last_stat, h.changed_at = st, time.time()
            elif h.changed_at is not None and time.time() - h.changed_at >= SETTLE:
                h.changed_at = None
                _rebuild("data changed" if h.last_build else "start")
    except Exception as e:
        h.status, h.error = "error", f"{type(e).__name__}: {e}"
        traceback.print_exc()


def _loop():
    while not HOST.stop_evt.wait(TICK if HOST.server is not None else TICK * 1.5):
        supervise()
        if HOST.role == "service":
            s = phonecfg.load()
            if not (s["enabled"] and s["background"]):
                print("background phone sync switched off - exiting")
                _unlisten()
                return


def start(db_path=DEFAULT_DB, role: str = "app") -> None:
    """Begin supervising in this process (a daemon thread). Idempotent."""
    with HOST.lock:
        if HOST.thread is not None and HOST.thread.is_alive():
            return
        HOST.db, HOST.role = str(db_path), role
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


def apply_options(db_path, enabled=None, lan=None, background=None) -> dict:
    """The laptop page's switches. Background on/off also adds/removes the
    Startup shortcut; the change takes effect on the next tick (seconds)."""
    if background is not None:
        err = set_autostart(bool(background) and (enabled is not False)
                            and (enabled or phonecfg.load()["enabled"]))
        if err:
            return {"ok": False, "error": err}
    s = phonecfg.set_options(enabled=enabled, lan=lan, background=background)
    if not s["enabled"]:
        set_autostart(False)
    if HOST.db is None:
        HOST.db = str(db_path)
    if HOST.role is not None:
        supervise(allow_build=False)
    return {"ok": True, **ui_state(db_path)}


def ui_state(db_path=DEFAULT_DB) -> dict:
    s = phonecfg.load()
    pub = phonecfg.public(s)
    meta = read_meta()
    port = int(s["port"])
    status = HOST.status
    if HOST.role is None:                       # this process is not supervising
        status = ("elsewhere" if _ping(port) else "starting") if s["enabled"] else "off"
    return {"ok": True, **pub, "status": status, "error": HOST.error,
            "here": HOST.server is not None, "role": HOST.role,
            "autostart": (startup_folder() / STARTUP_NAME).exists(),
            "meta": meta, "build": HOST.last_build, "building": HOST.building,
            "lan_urls": [f"http://{ip}:{port}" for ip in lan_addresses()] if s["lan"] else [],
            "local_url": f"http://127.0.0.1:{port}",
            "format": phonecfg.FORMAT}
