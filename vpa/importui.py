"""Import UI — a local, single-page front door for ingestion.

    python -m vpa import-ui          (or: python -m vpa.importui)

Flow: drop/pick a CSV/JSON/HTML file or paste listing text -> the EXISTING
adapter/normalisation code parses it (adapters.parse_file) -> a preview
table shows every normalised row with flags (no price, no odometer/year,
already in database, duplicate within the upload) -> untick or edit cells ->
Commit writes through the EXISTING Store.ingest(). Nothing touches the
database until Commit; edits are re-normalised by the same schema code the
CLI uses, so a hand-fixed "$18,990 neg" parses exactly as it would from a
file.

This module contains NO parsing, dedupe or schema logic of its own — it is
glue: temp file -> parse_file -> Store.preview -> (user) -> Store.ingest.
Serves on 127.0.0.1 only. Zero third-party dependencies (stdlib http.server).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .schema import make_record
from .adapters import parse_file
from .store import Store, DEFAULT_DB

DEFAULT_PORT = 8765
_SUFFIXES = {"csv": ".csv", "json": ".json", "html": ".html", "text": ".txt"}

# Build marker: newest mtime across the WHOLE package at import time. A
# long-running server keeps the code it started with (lazy imports are
# cached after first use, so quickadd/viewer_xlsx edits don't reach a live
# process either); when any vpa/*.py changed on disk, a new launch sees a
# different marker and replaces the stale instance instead of reusing it.
_BUILD = str(max(p.stat().st_mtime_ns
                 for p in Path(__file__).parent.rglob("*.py")))


# --------------------------------------------------------------------- api
def parse_payload(payload: dict, db_path: str | Path) -> dict:
    """Parse uploaded content and classify against the store. NO writes."""
    name = payload.get("name") or "pasted.txt"
    text = payload.get("text") or ""
    if not text.strip():
        return {"ok": False, "error": "no content - drop a file or paste text"}
    fmt = payload.get("format") or None
    source = payload.get("source") or None
    capture_date = payload.get("capture_date") or None

    # Before anything else: a bot-protection interstitial saved instead of the
    # results. It parses to zero rows and would otherwise be reported as an
    # empty page, which sends you looking at the parser rather than at the
    # file. Refused outright rather than previewed - there is nothing in it.
    from .adapters.challenge import explain as _challenge
    blocked = _challenge(text, name)
    if blocked:
        return {"ok": False, "error": blocked, "challenge": True}

    suffix = _SUFFIXES.get(fmt or "", Path(name).suffix or ".txt")
    tmp = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(text)
        tmp.close()
        records, used_fmt, raw_count = parse_file(
            tmp.name, source=source, fmt=fmt, capture_date=capture_date)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    with Store(db_path) as store:
        verdicts = store.preview(records)

    seen: set[tuple] = set()
    rows = []
    for rec, v in zip(records, verdicts):
        row = rec.to_row()
        flags, checked = [], True
        exact = (v["key"], rec.capture_date, rec.price_aud, rec.odometer_km)
        if v["status"] == "duplicate":
            flags.append("already in database (same date, price, odometer)")
            checked = False
        elif exact in seen:
            flags.append("duplicate within this upload")
            checked = False
        elif v["status"] == "new_observation":
            prior = v["prior_price"]
            if prior is not None and rec.price_aud is not None and prior != rec.price_aud:
                flags.append(f"known listing: price change A${prior:,.0f} -> "
                             f"A${rec.price_aud:,.0f}")
            else:
                flags.append("known listing (new sighting)")
        seen.add(exact)
        if rec.price_aud is None:
            flags.append("no usable price"
                         + (f" ({rec.price_note})" if rec.price_note else ""))
            checked = False
        if rec.year is None:
            flags.append("no year")
        if rec.odometer_km is None:
            flags.append("no odometer")
        rows.append({**row, "key": v["key"], "status": v["status"],
                     "flags": flags, "checked": checked})

    n_flagged = sum(1 for r in rows if r["flags"])
    n_dupe = sum(1 for r in rows if not r["checked"] and
                 any("duplicate" in f or "already" in f for f in r["flags"]))
    note = None
    if not rows and used_fmt == "html":
        note = ("This HTML file contains no listings. Facebook Marketplace "
                "saved pages are empty shells (the listings never reach the "
                "file) - on Facebook, select the results text (Ctrl+A, "
                "Ctrl+C) and use the paste box instead.")
    return {"ok": True, "note": note,
            "format": used_fmt, "raw_count": raw_count,
            "dropped": raw_count - len(rows), "rows": rows,
            "summary": {"parsed": len(rows), "flagged": n_flagged,
                        "duplicates": n_dupe,
                        "no_price": sum(1 for r in rows if r["price_aud"] is None),
                        "no_odometer": sum(1 for r in rows if r["odometer_km"] is None)}}


def commit_payload(payload: dict, db_path: str | Path) -> dict:
    """Re-normalise the (possibly edited) rows and write via Store.ingest."""
    rows = payload.get("rows") or []
    if not rows:
        return {"ok": False, "error": "no rows selected"}
    records, rejected = [], []
    today = date.today()
    for i, raw in enumerate(rows):
        source = str(raw.pop("source", None) or "other").casefold()
        rec = make_record(raw, source=source, capture_fallback=today)
        if rec is None:
            title = raw.get("raw_title") or f"row {i + 1}"
            rejected.append(f"{title}: make/model missing")
        else:
            records.append(rec)
    counts = {"rows_parsed": 0, "vehicles_new": 0, "obs_new": 0, "obs_duplicate": 0}
    if records:
        with Store(db_path) as store:
            counts = store.ingest(records, source_file="(import-ui)", adapter="ui")
    return {"ok": True, "committed": counts["obs_new"],
            "new_vehicles": counts["vehicles_new"],
            "duplicates_skipped": counts["obs_duplicate"],
            "rejected": rejected}


def _edit_row_payload(db_path: str | Path, key: str) -> dict:
    """One vehicle and all of its sightings, as the edit form needs them.

    Read-only. The form is populated from the database rather than from
    whatever the page happened to be showing, so you are always editing the
    row as it is now — not as it looked before another tab committed an
    import.
    """
    if not key:
        return {"ok": False, "error": "no vehicle named"}
    from .edit import OBSERVATION_FIELDS, VEHICLE_FIELDS
    with Store(db_path) as store:
        v = store.conn.execute(
            "SELECT * FROM vehicles WHERE key = ?", (key,)).fetchone()
        if v is None:
            return {"ok": False, "error": "that vehicle is not in the database"}
        obs = [dict(r) for r in store.conn.execute(
            "SELECT * FROM observations WHERE key = ? ORDER BY capture_date, id",
            (key,))]
        edits = [dict(r) for r in store.conn.execute(
            "SELECT * FROM edits WHERE target = ? OR target IN "
            "(SELECT CAST(id AS TEXT) FROM observations WHERE key = ?) "
            "ORDER BY id DESC LIMIT 20", (key, key))]
    return {"ok": True, "key": key, "vehicle": dict(v), "observations": obs,
            "vehicle_fields": list(VEHICLE_FIELDS),
            "observation_fields": list(OBSERVATION_FIELDS),
            "edits": edits}


def heatmap_payload(db_path: str | Path) -> dict:
    """Prices recorded per capture day - feeds the calendar heatmap."""
    with Store(db_path) as store:
        rows = store.conn.execute(
            "SELECT capture_date, COUNT(*) FROM observations "
            "WHERE price_aud IS NOT NULL GROUP BY capture_date").fetchall()
    return {"ok": True, "days": {r[0]: r[1] for r in rows}}


OVERVIEW_TOP_N = 8
_MIN_MOVER_PRICES = 3      # below this a "median" is one or two ads, not a market


def overview_payload(db_path: str | Path, level: str = "line",
                     top: int = OVERVIEW_TOP_N) -> dict:
    """Dataset overview: headline totals, the most-tracked models with a
    median-per-capture-day sparkline, biggest movers between the last two
    captures, and coverage warnings.

    Read-only. Grouping reuses add_segment_cols()'s model_line and the same
    sel_key rule as viewer_xlsx.generate(), so a name shown here is the same
    string you pick in the workbook - no second vocabulary.
    """
    from . import analyze
    from .segments import add_segment_cols

    with Store(db_path) as store:
        s = store.status()
        df = add_segment_cols(store.frame())

    totals = {"vehicles": s["vehicles"], "observations": s["observations"],
              "priced": s["priced_observations"], "ingests": s["ingests"],
              "sources": s["sources"], "first": None, "last": None,
              "capture_days": 0}
    if df.empty:
        return {"ok": True, "empty": True, "level": level, "totals": totals,
                "models": [], "movers": [], "thin": [], "health": {},
                "latest": None}

    df["day"] = df["capture_date"].dt.strftime("%Y-%m-%d")
    # identical rule to viewer_xlsx: trimless rows collapse to "-"
    df["sel_key"] = df.apply(
        lambda r: "-" if not isinstance(r["variant"], str) or not r["variant"].strip()
        else f"{r['model_line']} {r['variant']}", axis=1)

    all_days = sorted(df["day"].unique())
    totals.update(capture_days=len(all_days), first=all_days[0], last=all_days[-1])

    col = "model_line" if level != "trim" else "sel_key"
    priced = df[df["price_aud"].notna()]
    collided = analyze.collided_keys(df)
    if level == "trim":
        priced = priced[priced["sel_key"] != "-"]

    def series(frame):
        """[(day, median, n)] ordered by day, for one model."""
        g = (frame.groupby("day")["price_aud"].agg(["median", "count"])
             .reset_index().sort_values("day"))
        return [{"d": r["day"], "v": round(float(r["median"]), 2),
                 "n": int(r["count"])} for _, r in g.iterrows()]

    def like_for_like(sub, day_a: str, day_b: str):
        """Median change over vehicles present on BOTH days.

        Comparing raw medians across two captures is only meaningful when the
        same kind of stock was swept. A broad sweep that adds cheap trims
        drags the median down and reads as a crash: 'Ford Falcon' looks like
        -28% between 27/07 and 28/07 purely because 28/07 covered the whole
        range. Matching on `key` (the dedupe identity) compares the same cars
        with themselves. Returns (pct, n_matched) or (None, n_matched).
        """
        a = sub[sub["day"] == day_a].groupby("key")["price_aud"].median()
        b = sub[sub["day"] == day_b].groupby("key")["price_aud"].median()
        # a key holding two prices on one day is two cars merged, so its
        # "median" is a blend of different vehicles - drop it from the match
        common = a.index.intersection(b.index).difference(list(collided))
        if len(common) < _MIN_MOVER_PRICES:
            return None, len(common)
        before, after = float(a[common].median()), float(b[common].median())
        if not before:
            return None, len(common)
        return round((after - before) / before * 100, 1), len(common)

    models, movers = [], []
    for name, sub in priced.groupby(col):
        pts = series(sub)
        latest, first = pts[-1]["v"], pts[0]["v"]
        # step = latest capture vs the one before it, for THIS model (models
        # are not all captured on the same days - see NOTES bug 6b)
        def mixed(a: dict, b: dict) -> bool:
            """True when the listing count roughly doubled or halved between
            two captures - the pool changed, so a median comparison across
            them describes different stock rather than a price move."""
            lo_n, hi_n = sorted((a["n"], b["n"]))
            return hi_n >= 2 * max(lo_n, 1)

        step = matched = None
        n_matched = 0
        mix = mix_overall = False
        if len(pts) > 1:
            mix_overall = mixed(pts[0], pts[-1])
            if pts[-2]["v"]:
                step = round((latest - pts[-2]["v"]) / pts[-2]["v"] * 100, 1)
                matched, n_matched = like_for_like(sub, pts[-2]["d"], pts[-1]["d"])
                mix = mixed(pts[-2], pts[-1])
        # carry the parts so the page can deep-link to /explore. The name
        # string alone can't be split reliably - "BMW 4 Series" has a
        # two-word model, so whitespace splitting would mis-assign it.
        row0 = sub.iloc[0]
        entry = {"name": str(name),
                 "make": str(row0["make"]), "model": str(row0["model"]),
                 "variant": (str(row0["variant"]) if level == "trim"
                             and isinstance(row0["variant"], str) else ""),
                 "listings": int(len(sub)),
                 "days": len(pts), "latest": latest, "first": first,
                 "overall": round((latest - first) / first * 100, 1) if first else None,
                 "step": step, "matched": matched, "n_matched": n_matched,
                 "mix": mix, "mix_overall": mix_overall,
                 "last_day": pts[-1]["d"], "spark": pts,
                 "lo": round(float(sub["price_aud"].min()), 2),
                 "hi": round(float(sub["price_aud"].max()), 2)}
        models.append(entry)
        # a mover needs a real reading on both sides; prefer the matched
        # figure and fall back to the raw step only when the mix held steady
        shown = matched if matched is not None else (None if mix else step)
        if shown is not None and pts[-1]["n"] >= _MIN_MOVER_PRICES \
                and pts[-2]["n"] >= _MIN_MOVER_PRICES:
            movers.append({"name": entry["name"], "step": shown,
                           "basis": "same cars" if matched is not None else "median",
                           "n_matched": n_matched,
                           "from": pts[-2]["v"], "to": latest,
                           "from_day": pts[-2]["d"], "to_day": pts[-1]["d"],
                           "n": pts[-1]["n"]})

    models.sort(key=lambda m: (-m["listings"], m["name"]))
    movers.sort(key=lambda m: -abs(m["step"]))

    # Coverage: a trim swept once or twice cannot show a trend. Surfacing it
    # here is what stops a narrow selection reading as lost data.
    thin = sorted(({"name": str(k), "days": int(v)} for k, v in
                   priced[priced["sel_key"] != "-"].groupby("sel_key")["day"]
                   .nunique().items() if v <= 2),
                  key=lambda t: (t["days"], t["name"]))

    last_day = all_days[-1]
    day_rows = df[df["day"] == last_day]

    health = {
        "no_odometer": round(df["odometer_km"].isna().mean() * 100),
        "no_year": round(df["year"].isna().mean() * 100),
        "no_price": round(df["price_aud"].isna().mean() * 100),
        "brief": round((df["detail_level"] == "brief").mean() * 100),
        "rows": int(len(df)),
        # Vehicles re-seen on a later day: the only rows that can give a
        # same-car price movement. Pasted text carries no ad id, so a capture
        # with no ids can never be matched to any other - which is why the
        # tip is to save the page (Ctrl+S) when tracking movement.
        "tracked": int((df.groupby("key")["day"].nunique() > 1).sum()),
        "ids_latest": round(day_rows["listing_id"].notna().mean() * 100),
        # two cars merged onto one identity - see analyze.collided_keys
        "collisions": len(collided),
    }
    fresh = day_rows[day_rows["first_seen"].astype(str).str[:10] == last_day]
    latest = {
        "day": last_day,
        "rows": int(len(day_rows)),
        "new_vehicles": int(fresh["key"].nunique()),
        "models": [{"name": str(k), "n": int(v)} for k, v in
                   day_rows.groupby("model_line")["key"].count()
                   .sort_values(ascending=False).head(5).items()],
    }

    return {"ok": True, "empty": False, "level": level, "totals": totals,
            "models": models[:top], "model_count": len(models),
            "movers": movers[:5], "thin": thin[:6], "thin_count": len(thin),
            "health": health, "latest": latest}


def status_payload(db_path: str | Path) -> dict:
    """Cheap poll target. `data_version` changes whenever the data changes,
    so any open page can notice an import made in another tab and refresh
    instead of quietly showing stale numbers. `workbook` reports whether
    viewer.xlsx shows the data as it is now - the Excel file is the one view
    that does NOT recompute itself - and any build in progress."""
    with Store(db_path) as store:
        s = store.status()
        last = store.conn.execute(
            "SELECT MAX(id), MAX(ts) FROM ingest_log").fetchone()
    last_id, last_ts = (last or (None, None))
    version = f"{last_id or 0}:{s['observations']}:{s['vehicles']}"

    # Stale = the data the workbook was stamped with is not the data now.
    # It used to compare the file's date with the last IMPORT, so an edit, a
    # merge or an undo left a workbook showing old numbers marked as current.
    from . import viewer_xlsx
    stamp = viewer_xlsx.workbook_stamp(viewer_xlsx.DEFAULT_OUT)
    built = stamp["built"] if stamp else None
    if stamp is None:
        stale = bool(s["observations"])          # never built, or unreadable
    elif stamp["data"] is None:
        stale = True        # built before stamps: cannot vouch for it
    else:
        stale = stamp["data"] != _data_fp(db_path)

    # Possible duplicates, re-checked whenever the data changes - so an
    # import that re-captures a car already stored under another identity
    # lights the header's Duplicates button on every open page. Never allowed
    # to break the status poll itself.
    try:
        from . import dupes
        dup = dupes.summary(db_path)
    except Exception:
        dup = None
    return {"ok": True, "app": "vpa-import", "build": _BUILD, "db": s["db_path"],
            "vehicles": s["vehicles"], "observations": s["observations"],
            "data_version": version, "last_ingest": last_ts,
            "workbook": {"built": built, "stale": stale,
                         "job": xl_state(recent=True)},
            "dupes": dup}


_FP_CACHE: dict = {}


def _data_fp(db_path) -> str | None:
    """The database's data fingerprint, recomputed only when the database
    file has been written since - every open page polls status."""
    p = Path(db_path)
    key = [str(p)]
    for f in (p, p.with_name(p.name + "-wal")):
        try:
            st = f.stat()
            key += [st.st_mtime_ns, st.st_size]
        except OSError:
            key += [None, None]
    if _FP_CACHE.get("key") != key:
        from .viewer_xlsx import data_fingerprint
        _FP_CACHE.update(key=key, val=data_fingerprint(db_path))
    return _FP_CACHE["val"]


# ---- the Excel build, as a background job ---------------------------------
# A build takes several seconds. Run inside the request, the button just sat
# greyed out with no sign of life. Now the request starts a job and returns,
# and every open page polls its progress. One job at a time: a second click
# (from any tab) joins the running one instead of starting a rival build of
# the same file.
_XL_LOCK = threading.Lock()
_XL: dict = {"id": 0, "running": False}
_XL_DONE: dict = {}


def xl_state(recent: bool = False) -> dict | None:
    """Snapshot of the current/last build. With `recent`, None unless one is
    running or finished in the last half minute (for the status poll)."""
    with _XL_LOCK:
        st = {k: v for k, v in _XL.items()}
    if not st["id"]:
        return None
    end = st.get("finished") or time.time()
    st["elapsed"] = round(end - st["started"], 1)
    if recent and not st["running"] and time.time() - end > 30:
        return None
    return st


def xl_start(db_path, open_after: bool) -> dict:
    with _XL_LOCK:
        if _XL["running"]:
            if open_after:
                _XL["open"] = True       # a joining "Excel viewer" click opens too
            return {"ok": True, "job": _XL["id"], "joined": True}
        job = _XL["id"] + 1
        _XL.clear()
        _XL.update(id=job, running=True, frac=0.0, text="Starting",
                   started=time.time(), finished=None, ok=None, error=None,
                   open=open_after, opened=False, path=None)
        _XL_DONE[job] = threading.Event()
        threading.Thread(target=_xl_run, args=(db_path, job), daemon=True).start()
    return {"ok": True, "job": job, "joined": False}


def _xl_run(db_path, job: int):
    from . import viewer_xlsx

    def prog(frac, text):
        with _XL_LOCK:
            _XL.update(frac=round(frac, 3), text=text)

    res = {"ok": False}
    try:
        p = viewer_xlsx.generate(db_path, viewer_xlsx.DEFAULT_OUT, progress=prog)
        res = {"ok": True, "path": str(p)}
        with _XL_LOCK:
            want = _XL["open"]
        if want:
            prog(1.0, "Opening in Excel")
            os.startfile(str(p))             # opens in Excel (Windows)
            res["opened"] = True
    except PermissionError:
        res["error"] = ("viewer.xlsx is locked - it is open in Excel, or "
                        "OneDrive is syncing it. Close it / wait a moment, "
                        "then click again. Nothing was changed.")
    except RuntimeError as e:
        res["error"] = str(e)
    except Exception as e:                    # say it; never leave a dead job
        traceback.print_exc()
        res["error"] = (f"the workbook build failed ({type(e).__name__}: {e}). "
                        "The previous viewer.xlsx is untouched.")
    finally:
        with _XL_LOCK:
            _XL.update(running=False, finished=time.time(),
                       ok=res.get("ok", False), error=res.get("error"),
                       opened=bool(res.get("opened")), path=res.get("path"),
                       frac=1.0 if res.get("ok") else _XL.get("frac", 0))
        _XL_DONE[job].set()


def _running_instance(port: int) -> bool:
    """True if a CURRENT vpa import UI already answers on this port.
    An instance running older code (different build marker) is asked to shut
    down so this launch can serve the updated code."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            status = json.loads(r.read().decode())
    except Exception:
        return False
    if status.get("app") != "vpa-import":
        return False
    if status.get("build") == _BUILD:
        return True
    # stale instance from before a code update - replace it
    print("replacing an older running instance...")
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown", data=b"{}"), timeout=3)
    except Exception:
        pass
    import time
    time.sleep(1.0)
    return False


