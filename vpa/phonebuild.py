"""Build the phone copy. Runs in ITS OWN PROCESS (python -m vpa phone-copy),
started by phone.py whenever the data changed.

The phone shows the same numbers as the laptop because they ARE the laptop's
numbers: every make, model line and trim is run through the laptop's own
Explore (explore.detail_payload) and Compare (compare._one) code, and the
dataset overview through importui.overview_payload. The phone then only adds
what is plain counting over the sightings it carries (the car lists, scatter
points, ranks) - tests/check_phone_app.py checks that arithmetic against the
Python it mirrors.

Database safety:
  * the real database is opened only through store.connect_readonly (SQLite
    refuses writes on it), and only to copy it - with SQLite's backup API,
    which yields a consistent snapshot even mid-import - into a private
    work file in output/phone/. Every page function then runs on that copy.
  * to run ~800 selections in minutes rather than an hour, this process
    caches the analysis frame in place of re-reading it per call (it patches
    explore._frame and friends). That is why it is a separate process: the
    patch can never be seen by the running app.

Speed: each selection's result is cached (sel_cache.json) under a hash of
exactly the rows it was computed from plus the analysis code, so after an
import only the selections that import touched are recomputed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from . import phonecfg
from .phonecfg import FORMAT
KEEP_SNAPSHOTS = 2
_CODE_FILES = ("explore.py", "compare.py", "turnover.py", "analyze.py", "forecast.py",
               "dupes.py", "importui.py", "segments.py", "links.py", "phonebuild.py",
               "photos.py", "trims.py", "store.py")
_COLS_V = ["k", "src", "id", "url", "make", "model", "variant", "year", "body",
           "type", "state", "seller", "title", "first", "last", "u0", "ph", "gear"]
_COLS_S = ["v", "d", "p", "km"]


def _say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _code_sig() -> str:
    from importlib import metadata
    here = Path(__file__).resolve().parent
    h = hashlib.sha1(FORMAT.encode())
    for f in _CODE_FILES:
        h.update((here / f).read_bytes())
    for pkg in ("pandas", "numpy", "scipy", "statsmodels"):
        try:
            h.update(f"{pkg}={metadata.version(pkg)}".encode())
        except metadata.PackageNotFoundError:
            pass
    return h.hexdigest()[:16]


def _rows_hash(df) -> str:
    import pandas as pd
    return hashlib.sha1(pd.util.hash_pandas_object(df, index=False).values.tobytes()
                        ).hexdigest()


def _jsonable(o):
    """numpy / pandas scalars -> plain Python, NaN -> None, recursively."""
    import math
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            o = o.item()
        except (ValueError, AttributeError):
            return str(o)
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    return str(o)


def _slim_curve(c):
    """The drawn x/y grid is recomputed on the phone from mu, sigma, lo and
    hi with the same formula (analyze.price_curve), so it is not shipped."""
    if not isinstance(c, dict):
        return c
    return {k: v for k, v in c.items() if k not in ("x", "y")}


def _slim_detail(d: dict, level: str) -> dict:
    """detail_payload minus what the phone rebuilds from sightings (cars,
    movers, scatter, standouts) and, below line level, minus the line's
    sibling tables (the phone reads them from the line)."""
    drop = {"vehicles", "movers", "scatter", "standouts", "starred_total", "picked"}
    if level == "trim":
        drop |= {"siblings", "trim_churn"}
    out = {k: v for k, v in d.items() if k not in drop}
    out["curve"] = _slim_curve(out.get("curve"))
    out["adj_curve"] = _slim_curve(out.get("adj_curve"))
    ch = out.get("churn")
    if isinstance(ch, dict):
        ch = {k: v for k, v in ch.items() if k != "daily"}   # merged into byday
        if isinstance(ch.get("departed"), list):
            ch["departed"] = ch["departed"][:30]
        out["churn"] = ch
    return out


def _slim_compare(c: dict) -> dict:
    out = {k: v for k, v in c.items() if k not in ("scatter",)}
    out["curve"] = _slim_curve(out.get("curve"))
    return out


def _fit_tuple(f: dict) -> list:
    """analyze.fit_mileage's answer, compact: [usable, slope, median_odo, n,
    pct_per_10k, reason]."""
    return [bool(f.get("usable")), f.get("slope_per_km"), f.get("median_odo"),
            int(f.get("n", 0)), f.get("pct_per_10k_km"), f.get("reason", "")]


def _card(sub) -> dict:
    """What the phone's car card needs that is not counting: the trim's
    mileage fit (for a car you type in), the fit with EACH stored car left
    out (car_payload never lets a car bend the curve that judges it), and the
    trim's data caveats."""
    from . import analyze
    prepped = analyze.prep(sub, freq="MS")
    pool = analyze.latest_per_vehicle(prepped)
    cars = {}
    for key in pool["key"].astype(str):
        cars[key] = _fit_tuple(analyze.fit_mileage(prepped[prepped["key"] != key]))
    return {"fit": _fit_tuple(analyze.fit_mileage(prepped)), "cars": cars,
            "caveats": list(analyze.data_caveats(prepped, analyze.bucket_stats(prepped)))}