# ------------------------------------------------------------------ server
class _Handler(BaseHTTPRequestHandler):
    db_path: str = str(DEFAULT_DB)   # set by serve()

    def log_message(self, fmt, *args):  # quiet: one short line, ASCII
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # NEVER let a browser cache anything this app serves.
        #
        # The pages carry their JavaScript inline, so a cached page pins an
        # OLD copy of the client code against today's API. It renders the
        # shell — picker, headings, tab strip — then throws part-way through
        # and leaves every panel blank, with no console error to explain it
        # and a server that answers every request perfectly when probed. That
        # cost a long diagnosis on 06/08/2026; the page served was
        # byte-identical to source and the same URL rendered fine in another
        # browser, which is the signature of a stale cache and nothing else.
        #
        # There is no upside to caching here: it is localhost, the payloads
        # are small, and the code changes often.
        self.send_header("Cache-Control",
                         "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        # split off any query string: the page ignores one, but "/?x=1" is a
        # perfectly ordinary URL to arrive with (a bookmark, a reload with a
        # cache-buster) and used to 404
        bare = self.path.split("?", 1)[0]
        if bare in ("/", "/index.html", "/overview"):
            self._send(200, OVERVIEW.encode("utf-8"), "text/html; charset=utf-8")
        elif bare in ("/add", "/import"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif bare == "/bulk":
            from . import bulk
            self._send(200, bulk.PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif bare == "/quick":
            from . import quickadd
            self._send(200, quickadd.PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif bare == "/explore":
            from . import explore
            self._send(200, explore.PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif bare == "/duplicates":
            from . import dupes
            self._send(200, dupes.PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/dupes":
            from . import dupes
            self._json(dupes.payload(self.db_path))
        elif bare == "/api/inbox":
            from urllib.parse import parse_qs, urlparse
            from . import inbox
            q = parse_qs(urlparse(self.path).query)
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._json(inbox.state(self.db_path, since))
        elif self.path == "/api/viewer/progress":
            self._json({"ok": True, "job": xl_state()})
        elif bare == "/compare":
            from . import compare
            self._send(200, compare.PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/compare/catalogue":
            from . import compare
            self._json(compare.catalogue_payload(self.db_path))
        elif self.path.startswith("/api/compare"):
            from urllib.parse import parse_qs, urlparse
            from . import compare
            q = parse_qs(urlparse(self.path).query)
            self._json(compare.compare_payload(
                self.db_path, labels=q.get("sel") or [],
                live_only=(q.get("live") or ["0"])[0] == "1"))
        elif self.path == "/api/verify":
            from . import verify
            self._json(verify.verify_payload(self.db_path))
        elif self.path == "/api/explore/picker":
            from . import explore
            self._json(explore.picker_payload(self.db_path))
        elif self.path.startswith("/api/explore/detail"):
            from urllib.parse import parse_qs, urlparse
            from . import explore
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: (q.get(k) or [""])[0].strip() or None
            # `variant` repeats: one trim, several pooled trims and all trims
            # are the same parameter, so a bookmarked pooled selection is an
            # ordinary URL rather than an encoded blob.
            self._json(explore.detail_payload(
                self.db_path, make=one("make"), model=one("model"),
                variants=[v for v in (q.get("variant") or []) if v.strip()],
                year_min=one("y0"), year_max=one("y1"),
                price_min=one("p0"), price_max=one("p1"), keyword=one("q")))
        elif self.path.startswith("/api/explore/car"):
            from urllib.parse import parse_qs, urlparse
            from . import explore
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: (q.get(k) or [""])[0].strip() or None
            # `target` is deliberately NOT read: it is the user's own number,
            # not a market statistic, and it never leaves their browser.
            self._json(explore.car_payload(
                self.db_path, make=one("make"), model=one("model"),
                variant=one("variant"), key=one("car"), price=one("price"),
                odometer=one("odo"), year=one("year"), seller=one("seller"),
                state=one("state")))
        elif self.path == "/api/quick/models":
            from . import quickadd
            self._json(quickadd.models_payload(self.db_path))
        elif self.path == "/api/watchlist":
            from . import starred
            self._json(starred.watchlist_payload(self.db_path))
        elif self.path == "/api/edit/history":
            from . import edit
            self._json({"ok": True, "batches": edit.history(self.db_path)})
        elif self.path == "/api/bulk/history":
            from . import bulk
            self._json({"ok": True, "runs": bulk.history(self.db_path)})
        elif self.path.startswith("/api/edit/row"):
            from urllib.parse import parse_qs, urlparse
            from . import edit
            q = parse_qs(urlparse(self.path).query)
            self._json(_edit_row_payload(
                self.db_path, (q.get("key") or [""])[0]))
        elif self.path == "/api/heatmap":
            self._json(heatmap_payload(self.db_path))
        elif self.path.startswith("/api/overview"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            self._json(overview_payload(self.db_path,
                                        level=(q.get("level") or ["line"])[0]))
        elif self.path == "/api/status":
            self._json(status_payload(self.db_path))
        elif self.path.startswith("/api/"):
            # a GET to an API route this build does not have - same stale
            # process story as the POST side, and the page can act on it
            self._json({"ok": False, "stale": True, "error":
                        f"this copy of the app does not have {self.path}. It "
                        f"is running an older version than the page you are "
                        f"looking at - click Shut down, then reopen Vehicle "
                        f"Import."}, 404)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "bad request body"}, 400)
            return
        try:
            if self.path == "/api/parse":
                self._json(parse_payload(payload, self.db_path))
            elif self.path == "/api/commit":
                self._json(commit_payload(payload, self.db_path))
            elif self.path == "/api/explore/identify":
                from . import explore
                self._json(explore.identify_payload(
                    self.db_path, payload.get("text") or ""))
            elif self.path == "/api/explore/link":
                from . import explore
                self._json(explore.link_payload(
                    self.db_path, payload.get("link") or ""))
            elif self.path == "/api/quick/stats":
                from . import quickadd
                self._json(quickadd.stats_payload(payload, self.db_path))
            elif self.path == "/api/quick/commit":
                from . import quickadd
                self._json(quickadd.commit_payload(payload, self.db_path))
            elif self.path in ("/api/viewer/open", "/api/viewer/build"):
                # Same build either way; only the last step differs. Kept as
                # one job so the two can never build the workbook differently
                # — "rebuild" must produce the identical file that "rebuild
                # and open" would have opened. Returns at once with a job id;
                # the page polls /api/viewer/progress. {"wait": true} blocks
                # until it is done instead (the tests, and scripts).
                started = xl_start(self.db_path, self.path.endswith("/open"))
                if payload.get("wait"):
                    _XL_DONE[started["job"]].wait()
                    st = xl_state()
                    self._json({"ok": st["ok"], "path": st["path"],
                                "opened": st["opened"], "error": st["error"],
                                "job": st["id"], "elapsed": st["elapsed"]})
                else:
                    self._json(started)
            elif self.path == "/api/star":
                from . import starred
                self._json(starred.star(self.db_path,
                                        key=payload.get("key") or "",
                                        note=payload.get("note"),
                                        on=bool(payload.get("on", True))))
            elif self.path == "/api/edit/preview":
                from . import edit
                from .store import Store as _S
                with _S(self.db_path) as st:
                    p = edit.plan(st, payload.get("ops") or [])
                self._json({**p, "fingerprint": edit.fingerprint(p)})
            elif self.path == "/api/edit/apply":
                from . import edit
                self._json(edit.apply(self.db_path, payload.get("ops") or [],
                                      confirm=payload.get("confirm")))
            elif self.path == "/api/edit/undo":
                from . import edit
                self._json(edit.undo(self.db_path, payload.get("batch") or ""))
            elif self.path == "/api/dupes/merge":
                from . import dupes
                self._json(dupes.merge(self.db_path, payload.get("ids") or []))
            elif self.path == "/api/dupes/dismiss":
                from . import dupes
                self._json(dupes.dismiss(self.db_path, payload.get("id") or "",
                                         on=bool(payload.get("on", True))))
            elif self.path == "/api/dupes/undo":
                from . import dupes
                self._json(dupes.undo(self.db_path, payload.get("batch") or ""))
            elif self.path == "/api/inbox/start":
                from . import inbox
                self._json(inbox.start(self.db_path, payload.get("folder") or "",
                                       payload.get("pages"),
                                       payload.get("label") or ""))
            elif self.path == "/api/inbox/stop":
                from . import inbox
                self._json(inbox.stop())
            elif self.path == "/api/inbox/undo":
                from . import inbox
                self._json(inbox.undo_session(self.db_path))
            elif self.path == "/api/inbox/open":
                # show the folder in Explorer - only the folder a session or
                # the saved settings name, never a path sent by the page
                from . import inbox
                st = inbox.state(self.db_path)
                f = Path((st["session"] or {}).get("folder")
                         or st["settings"]["folder"])
                if f.is_dir():
                    os.startfile(str(f))
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "that folder does not exist yet - "
                                "start a session and it is created"})
            elif self.path == "/api/bulk/run":
                from . import bulk
                self._json(bulk.run_payload(payload, self.db_path))
            elif self.path == "/api/bulk/undo":
                from . import bulk
                self._json(bulk.undo(self.db_path,
                                     run_id=int(payload.get("run") or 0)))
            elif self.path == "/api/shutdown":
                self._json({"ok": True, "bye": True})
                # shutdown() blocks until serve_forever returns, so it must
                # run on another thread than the one handling this request
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                # Almost always a stale process rather than a typo: the page
                # was served by a newer build than the one still running, so
                # it calls an endpoint this process has never heard of. Say
                # that, instead of leaving a bare "unknown endpoint".
                self._json({"ok": False, "stale": True, "error":
                            f"this copy of the app does not have {self.path}. "
                            f"It is running an older version than the page you "
                            f"are looking at - click Shut down, then reopen "
                            f"Vehicle Import."}, 404)
        except Exception as e:   # surface real errors to the page, not a 500 page
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def serve(db_path: str | Path = DEFAULT_DB, port: int = DEFAULT_PORT,
          open_browser: bool = True) -> None:
    # Under pythonw.exe (the no-console launcher) stdout/stderr are None and
    # any print() would crash the server thread — route them to devnull.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # One instance is enough: if the UI is already up, just open the page.
    if _running_instance(port):
        print(f"Import UI already running on http://127.0.0.1:{port} - opening it.")
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}")
        return
    handler = type("Handler", (_Handler,), {"db_path": str(db_path)})
    httpd = None
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port in {port}..{port + 9}; use --port")
    url = f"http://127.0.0.1:{port}"
    print(f"Import UI on {url}  (database: {db_path})")
    print("Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    print("stopped.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vpa import-ui",
                                 description="Local preview-before-commit import page.")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    serve(args.db, args.port, open_browser=not args.no_browser)
    return 0


# -------------------------------------------------------------------- page
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>vpa add data - import</title>
<style>
__CHROME_CSS__
 /* ---- bringing data in ---- */
 .inrow{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
 #drop{border:1.5px dashed var(--field);border-radius:12px;padding:0 18px;gap:12px;
       color:var(--dim);cursor:pointer;flex:1;min-width:260px;background:var(--head);
       height:74px;display:flex;align-items:center;justify-content:center;font-size:13px;
       transition:background .15s,border-color .15s,color .15s}
 #drop b{color:var(--ink);font-weight:600}
 #drop .dropico{color:var(--accent)}
 #drop:hover{background:#f3f7fe;border-color:var(--accent-line);color:var(--ink-2)}
 #drop.hover{background:var(--accent-soft);border-color:var(--accent)}
 #paste{width:100%;height:92px;margin-top:10px;resize:vertical}
 input[type=date]{width:148px}
 select{min-width:128px}
 /* the preview table: every cell is an input, so it reads as a sheet */
 #tbl td{padding:2px 4px;vertical-align:top}
 td input{border:1px solid transparent;border-radius:4px;height:26px;
          font-size:12.5px;padding:0 4px;background:transparent;color:var(--ink)}
 td input[data-f=capture_date]{width:96px}td input[data-f=make]{width:82px}
 td input[data-f=model]{width:92px}td input[data-f=variant]{width:130px}
 td input[data-f=year]{width:50px}td input[data-f=price_aud]{width:76px}
 td input[data-f=odometer_km]{width:80px}td input[data-f=state]{width:48px}
 td input[data-f=seller_type]{width:68px}
 td input:hover{border-color:var(--field)}
 td input:focus{border-color:var(--accent);background:#fff;outline:0}
 tr.off{opacity:.45}
 .flag{margin:1px 3px 1px 0}
 .count{font-weight:600}
 #summary{margin:0 0 10px;color:var(--ink-2);font-size:13px}
 .commitbar{display:flex;gap:10px;align-items:center;margin-top:12px}
 #selcount{color:var(--dim);font-size:12.5px}
 #result{white-space:pre-line;font-size:13.5px}
 /* ---- overview ---- */
 #heat{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,11px);gap:2px}
 #heat div{width:11px;height:11px;border-radius:2px;background:#e8eaee}
 .h1{background:#c9e6c9!important}.h2{background:#8fcb8f!important}
 .h3{background:#4d9e4d!important}.h4{background:#256e25!important}
 .heatlegend{font-size:11.5px;color:var(--dim);display:flex;gap:3px;align-items:center;
             margin-top:10px}
 .heatlegend div{width:11px;height:11px;border-radius:2px;background:#e8eaee}
 .ov td{vertical-align:middle}
 .ov .nm{font-weight:600}
 .bar{height:6px;border-radius:3px;background:var(--line-2);overflow:hidden;margin-top:3px}
 .bar i{display:block;height:100%;background:var(--accent)}
 /* data check rows wrap freely - .lst clips to one line, which would hide
    exactly the explanation that makes a warning actionable */
 .vf>div{display:flex;gap:9px;align-items:flex-start;padding:6px 0;
         border-top:1px solid var(--line-2);font-size:12.5px}
 .vf>div:first-child{border-top:0}
 .vf .mk{font-weight:700;width:13px;flex:none;text-align:center;line-height:1.4}
 .vf .bd{flex:1}
 .vf .bd b{font-weight:600}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Add data</h1>
 <div class="sub">Check every row before anything is saved.</div>
</div>__ACTIONS__</div>
__MODES__
<div class="card" id="help">
 <p><b>The one rule:</b> nothing is written to the database until you press
 <b>Commit selected</b>. Preview is free - parse anything, look at it, discard it.</p>
 <details open><summary>Collecting listings (within each site's terms)</summary>
  <ul>
   <li>Browse yourself and save what you see - this app never fetches from the
       sites, and don't point bots or scrapers at them.</li>
   <li><b>Carsales / Bikesales:</b> open a results or listing page, press
       <code>Ctrl+S</code> ("Webpage, Complete" or "HTML Only"), drop the saved
       <code>.html</code> here. Saved pages carry structured data, so this is the
       cleanest source.</li>
   <li><b>Facebook Marketplace:</b> select the results text on the page
       (<code>Ctrl+A</code>, <code>Ctrl+C</code>) and use the paste box. Tile
       dumps parse as-is - prices above titles, "AU$" prices, and struck-through
       old prices are handled; tiles without a year-led title (wheels, parts)
       are skipped. Do NOT save the page as HTML - Facebook saves are empty
       shells with no listings in them.</li>
   <li><b>Your own spreadsheet:</b> drop a <code>.csv</code> or <code>.json</code>
       export - common column names (Price ($), KMs, Rego State, Ad ID...) are
       recognised automatically.</li>
  </ul></details>
 <details><summary>Preview flags - what they mean</summary>
  <ul>
   <li><span class="flag">already in database (same date, price, odometer)</span>
       - exact duplicate; starts unticked. Committing it anyway is harmless
       (the store skips exact duplicates).</li>
   <li><span class="flag">duplicate within this upload</span> - the same listing
       appears twice in this file; starts unticked.</li>
   <li><span class="flag info">known listing: price change A$X -&gt; A$Y</span>
       - a vehicle already tracked, now at a new price. Commit it - that IS the
       price history.</li>
   <li><span class="flag info">known listing (new sighting)</span> - seen before,
       details differ slightly or same price on a new date.</li>
   <li><span class="flag">no usable price</span> - "Negotiable" / "POA" or
       unreadable; starts unticked. Tick it only if you fix the price cell.</li>
   <li><span class="flag">no year</span> / <span class="flag">no odometer</span>
       - kept, but year-less rows land in an "unknown-year" segment and
       odometer-less rows weaken mileage adjustment. Fill the cell if the ad
       shows the value.</li>
   <li>"<b>N raw block(s) could not be parsed</b>" in the summary = blocks with
       no usable make+model. Usually a paste without a title line - add one and
       re-preview.</li>
  </ul></details>
 <details><summary>Tips for clean data</summary>
  <ul>
   <li><b>Capture date = the day you saved/copied the listing.</b> It defaults
       to the file's save date; correct it if you're importing older saves.</li>
   <li><b>One source per import</b>, and set the source dropdown when pasting -
       identity for pasted listings uses source + vehicle details, so mixing
       sources in one paste can miss duplicates.</li>
   <li><b>Always include the odometer line in pastes</b> - it is part of how
       re-listings are matched when there is no site ID.</li>
   <li><b>Fix values in the table, not in your file:</b> click a cell, type
       "$18,990" or "98k" or "NSW" - it is re-normalised on commit exactly like
       file input.</li>
   <li><b>Sanity-check outliers before committing:</b> a $500 bike or $123,456
       car is usually a typo or a joke ad - untick it.</li>
   <li>Re-importing the same file later is safe: everything already stored is
       flagged and skipped.</li>
  </ul></details>
</div>

<div class="card">
 <div class="ptitle">add listings <span>&mdash; one file or several</span></div>
 <div class="inrow">
  <div id="drop"><svg class="ico dropico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V4m0 0 4 4m-4-4-4 4M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg><span id="droplabel"><b>Drop saved pages, CSV or JSON</b> or click to choose</span></div>
  <input type="file" id="file" accept=".csv,.json,.html,.htm,.txt" multiple hidden>
  <div><label>source</label>
   <select id="source"><option value="">auto / from data</option>
    <option>carsales</option><option>bikesales</option><option>facebook</option>
    <option>gumtree</option><option>other</option></select></div>
  <div><label>format</label>
   <select id="format"><option value="">auto</option><option>csv</option>
    <option>json</option><option>html</option><option>text</option></select></div>
  <div><label>capture date</label><input type="date" id="capdate"></div>
  <div><button class="primary" id="parseBtn" disabled>Preview</button></div>
 </div>
 <textarea id="paste" placeholder="...or paste raw listing text here (title lines like '2014 Ford Falcon XR6', then price / km / suburb)"></textarea>
</div>

<!-- The preview sits directly under the box that produced it. It used to
     be the LAST card, below six overview panels, so after pressing Preview
     nothing visible happened until you scrolled a long way down. -->
<div class="card" id="previewCard" style="display:none">
 <div class="ptitle">preview <span>&mdash; nothing is saved until you commit</span></div>
 <div id="summary"></div>
 <div class="scroll"><table id="tbl"><thead><tr>
  <th><input type="checkbox" id="all" checked></th><th>status / flags</th>
  <th>src</th><th>date</th><th>make</th><th>model</th><th>variant</th>
  <th>year</th><th>price</th><th>odo km</th><th>state</th><th>seller</th>
 </tr></thead><tbody></tbody></table></div>
 <div class="commitbar">
  <button class="commit" id="commitBtn">Commit selected</button>
  <button class="ghost" id="cancelBtn">Discard</button>
  <span id="selcount"></span>
 </div>
</div>

<div class="card" id="resultCard" style="display:none"><div id="result"></div></div>

<script>
const $=id=>document.getElementById(id);
let pending=null;           // [{name,text,size,capture_date}] - several files
let rows=[];                // parse result rows, from every file merged
let SOURCES=[];             // rows[i] came from SOURCES[i] - which file
const EDITABLE=["capture_date","make","model","variant","year","price_aud",
                "odometer_km","state","seller_type"];

const localISO=d=>{const x=new Date(d);x.setMinutes(x.getMinutes()-x.getTimezoneOffset());
  return x.toISOString().slice(0,10)};
$("capdate").value=localISO(Date.now());

const money=v=>"$"+Math.round(v).toLocaleString();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
/* Several files at once. Each keeps ITS OWN capture date, taken from that
   file's own mtime - a folder of pages saved across three evenings is three
   capture days, and collapsing them onto one date would invent price history
   that never happened. */
function takeFiles(list){
  const files=[...list];
  if(!files.length)return;
  Promise.all(files.map(f=>f.text().then(t=>({
    name:f.name,text:t,size:f.size,
    capture_date:f.lastModified?localISO(f.lastModified):null}))))
    .then(loaded=>{
      pending=pending&&pending.length?pending.concat(loaded):loaded;
      // NOTE: write the label span, never drop.textContent - that would
      // destroy #drop's children (this was the "works only once" bug when
      // the file input lived inside #drop)
      const kb=pending.reduce((a,f)=>a+f.size,0)/1024;
      $("droplabel").textContent=pending.length===1
        ?`${pending[0].name} (${kb.toFixed(1)} kB) - ready`
        :`${pending.length} files (${(kb/1024).toFixed(1)} MB) - ready`;
      // one file: show its date, and let it be overridden. Several: the
      // field cannot speak for all of them, so it is left blank and each
      // file's own date is used.
      $("capdate").value=pending.length===1&&pending[0].capture_date
        ?pending[0].capture_date:"";
      $("capdate").disabled=pending.length>1;
      $("parseBtn").disabled=false;
      // clear the input so picking the same (or a re-saved) file fires
      // 'change' again - browsers only fire it when the value differs
      $("file").value="";});}
$("drop").onclick=()=>$("file").click();
$("file").onchange=e=>takeFiles(e.target.files);
["dragover","dragenter"].forEach(ev=>$("drop").addEventListener(ev,e=>{e.preventDefault();$("drop").classList.add("hover");}));
["dragleave","drop"].forEach(ev=>$("drop").addEventListener(ev,e=>{e.preventDefault();$("drop").classList.remove("hover");}));
$("drop").addEventListener("drop",e=>takeFiles(e.dataTransfer.files));
$("paste").oninput=()=>{$("parseBtn").disabled=!($("paste").value.trim()||(pending&&pending.length));};

$("parseBtn").onclick=async()=>{
  const queue=(pending&&pending.length)?pending
    :[{name:"pasted.txt",text:$("paste").value,capture_date:null}];
  $("parseBtn").disabled=true;
  rows=[];SOURCES=[];
  let dropped=0,fmt=null,notes=[],failed=[];
  for(let i=0;i<queue.length;i++){
    const f=queue[i];
    $("parseBtn").textContent=queue.length>1
      ?`parsing ${i+1}/${queue.length}...`:"parsing...";
    const body={name:f.name,text:f.text,source:$("source").value,
      format:$("format").value,
      // the date box wins only when it is usable: one file, or a paste
      capture_date:($("capdate").value||f.capture_date||"")};
    if(f.name==="pasted.txt"&&!body.format)body.format="text";
    let res;
    try{
      res=await fetch("/api/parse",{method:"POST",
        body:JSON.stringify(body)}).then(r=>r.json());
    }catch(e){res={ok:false,error:String(e)};}
    if(!res.ok){failed.push({name:f.name,error:res.error,challenge:!!res.challenge});continue;}
    fmt=fmt||res.format;dropped+=res.dropped||0;
    if(res.note)notes.push(res.note);
    // remember which file each row came from, so the preview can say
    res.rows.forEach(r=>{rows.push(r);SOURCES.push(f.name);});
  }
  $("parseBtn").disabled=false;$("parseBtn").textContent="Preview";
  if(!rows.length&&failed.length){reportFailures(failed);return;}
  renderTable({format:fmt||"text",dropped:dropped,
    note:notes.join(" "),rows:rows,files:queue.length,
    failed:failed,
    summary:{parsed:rows.length,
      duplicates:rows.filter(r=>!r.checked&&r.flags.some(f=>/duplicate|already/.test(f))).length,
      no_price:rows.filter(r=>r.price_aud==null).length,
      no_odometer:rows.filter(r=>r.odometer_km==null).length}});
};

/* A file the server refused is shown in full, not alerted away: a
   bot-protection page needs re-saving and the message says how. */
function reportFailures(failed){
  $("previewCard").style.display="none";
  $("resultCard").style.display="";
  $("result").innerHTML=failed.map(f=>
    `<div class="${f.challenge?"warn":"warn"}" style="margin-bottom:8px">${f.error}</div>`
  ).join("");
  vpaReveal($("resultCard"));
}

function fmtPrice(v){return v==null?"":Number(v).toLocaleString();}
function renderTable(res){
  const s=res.summary;
  // per-file breakdown, so a folder of pages does not become one opaque
  // number - and so a file that yielded NOTHING is visible rather than
  // averaged away by the ones that worked
  let perFile="";
  if(res.files>1){
    const counts={};SOURCES.forEach(n=>{counts[n]=(counts[n]||0)+1;});
    const names=[...new Set(SOURCES)];
    perFile=`<div class="hint" style="margin-top:6px">from
      <b>${res.files}</b> file(s): `
      +names.map(n=>`${n.replace(/ cars for sale.*$/,"")} <b>${counts[n]}</b>`).join(" &middot; ")
      +(names.length<res.files-(res.failed||[]).length
        ?` &middot; <span class="warn">${res.files-(res.failed||[]).length-names.length}
           file(s) produced no rows</span>`:"")
      +`. Each file keeps its own capture date.</div>`;
  }
  const refused=(res.failed||[]).length
    ? (res.failed||[]).map(f=>`<div class="warn" style="margin-top:6px">${f.error}</div>`).join("")
    : "";
  $("summary").innerHTML=`<span class="count">${s.parsed}</span> row(s) parsed as
   <b>${res.format}</b>${res.dropped?` - <span class="warn">${res.dropped} raw block(s) could not be parsed at all</span>`:""}.
   ${res.note?`<span class="warn">${res.note}</span> `:""}
   Flags: ${s.duplicates} duplicate(s), ${s.no_price} without a price, ${s.no_odometer} without odometer.
   Flagged duplicates and no-price rows start unticked.`+perFile+refused;
  const tb=$("tbl").querySelector("tbody");tb.innerHTML="";
  rows.forEach((r,i)=>{
    const tr=document.createElement("tr");tr.dataset.i=i;
    const flags=r.flags.map(f=>`<span class="flag${f.startsWith("known")?" info":""}">${f}</span>`).join("");
    tr.innerHTML=`<td><input type="checkbox" class="sel" ${r.checked?"checked":""}></td>
     <td title="${String(r.raw_title||"").replace(/"/g,"&quot;")}">${r.status==="new"?"":r.status.replace("_"," ")} ${flags}</td>
     <td>${r.source}</td>`+
     EDITABLE.slice(0).map(f=>{
       const v=r[f]==null?"":(f==="price_aud"?fmtPrice(r[f]):r[f]);
       const cls=(f==="price_aud"||f==="odometer_km"||f==="year")?" class=num":"";
       return `<td${cls}><input data-f="${f}" value="${String(v).replace(/"/g,"&quot;")}"></td>`;}).join("");
    if(!r.checked)tr.classList.add("off");
    tr.querySelector(".sel").onchange=e=>{tr.classList.toggle("off",!e.target.checked);count();};
    tb.appendChild(tr);});
  $("previewCard").style.display="";$("resultCard").style.display="none";count();
}
function count(){const n=document.querySelectorAll(".sel:checked").length;
  $("selcount").textContent=`${n} of ${rows.length} selected`;
  $("commitBtn").disabled=!n;}
$("all").onchange=e=>{document.querySelectorAll(".sel").forEach(cb=>{
  cb.checked=e.target.checked;cb.closest("tr").classList.toggle("off",!e.target.checked);});count();};

$("commitBtn").onclick=async()=>{
  const out=[];
  document.querySelectorAll("#tbl tbody tr").forEach(tr=>{
    if(!tr.querySelector(".sel").checked)return;
    const r={...rows[tr.dataset.i]};
    delete r.flags;delete r.checked;delete r.status;delete r.key;
    tr.querySelectorAll("input[data-f]").forEach(inp=>{r[inp.dataset.f]=inp.value.trim()||null;});
    out.push(r);});
  $("commitBtn").disabled=true;$("commitBtn").textContent="writing...";
  const res=await fetch("/api/commit",{method:"POST",body:JSON.stringify({rows:out})}).then(r=>r.json());
  $("commitBtn").disabled=false;$("commitBtn").textContent="Commit selected";
  if(!res.ok){alert(res.error);return;}
  $("previewCard").style.display="none";$("resultCard").style.display="";
  let msg=`<span class="ok">Committed: ${res.committed} observation(s), ${res.new_vehicles} new vehicle(s).</span>`;
  if(res.duplicates_skipped)msg+=`\n${res.duplicates_skipped} exact duplicate(s) skipped by the store.`;
  if(res.rejected.length)msg+=`\n<span class="warn">Rejected: ${res.rejected.join("; ")}</span>`;
  msg+=`\n\nRe-parse the same file to confirm: every committed row should now show as a duplicate.`;
  $("result").innerHTML=msg;
  // The point of an import, for anyone with a shortlist: what did it just do
  // to the cars I am following? Shown here rather than left to be found on
  // another page two clicks away.
  showStarredAfterImport();
  // the header's counts and the workbook's age both moved; the dataset
  // panels live on the Overview page and recompute when it is opened
  vpaStatus();
  pending=null;SOURCES=[];
  $("droplabel").innerHTML="<b>Drop saved pages, CSV or JSON</b> or click to choose";
  $("capdate").value="";$("capdate").disabled=false;
  $("paste").value="";$("parseBtn").disabled=true;
};
/* What the import just did to the starred cars. Read-only: it asks the same
   /api/watchlist the Explore page uses, so the two can never tell different
   stories about the same shortlist. */
async function showStarredAfterImport(){
  let w;
  try{w=await fetch("/api/watchlist").then(r=>r.json());}catch(e){return;}
  if(!w||!w.ok||!w.total)return;
  const cash=v=>v==null?"—":"$"+Number(v).toLocaleString();
  const pc=v=>`<span class="${v>0?"warn":"ok"}">${v>0?"+":""}${v}%</span>`;
  const moved=w.cars.filter(c=>c.present&&c.since&&c.since.move!=null);
  const stale=w.cars.filter(c=>c.present&&!c.live&&!moved.includes(c));
  const missing=w.cars.filter(c=>!c.present);
  const line=c=>`<div style="padding:3px 0">&#9733; <b>${c.label}</b>`
    +(c.since&&c.since.move!=null
      ?` ${cash(c.since.was)} &rarr; ${cash(c.since.now)} ${pc(c.since.move)}`
       +(c.since.vs_trim!=null
         ?` <span class="muted">(${c.since.vs_trim>0?"+":""}${c.since.vs_trim}% against its own trim)</span>`:"")
      :c.present?` ${cash(c.price)}`:"")
    +(!c.present?' <span class="warn">no longer in the database</span>'
      :!c.live?` <span class="warn">not in the ${c.trim_last_day} sweep of its trim</span>`:"")
    +`</div>`;
  const interesting=[...moved,...stale,...missing];
  $("result").innerHTML+=
    `<div style="margin-top:12px;padding-top:10px;border-top:1px solid #e6e9ef">`
    +`<b>Your watchlist after this import</b> &mdash; ${w.total} car(s) followed`
    +(interesting.length
      ?`:</div>`+interesting.map(line).join("")
       +`<div class="muted" style="margin-top:6px">Full watchlist on the `
       +`<a href="/explore">Explore</a> page.</div>`
      :`. Nothing on it changed.</div>`);
}

$("cancelBtn").onclick=()=>{$("previewCard").style.display="none";rows=[];SOURCES=[];
  pending=null;$("capdate").disabled=false;
  $("droplabel").innerHTML="<b>Drop saved pages, CSV or JSON</b> or click to choose";};

__CHROME_JS__
</script></div></body></html>
"""

#: The Overview page: the dataset at a glance, the data check and the
#: workbook rebuild. It was the bottom two thirds of the import page,
#: which made "Import" the home page and the place to read the data.
_OVERVIEW = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>vpa overview</title>
<style>
__CHROME_CSS__
 /* ---- bringing data in ---- */
 .inrow{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
 #drop{border:1.5px dashed var(--field);border-radius:12px;padding:0 18px;gap:12px;
       color:var(--dim);cursor:pointer;flex:1;min-width:260px;background:var(--head);
       height:74px;display:flex;align-items:center;justify-content:center;font-size:13px;
       transition:background .15s,border-color .15s,color .15s}
 #drop b{color:var(--ink);font-weight:600}
 #drop .dropico{color:var(--accent)}
 #drop:hover{background:#f3f7fe;border-color:var(--accent-line);color:var(--ink-2)}
 #drop.hover{background:var(--accent-soft);border-color:var(--accent)}
 #paste{width:100%;height:92px;margin-top:10px;resize:vertical}
 input[type=date]{width:148px}
 select{min-width:128px}
 /* the preview table: every cell is an input, so it reads as a sheet */
 #tbl td{padding:2px 4px;vertical-align:top}
 td input{border:1px solid transparent;border-radius:4px;height:26px;
          font-size:12.5px;padding:0 4px;background:transparent;color:var(--ink)}
 td input[data-f=capture_date]{width:96px}td input[data-f=make]{width:82px}
 td input[data-f=model]{width:92px}td input[data-f=variant]{width:130px}
 td input[data-f=year]{width:50px}td input[data-f=price_aud]{width:76px}
 td input[data-f=odometer_km]{width:80px}td input[data-f=state]{width:48px}
 td input[data-f=seller_type]{width:68px}
 td input:hover{border-color:var(--field)}
 td input:focus{border-color:var(--accent);background:#fff;outline:0}
 tr.off{opacity:.45}
 .flag{margin:1px 3px 1px 0}
 .count{font-weight:600}
 #summary{margin:0 0 10px;color:var(--ink-2);font-size:13px}
 .commitbar{display:flex;gap:10px;align-items:center;margin-top:12px}
 #selcount{color:var(--dim);font-size:12.5px}
 #result{white-space:pre-line;font-size:13.5px}
 /* ---- overview ---- */
 #heat{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,11px);gap:2px}
 #heat div{width:11px;height:11px;border-radius:2px;background:#e8eaee}
 .h1{background:#c9e6c9!important}.h2{background:#8fcb8f!important}
 .h3{background:#4d9e4d!important}.h4{background:#256e25!important}
 .heatlegend{font-size:11.5px;color:var(--dim);display:flex;gap:3px;align-items:center;
             margin-top:10px}
 .heatlegend div{width:11px;height:11px;border-radius:2px;background:#e8eaee}
 .ov td{vertical-align:middle}
 .ov .nm{font-weight:600}
 .bar{height:6px;border-radius:3px;background:var(--line-2);overflow:hidden;margin-top:3px}
 .bar i{display:block;height:100%;background:var(--accent)}
 /* data check rows wrap freely - .lst clips to one line, which would hide
    exactly the explanation that makes a warning actionable */
 .vf>div{display:flex;gap:9px;align-items:flex-start;padding:6px 0;
         border-top:1px solid var(--line-2);font-size:12.5px}
 .vf>div:first-child{border-top:0}
 .vf .mk{font-weight:700;width:13px;flex:none;text-align:center;line-height:1.4}
 .vf .bd{flex:1}
 .vf .bd b{font-weight:600}
 .vfcard{padding:14px 20px}
 /* split screen: the price range is the column that can go */
 @media (max-width:760px){#topTbl th:nth-child(8),#topTbl td:nth-child(8){display:none}}
 #verify:not(:empty){margin-top:12px}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Overview</h1>
 <div class="sub">Everything collected so far, and whether it adds up.</div>
</div>__ACTIONS__</div>

<div class="card" id="help">
 <p>This page reads the database and writes nothing. Add listings from
 <b>Add data</b>; look at one trim in <b>Explore</b>; set several side by side
 in <b>Compare</b>.</p>
 <details><summary>Reading the overview</summary>
  <ul>
   <li><b>Top models</b> ranks by how many prices you have collected, not by
       price. Switch between <b>Model lines</b> ("Ford Falcon", pools every
       trim) and <b>Trims</b> ("Ford Falcon XR6 FG X") - the same two levels
       the Excel picker offers, using the same names.</li>
   <li><b>trend</b> is the median for that model on each day it was captured.
       Each sparkline is scaled to its own range, so compare shapes, not
       heights.</li>
   <li><span class="flag">mix changed</span> means the number of listings
       roughly doubled or halved between those two captures, so the medians
       describe <i>different stock</i>. A broad sweep that adds cheap trims
       drags the median down and looks like a crash - it isn't one.</li>
   <li><b>Biggest movers</b> prefers <span class="flag info">same cars</span>:
       the median over vehicles priced on <i>both</i> days, which is a real
       price move. <span class="flag info">asking mix</span> means no cars
       could be matched, so it is only the medians of whatever was listed.</li>
   <li><b>Trims captured once or twice</b> cannot show a trend. That is thin
       coverage, not lost data - the rows are all still there.</li>
   <li><b>ad IDs in the latest capture</b>: pasted text carries no ad ID, so
       those listings can never be matched to a later sighting. Save the page
       with <code>Ctrl+S</code> when you care about same-car movement.</li>
  </ul></details>
 <details><summary>Closing the app</summary>
  <p>Click <b>Shut down</b> (top right), then close this tab. There is no
  console window to find - the server runs quietly in the background until you
  shut it down (or you sign out / restart Windows).</p></details>
</div>

<!-- Data check leads the page, ABOVE the dataset panels.
     It is the thing you want immediately after committing rows, and it used
     to be the last card on a long page. -->
<div class="card vfcard">
 <div class="ptitle" style="margin:0">data check
    <span id="vfspan">&mdash; not run yet &middot; compares the database with itself and with the workbook</span></div>
 <div id="verify"></div>
</div>

<div class="card">
 <div class="ptitle">dataset at a glance <span id="ovspan"></span></div>
 <div class="tiles" id="tiles"></div>
</div>

<div class="card">
 <div class="act">
  <div class="panel">
   <div class="ptitle">capture days <span>&mdash; last 26 weeks</span></div>
   <div id="heat" title="prices recorded per capture day"></div>
   <span class="heatlegend">less <div></div><div class="h1"></div><div class="h2"></div><div class="h3"></div><div class="h4"></div> more <em id="heatpeak" style="font-style:normal;margin-left:6px"></em></span>
  </div>
  <div class="panel">
   <div class="ptitle">total collected over time <span id="charttotal"></span></div>
   <div id="chart"></div>
  </div>
 </div>
</div>

<div class="card">
 <div class="hdr2">
  <div class="ptitle">top models <span id="topspan"></span></div>
  <div class="seg"><button id="lvLine" class="on">Model lines</button
   ><button id="lvTrim">Trims</button></div>
 </div>
 <table class="ov" id="topTbl" style="margin-top:2px"><thead><tr>
  <th>model</th><th class="num">prices</th><th class="num">days</th>
  <th class="num">median now</th><th>trend</th><th class="num">vs first</th>
  <th class="num">vs previous</th><th>range</th>
 </tr></thead><tbody></tbody></table>
 <div class="hint" id="tophint"></div>
</div>

<div class="card">
 <div class="act">
  <div class="panel">
   <div class="ptitle">biggest movers <span>&mdash; latest two captures of each model</span></div>
   <div class="lst" id="movers"></div>
  </div>
  <div class="panel">
   <div class="ptitle">latest capture <span id="latestday"></span></div>
   <div class="lst" id="latest"></div>
  </div>
  <div class="panel">
   <div class="ptitle">data health</div>
   <div class="lst" id="health"></div>
  </div>
 </div>
</div>

<script>
const $=id=>document.getElementById(id);
const localISO=d=>{const x=new Date(d);x.setMinutes(x.getMinutes()-x.getTimezoneOffset());
  return x.toISOString().slice(0,10)};
function drawHeatmap(){
  fetch("/api/heatmap").then(r=>r.json()).then(h=>{
    const grid=$("heat"), today=new Date();
    grid.innerHTML="";
    const start=new Date(today); start.setDate(start.getDate()-181);
    start.setDate(start.getDate()-((start.getDay()+6)%7));   // back to Monday
    // auto-scale: shade relative to the busiest day actually in view, so
    // only the biggest day reaches the darkest green (fixed thresholds made
    // almost every capture max out)
    const inView=[];
    for(let d=new Date(start); d<=today; d.setDate(d.getDate()+1)){
      const n=h.days[localISO(d)]||0; if(n>0)inView.push(n);
    }
    const peak=inView.length?Math.max(...inView):0;
    for(let d=new Date(start); d<=today; d.setDate(d.getDate()+1)){
      const key=localISO(d), n=h.days[key]||0;
      const cell=document.createElement("div");
      if(n>0){
        const f=n/peak;
        cell.className=f>0.75?"h4":f>0.5?"h3":f>0.25?"h2":"h1";
      }
      cell.title=n?`${key}: ${n} price(s)`:`${key}: nothing captured`;
      grid.appendChild(cell);
    }
    $("heatpeak").textContent=peak?`busiest day ${peak.toLocaleString()}`:"";
    drawChart(h.days);
  });
}

// cumulative prices collected, one point per capture day
function drawChart(days){
  const el=$("chart"), keys=Object.keys(days).sort();
  if(!keys.length){el.innerHTML='<div class="sub">nothing collected yet</div>';return;}
  let run=0;
  const pts=keys.map(k=>{run+=days[k];return{d:k,v:run};});
  const W=620,H=168,L=52,R=10,T=12,B=24,n=pts.length,max=run||1;
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const y=v=>T+(H-T-B)*(1-v/max);
  const path=pts.map((p,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const grid=vpaTicks(0,max,4).map(v=>{return `
      <line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#edeff3"/>
      <text x="${L-7}" y="${(y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${v.toLocaleString()}</text>`;}).join("");
  const dots=pts.map((p,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3"
      fill="#2e7d32"><title>${p.d}: ${p.v.toLocaleString()} total (+${days[p.d]} that day)</title></circle>`).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="cumulative prices collected over time">${grid}
    <path d="${path}L${x(n-1).toFixed(1)},${y(0).toFixed(1)}L${x(0).toFixed(1)},${y(0).toFixed(1)}Z"
          fill="#2e7d32" opacity=".09"/>
    <path d="${path}" fill="none" stroke="#2e7d32" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round"/>${dots}
    <text x="${L}" y="${H-6}" class="ax">${pts[0].d}</text>
    <text x="${W-R}" y="${H-6}" text-anchor="end" class="ax">${pts[n-1].d}</text></svg>`;
  $("charttotal").textContent=`— ${run.toLocaleString()} prices across ${n} capture days`;
}
/* ---------------------------------------------------------- overview */
const money=v=>"$"+Math.round(v).toLocaleString();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function pct(v){
  if(v==null)return '<span class="flat">&mdash;</span>';
  const c=v>0.05?"up":(v<-0.05?"down":"flat");
  return `<span class="${c}">${v>0?"+":""}${v.toFixed(1)}%</span>`;
}
// median-per-capture-day sparkline; scaled to its own min/max so shape reads
// even when two models are orders of magnitude apart in price
function spark(pts){
  const W=104,H=24,P=3;
  if(pts.length<2)return '<span class="flat" style="font-size:11px">one capture</span>';
  const vs=pts.map(p=>p.v),lo=Math.min(...vs),hi=Math.max(...vs),rng=(hi-lo)||1;
  const x=i=>P+i*(W-2*P)/(pts.length-1), y=v=>P+(H-2*P)*(1-(v-lo)/rng);
  const d=pts.map((p,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const last=pts[pts.length-1], col=last.v>=pts[0].v?"#2e7d32":"#a03030";
  const tip=pts.map(p=>`${p.d}: ${money(p.v)} (n=${p.n})`).join("\n");
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="median price per capture day"><title>${esc(tip)}</title>
    <path d="${d}" fill="none" stroke="${col}" stroke-width="1.6"
     stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(pts.length-1).toFixed(1)}" cy="${y(last.v).toFixed(1)}" r="2.4"
     fill="${col}"/></svg>`;
}

let ovLevel="line";
function loadOverview(){
  fetch("/api/overview?level="+ovLevel).then(r=>r.json()).then(o=>{
    if(!o.ok)return;
    const t=o.totals;
    const src=Object.entries(t.sources||{}).sort((a,b)=>b[1]-a[1])
      .map(([k,v])=>`${k} ${Number(v).toLocaleString()}`).join(" · ");
    $("ovspan").textContent=o.empty?"— nothing imported yet"
      :`— ${t.first} to ${t.last}`+(src?` · ${src}`:"");
    $("tiles").innerHTML=[
      [t.vehicles.toLocaleString(),"vehicles tracked"],
      [t.observations.toLocaleString(),"price observations"],
      [t.capture_days,"capture days"],
      [o.model_count||0,ovLevel==="trim"?"trims with prices":"model lines"],
      [(o.health.tracked||0).toLocaleString(),"re-seen on a later day"],
      [t.ingests,"import batches"],
    ].map(([v,l])=>`<div class="tile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");

    // ---- top models
    const tb=$("topTbl").querySelector("tbody");
    tb.innerHTML=o.models.map(m=>{
      const MIX='<span class="tag warn" title="the number of listings roughly doubled or halved between those two captures, so this compares different stock - not a price move">mix</span>';
      const q=new URLSearchParams({make:m.make,model:m.model,variant:m.variant||""});
      return `<tr>
        <td class="nm"><a class="ext" href="/explore?${q}"
          title="open this ${m.variant?"trim":"model"} in Explore">${esc(m.name)}</a></td>
        <td class="num">${m.listings.toLocaleString()}</td>
        <td class="num">${m.days}</td>
        <td class="num">${money(m.latest)}</td>
        <td>${spark(m.spark)}</td>
        <td class="num">${m.days>1?pct(m.overall)+(m.mix_overall?MIX:""):'<span class="flat">&mdash;</span>'}</td>
        <td class="num">${pct(m.step)}${m.mix?MIX:""}</td>
        <td class="flat">${money(m.lo)}–${money(m.hi)}</td></tr>`;}).join("")
      ||'<tr><td colspan="8" class="flat">nothing to show yet</td></tr>';
    $("topspan").textContent=o.model_count>o.models.length
      ? `— ${o.models.length} of ${o.model_count} by number of prices` : "";
    $("tophint").innerHTML=o.thin_count
      ? `<span title="e.g. ${esc(o.thin.slice(0,3).map(t2=>t2.name).join(", "))}">`
        +`${o.thin_count} trims have only one or two captures, so no trend yet.</span>`
      : "";

    // ---- movers
    $("movers").innerHTML=o.movers.map(m=>{
      const tag=m.basis==="same cars"
        ? `<span class="tag good" title="the same ${m.n_matched} cars priced on both days">same cars</span>`
        : '<span class="tag" title="medians of whatever was listed on each day; different cars may be in each">asking mix</span>';
      return `<div><span class="nm" title="${esc(m.name)}">${esc(m.name)}</span>
        <span>${pct(m.step)}</span>${tag}
        <span class="flat">${money(m.from)}→${money(m.to)}</span></div>`;}).join("")
      ||'<div class="flat">no model has two comparable captures yet</div>';

    // ---- latest capture
    if(o.latest){
      $("latestday").textContent="— "+o.latest.day;
      $("latest").innerHTML=
        `<div><span class="nm">prices recorded</span><b>${o.latest.rows.toLocaleString()}</b></div>`
       +`<div><span class="nm">first seen that day</span><b>${o.latest.new_vehicles.toLocaleString()}</b></div>`
       +o.latest.models.map(m=>`<div><span class="nm">${esc(m.name)}</span><span class="flat">${m.n}</span></div>`).join("");
    }

    // ---- data health
    const h=o.health, bar=(lbl,v,inv)=>`<div><span class="nm">${lbl}</span>
      <span class="${inv?(v>25?"down":"flat"):(v>75?"up":"flat")}">${v}%</span></div>
      <div class="bar"><i style="width:${Math.min(100,v)}%"></i></div>`;
    $("health").innerHTML=
      bar("missing odometer",h.no_odometer,true)
     +bar("missing year",h.no_year,true)
     +(h.brief?bar("quick-add rows (price only)",h.brief,true):"")
     +`<div><span class="nm">ad IDs in the latest capture</span>
        <span class="${h.ids_latest>0?"up":"down"}">${h.ids_latest}%</span></div>`
     +(h.ids_latest===0?'<div class="hint">That capture was pasted text, so those listings carry no ad ID and can never be matched to a later sighting. Save the page with Ctrl+S instead when you want same-car price movement.</div>':"");
  });
}
$("lvLine").onclick=()=>{ovLevel="line";$("lvLine").classList.add("on");
  $("lvTrim").classList.remove("on");loadOverview();};
$("lvTrim").onclick=()=>{ovLevel="trim";$("lvTrim").classList.add("on");
  $("lvLine").classList.remove("on");loadOverview();};

// The web pages recompute from the database on every request, so they are
// never stale. viewer.xlsx is the exception - it is a file, frozen at the
// moment it was built - so say so rather than letting it drift silently.
function refreshStatus(){return vpaStatus();}

// Data check: every row here is two answers compared, not one answer
// restated, so a PASS is evidence. Run on demand - it reads the workbook,
// which is far heavier than the polls.
const VMARK={pass:["&#10003;","#2e7d32"],warn:["!","#a06010"],
             fail:["&#10007;","#a03030"],skip:["&ndash;","#8a92a2"]};
async function runVerify(){
  const b=$("vfBtn");b.disabled=true;b.classList.add("busy");
  try{
    const v=await fetch("/api/verify").then(r=>r.json());
    const c=v.counts;
    $("vfspan").innerHTML=`&mdash; ${c.pass} passed`
      +(c.warn?`, <b style="color:#a06010">${c.warn} to look at</b>`:"")
      +(c.fail?`, <b style="color:#a03030">${c.fail} failed</b>`:"")
      +(c.skip?`, ${c.skip} skipped`:"")+` &middot; ${v.checked_at}`;
    const cov=v.coverage||{};
    $("verify").innerHTML=`<div class="vf">`+v.checks.map(k=>{
      const [gl,col]=VMARK[k.status];
      return `<div><span class="mk" style="color:${col}">${gl}</span>
        <span class="bd"><b>${esc(k.name)}</b><br>
          <span class="flat">${esc(k.detail)}</span>
          ${k.status!=="pass"&&k.status!=="skip"&&k.why
            ?`<br><span class="flat"><i>${esc(k.why)}</i></span>`:""}
          ${k.fix?`<br><span class="flat">fix: ${esc(k.fix)}</span>`:""}</span></div>`;
    }).join("")+`</div>`
      +(cov.observations?`<div class="hint">${cov.observations.toLocaleString()}
        observations over ${cov.capture_days} capture days &middot;
        ${cov.priced_pct}% priced &middot; ${cov.odometer_pct}% with an odometer
        &middot; ${cov.full_pct}% full detail &middot; ${cov.reseen_pct}% of
        vehicles seen more than once</div>`:"");
  }catch(e){$("verify").innerHTML=`<div class="flat">check failed: ${esc(e)}</div>`;}
  b.disabled=false;b.classList.remove("busy");
}
$("vfBtn").onclick=()=>{
  // the button is up in the nav, so bring its results into view
  vpaReveal(document.getElementById("verify").closest(".card"));
  runVerify();};

drawHeatmap();
loadOverview();
refreshStatus();

/* Two buttons, one rebuild. `open` also launches Excel; `build` does not,
   which is what you want when the only goal is to clear the stale-workbook
   banner or leave a current file behind before shutting down. */
// same background build as the header's Excel viewer button, minus opening
$("rebuildBtn").onclick=()=>vpaWorkbook(false);
__CHROME_JS__
</script></div></body></html>
"""

from .ui import action as _action, chrome as _chrome   # noqa: E402

#: Assembled after the templates exist. Import is one of the three modes of
#: Add data; the Overview page owns the data check and the workbook rebuild.
PAGE = _chrome(_PAGE, "add", mode="import")
OVERVIEW = _chrome(_OVERVIEW, "overview",
                   _action("vfBtn", "Data check", "check",
                           "cross-check the database against itself and the workbook")
                   + _action("rebuildBtn", "Rebuild workbook", "rebuild",
                             "rebuild viewer.xlsx from the database WITHOUT opening "
                             "it - use this to clear the stale-workbook flag, or "
                             "before closing the app"))


# MUST stay the last statement in the file. `main()` blocks in serve_forever(),
# so anything below it never executes: with this block above the PAGE
# assignment, `python -m vpa.importui` served every page except "/", which
# died on `NameError: PAGE`. The CLI path (`python -m vpa import-ui`) imports
# this module instead of running it, completes the body, and hid the fault.
if __name__ == "__main__":
    raise SystemExit(main())