class _Cache:
    def __init__(self, path: Path, sig: str):
        self.path, self.sig = path, sig
        self.old, self.rows = {}, {}
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("sig") == sig:
                self.old = d.get("rows") or {}
        except (OSError, ValueError, AttributeError):
            pass

    def get(self, digest):
        hit = self.rows.get(digest) or self.old.get(digest)
        if hit is not None:
            self.rows[digest] = hit
        return hit

    def put(self, digest, value):
        self.rows[digest] = value

    def save(self):
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"sig": self.sig, "rows": self.rows},
                                      separators=(",", ":")), encoding="utf-8")
            phonecfg._replace(tmp, self.path)
        except OSError:
            pass


def _private_copy(db_path, out: Path) -> Path:
    """A consistent copy of the database, read through a read-only
    connection. SQLite's backup API copies a single consistent state even if
    an import is being written at that moment."""
    from .store import connect_readonly
    work = out / f"work-{os.getpid()}.db"
    src = connect_readonly(db_path)
    try:
        dst = sqlite3.connect(work)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return work


def _memoize(work: Path):
    """This process only: compute the analysis frame once. Every page
    function re-reads it per call, which across ~800 selections is most of
    the build. Returned copies, so no call can disturb another."""
    from . import compare, dupes, explore, starred
    frame = explore._frame(work)
    stars = starred.keys(work)
    dismissed = dupes.dismissed_sets(work)
    explore._frame = lambda db_path: frame.copy()
    compare._frame = explore._frame
    starred.keys = lambda db_path=None: set(stars)
    dupes.dismissed_sets = lambda db_path=None: list(dismissed)
    return frame, stars, dismissed


def _acquire(out: Path):
    lock = out / "build.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > 1800:     # a crashed build
                lock.unlink()
                return _acquire(out)
        except OSError:
            pass
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock


def build(db_path, out: Path, force: bool = False) -> dict:
    """Bring out/ up to date with the database. Returns the meta, with
    "built" False when it already matched (nothing is written then)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    lock = _acquire(out)
    if lock is None:
        return {"busy": True, "built": False}
    try:
        return _build(db_path, out, force)
    finally:
        for p in out.glob(f"work-{os.getpid()}.db*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            lock.unlink()
        except OSError:
            pass


def _build(db_path, out: Path, force: bool) -> dict:
    t0 = time.time()
    work = _private_copy(db_path, out)
    from .store import Store
    from . import viewer_xlsx
    with Store(work) as st:
        raw = st.frame()
        starred_rows = [[r["key"], r["starred_at"], r["note"]] for r in st.conn.execute(
            "SELECT key, starred_at, note FROM starred ORDER BY starred_at")]
        dismissed_raw = sorted(r[0] for r in st.conn.execute(
            "SELECT keys FROM not_duplicates"))
    data_fp = viewer_xlsx._fingerprint(raw) if not raw.empty else "empty"
    sig = _code_sig()
    # photo addresses (data/photos.json beside the REAL database, read-only):
    # a backfill changes the copy even when no price did
    from . import photos
    PH = photos.load(db_path)
    vh = hashlib.sha1(f"{FORMAT}|{data_fp}|{sig}|".encode())
    vh.update(json.dumps([starred_rows, dismissed_raw]).encode())
    vh.update(json.dumps(PH, sort_keys=True).encode())
    version = vh.hexdigest()[:16]
    try:
        old = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if not force and old and old.get("version") == version \
            and (out / str(old.get("file"))).is_file():
        return dict(old, built=False)

    from . import compare, explore, importui, links
    frame, stars, dismissed = _memoize(work)
    salt = hashlib.sha1(f"{sig}|{json.dumps(dismissed_raw)}".encode()).hexdigest()
    cache = _Cache(out / "sel_cache.json", sig)

    # ---- every selection, through the laptop's own page code
    index = compare.selection_index(frame) if not frame.empty else {}
    sels, done, todo_t = {}, 0, time.time()
    order = sorted(index.items(), key=lambda kv: {"make": 0, "line": 1, "trim": 2}[kv[1][3]])
    for label, (mk, mo, va, level) in order:
        sub = explore._select(frame, mk, mo, va)
        digest = hashlib.sha1(f"{label}|{level}|{salt}|{_rows_hash(sub)}".encode()).hexdigest()
        entry = cache.get(digest)
        if entry is None:
            d = explore.detail_payload(work, mk, mo, va)
            if not d.get("empty") and (d.get("mileage") or {}).get("usable"):
                # the exact slope (the page shows only % per 10,000 km): the
                # phone places each car on the mileage-adjusted curve with it
                from . import analyze
                d["mileage"]["slope_per_km"] = analyze.fit_mileage(
                    analyze.prep(sub, freq="MS"))["slope_per_km"]
            entry = {"lv": level, "mk": mk, "mo": mo, "va": va,
                     "d": _jsonable(_slim_detail(d, level)),
                     "c": _jsonable(_slim_compare(compare._one(frame, label, (mk, mo, va, level), 3)))}
            if level == "trim":
                entry["k"] = _jsonable(_card(sub))
            cache.put(digest, entry)
            done += 1
            if done % 25 == 0:
                _say(f"  {done} selections computed ({time.time() - todo_t:.0f}s)")
        sels[label] = entry
    cache.save()
    if done:
        _say(f"{done} of {len(index)} selections recomputed, the rest reused")

    overview = {lv: _jsonable(importui.overview_payload(work, lv)) for lv in ("line", "trim")}
    picker = _jsonable(explore.picker_payload(work))

    # ---- cars and sightings (identity from each car's latest sighting)
    def num(v):
        return None if v is None or v != v else int(round(float(v)))

    def txt(v):
        return None if v is None or v != v or v == "" else str(v)

    vehicles, sightings, index_k = [], [], {}
    if not raw.empty:
        r = raw.sort_values(["key", "capture_date"], kind="stable")
        for row in r.groupby("key", sort=False).tail(1).itertuples(index=False):
            index_k[row.key] = len(vehicles)
            vehicles.append([
                row.key, txt(row.source), txt(row.listing_id),
                links.ad_url(row.url, row.source) if isinstance(row.url, str) else None,
                txt(row.make), txt(row.model), txt(row.variant), num(row.year),
                txt(row.body_type), txt(row.vehicle_type), txt(row.state),
                txt(row.seller_type), txt(row.raw_title), txt(row.first_seen),
                txt(row.last_seen),
                # the stored address, only where no advert link can be made
                # from it - link matching needs it, display does not
                txt(row.url) if isinstance(row.url, str)
                and not links.ad_url(row.url, row.source) else None,
                # its photo addresses; the phone asks the site's photo server
                # for one only when that car is on screen
                PH.get(photos.ad_id(row.listing_id if isinstance(row.listing_id, str) else None,
                                    row.url if isinstance(row.url, str) else None) or ""),
                txt(row.gearbox)])
        # sightings in the frame's own order (capture_date, key): the phone's
        # "last sighting wins" must break ties exactly as the laptop does
        for row in raw.itertuples(index=False):
            sightings.append([index_k[row.key], row.capture_date.strftime("%Y-%m-%d"),
                              num(row.price_aud), num(row.odometer_km)])

    days = sorted({s[1] for s in sightings})
    counts = {"vehicles": len(vehicles), "sightings": len(sightings),
              "priced": sum(1 for s in sightings if s[2] is not None),
              "selections": len(sels), "starred": len(starred_rows),
              "first_day": days[0] if days else None,
              "last_day": days[-1] if days else None}
    built = datetime.now().isoformat(timespec="seconds")
    snap = {"format": FORMAT, "version": version, "built": built, "data_fp": data_fp,
            "counts": counts,
            "vehicles": {"cols": _COLS_V, "rows": vehicles},
            "sightings": {"cols": _COLS_S, "rows": sightings},
            "starred": {"cols": ["k", "at", "note"], "rows": starred_rows},
            "dismissed": [k.split(",") for k in dismissed_raw],
            "picker": picker, "overview": overview, "sel": sels,
            "cmp": {"rows": _jsonable(compare._row_meta()), "basis": compare._BASIS,
                    "max": compare.MAX_SELECTIONS},
            "consts": {k: getattr(explore, k) for k in (
                "CAR_MIN_PEERS", "CAR_PERCENTILE_MIN", "CAR_COMPARABLES",
                "CAR_SPLIT_MIN_N", "CAR_YEAR_MIN_N", "CAR_FIT_RANGE",
                "CAR_LINE_DISAGREE", "CAR_STALE_DAYS", "EST_NEAREST", "MAX_VEHICLES", "MAX_SCATTER",
                "MAX_STANDOUTS")}}
    body = gzip.compress(json.dumps(snap, separators=(",", ":")).encode("utf-8"),
                         compresslevel=9, mtime=0)

    name = f"snapshot-{version}.json.gz"
    tmp = out / f"{name}.{os.getpid()}.tmp"
    tmp.write_bytes(body)
    phonecfg._replace(tmp, out / name)
    meta = {"format": FORMAT, "version": version, "built": built, "file": name,
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "data_fp": data_fp, "counts": counts,
            "recomputed": done, "seconds": round(time.time() - t0, 2)}
    tmp = out / f"meta.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    phonecfg._replace(tmp, out / "meta.json")
    olds = sorted(out.glob("snapshot-*.json.gz"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    for p in olds[KEEP_SNAPSHOTS:]:
        if p.name != name:
            try:
                p.unlink()
            except OSError:
                pass
    for p in ("forecast_cache.json",):            # the format-1 cache, if any
        try:
            (out / p).unlink()
        except OSError:
            pass
    return dict(meta, built=True)


def main(db_path, out, force=False) -> int:
    m = build(db_path, out, force)
    if m.get("busy"):
        _say("another phone-copy build is running - not starting a second")
        return 3
    c = m["counts"]
    _say(f"phone copy {'written' if m['built'] else 'already up to date'}: "
         f"{c['vehicles']:,} cars, {c['sightings']:,} sightings, "
         f"{c['selections']} selections, {m['bytes'] // 1024} KB "
         f"(version {m['version']}, {m['seconds']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
