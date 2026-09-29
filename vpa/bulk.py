"""Direct upload — many files, straight into the database, no preview.

    served at /bulk by importui's server

The Import page is preview-first on purpose: it parses, shows you every
normalised row with its flags, and writes nothing until you commit. That is
the right shape for a page you have not seen before, and the wrong shape for
the twentieth weekly capture of a trim whose parsing you have already checked
twenty times. This page is for the second case: drop forty files, they land.

------------------------------------------------------- what is NOT skipped

Skipping the human check makes the machine checks matter MORE, not less, so
nothing below is optional and none of it is a judgement call:

1. **A backup before the run.** One `Store.backup()` before the first write,
   named in the result and kept in the run log. This was listed in NOTES §6
   as "automatic backup before each ingest" and never done; a page that
   writes without showing you anything first is where it stops being
   optional.
2. **Files that are not listings are refused, not imported.** A saved
   bot-protection interstitial (`adapters.challenge`) parses to zero rows and
   would otherwise be reported as an empty page. A file that yields no
   records at all is reported per-file with its name, never silently counted
   as a success.
3. **Every run is reversible.** `undo()` deletes exactly the observations the
   run's ingest batches created, restores every vehicle that existed before
   it, and deletes only the vehicles the run itself introduced.

What is skipped is the row-by-row eyeball: no preview table, no ticking, no
per-row edits. The normalisation is identical — the same `parse_file`, the
same `make_record`, the same `Store.ingest` the Import page and the CLI use.
A row that would have been flagged in the preview still gets flagged here;
it is counted and reported afterwards instead of before.

--------------------------------------------------------- the honest limits

* **Per-file commits, not one transaction.** `Store.ingest()` commits itself,
  and it is the core write path shared with the CLI and the Import page —
  wrapping it in an outer transaction would mean changing it, which is the
  one piece of code in this project least worth risking for a convenience
  page. So if file 7 of 10 fails, files 1-6 are already in. That is
  deliberate and it is why the run log records batch ids as it goes: a
  half-finished run is still completely undoable, and the work already done
  is not thrown away.
* **Undo cannot un-fill a gap.** `Store.ingest()` fills NULL vehicle columns
  with `COALESCE` and never blanks one, so a run that supplied a missing
  `variant` for a vehicle that already existed has changed that vehicle. Undo
  restores those columns from the snapshot this module takes BEFORE writing,
  so in practice it does put them back — the snapshot is the reason this is a
  limit on paper rather than in fact. What it genuinely cannot restore is a
  vehicle row another run has since edited; undo reports that instead of
  overwriting it.
* **Nothing here is a scraper.** Same as everywhere else in this project: it
  reads files you already have. Dropping forty of them at once does not
  change where they came from.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from .adapters import parse_file
from .adapters.challenge import explain as _challenge
from . import links
from .store import DEFAULT_DB, Store

#: columns of `vehicles` snapshotted before a run, so undo can put a
#: pre-existing vehicle back exactly. `key` is the identity and is never
#: restored - it is what the row is found BY.
_VEHICLE_COLS = ("source", "listing_id", "vehicle_type", "make", "model",
                 "variant", "year", "body_type", "state", "seller_type",
                 "raw_title", "url", "first_seen", "last_seen")

_SUFFIXES = {"csv": ".csv", "json": ".json", "html": ".html", "text": ".txt"}

#: files in one run. Not a technical limit - a guard against a mis-drop of a
#: whole Downloads folder writing four hundred files before anyone notices.
MAX_FILES = 200


class _Rich(ValueError):
    """A refusal whose message is HTML we wrote ourselves, not a raw error.

    Only `adapters.challenge.explain()` raises it. Everything else is plain
    text and is escaped by the page, so a filename containing a `<` cannot
    become markup.
    """


def _parse_one(name: str, text: str, source=None, fmt=None, capture_date=None):
    """One file -> (records, format, raw_count) or raise ValueError(reason).

    Identical call sequence to `importui.parse_payload`, deliberately: a file
    must not parse differently depending on which page it was dropped on.
    """
    if not (text or "").strip():
        raise ValueError("the file is empty")
    blocked = _challenge(text, name)
    if blocked:
        # `explain()` returns rich HTML - it has to, it is several paragraphs
        # telling you how to re-save the page. Flagged rather than sniffed at
        # the other end: the browser must not have to guess which of these
        # strings is markup and which is a raw exception message.
        raise _Rich(blocked)
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
    if not records:
        raise ValueError(
            "no listings found in this file"
            + (" - Facebook Marketplace saved pages are empty shells; use the "
               "paste box in Add data > Import with preview instead" if used_fmt == "html"
               else ""))
    return records, used_fmt, raw_count


def _snapshot(store: Store, keys) -> dict:
    """The current state of every named vehicle that ALREADY exists.

    Taken before the first write. Keys not present are simply absent, which
    is how undo knows the run introduced them and they should be deleted
    rather than restored.

    Chunked rather than one query per key: a full run can carry tens of
    thousands of records, and a per-key SELECT turns a snapshot into the
    slowest part of the upload. 500 is well inside SQLite's variable limit.
    """
    out = {}
    keys = list(keys)
    cols = ", ".join(_VEHICLE_COLS)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in store.conn.execute(
                f"SELECT key, {cols} FROM vehicles WHERE key IN ({marks})",
                chunk):
            out[row["key"]] = {c: row[c] for c in _VEHICLE_COLS}
    return out


def run(files: list[dict], db_path: str | Path = DEFAULT_DB,
        source: str | None = None, fmt: str | None = None,
        label: str = "", backup: str | None = None) -> dict:
    """Parse and commit every file, with no preview and no confirmation.

    `files` is [{name, text, capture_date}] - the same shape the Import page
    already sends, so the browser side is unchanged.

    Returns a report with one row per file (including the ones that failed
    and why), the totals, the backup path, and the run id needed to undo it.

    `backup` names a backup ALREADY taken before this run (the watch folder
    takes one when its session starts, then imports page by page); the run
    records it instead of copying the whole database again. Omitted, a fresh
    backup is taken here, as always.
    """
    if not files:
        return {"ok": False, "error": "no files - drop some first"}
    if len(files) > MAX_FILES:
        return {"ok": False, "error":
                f"{len(files)} files at once; the limit is {MAX_FILES}. That "
                f"many is usually a whole folder dropped by mistake, so it "
                f"asks rather than writing them."}

    # ---- Parse EVERYTHING first, and only then write. Parsing is pure, so
    # doing it up front costs nothing and means a run that is going to fail
    # on a bot-protection page fails before it has touched the database.
    parsed, reports = [], []
    for f in files:
        name = str(f.get("name") or "pasted.txt")
        try:
            records, used_fmt, raw = _parse_one(
                name, f.get("text") or "", source=source, fmt=fmt,
                capture_date=(f.get("capture_date") or None))
        except ValueError as exc:
            reports.append({"name": name, "ok": False, "error": str(exc),
                            "html": isinstance(exc, _Rich),
                            "records": 0, "obs_new": 0, "vehicles_new": 0,
                            "duplicates": 0})
            continue
        except Exception as exc:                      # a malformed file
            reports.append({"name": name, "ok": False, "html": False,
                            "error": f"could not be read: {exc}",
                            "records": 0, "obs_new": 0, "vehicles_new": 0,
                            "duplicates": 0})
            continue
        parsed.append({"name": name, "records": records, "format": used_fmt,
                       "raw": raw})

    if not parsed:
        return {"ok": True, "wrote": False, "run": None, "backup": None,
                "files": reports, "totals": _totals(reports), "cars": [],
                "reason": "nothing in these files could be read as listings, "
                          "so the database was not touched"}

    # ---- one backup for the whole run, before the first write
    with Store(db_path) as store:
        if not backup or not Path(backup).is_file():
            backup = str(store.backup())
        keys = {r.dedupe_key() for p in parsed for r in p["records"]}
        before = _snapshot(store, keys)

        batches, totals = [], {"rows_parsed": 0, "vehicles_new": 0,
                               "obs_new": 0, "obs_duplicate": 0}
        # every car read, in the order the files were dropped and the order
        # each file lists them - the page keeps a running list per session.
        # Returned to the page only; never stored in bulk_runs.
        cars, seen = [], set()
        for p in parsed:
            # the ingest_log row this ingest is about to create; ingest()
            # returns counts but not its own batch id, and the run must be
            # able to name its batches to undo them
            before_max = store.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM ingest_log").fetchone()[0]
            counts = store.ingest(p["records"],
                                  source_file=f"(bulk) {p['name']}",
                                  adapter="bulk")
            new_batches = [r[0] for r in store.conn.execute(
                "SELECT id FROM ingest_log WHERE id > ?", (before_max,))]
            batches.extend(new_batches)
            for k in totals:
                totals[k] += counts[k]
            for r in p["records"]:
                k = r.dedupe_key()
                cars.append({
                    "file": p["name"], "key": k, "year": r.year,
                    "make": r.make, "model": r.model, "variant": r.variant,
                    "title": r.raw_title, "price": r.price_aud,
                    "odometer_km": r.odometer_km,
                    "capture_date": str(r.capture_date or ""),
                    "url": links.ad_url(r.url, r.source),
                    # already in the database before this run, or earlier
                    # in this same run
                    "known": k in before or k in seen})
                seen.add(k)
            flagged = sum(1 for r in p["records"]
                          if r.price_aud is None or r.odometer_km is None
                          or r.year is None)
            reports.append({
                "name": p["name"], "ok": True, "error": "",
                "format": p["format"], "records": len(p["records"]),
                "dropped": max(0, p["raw"] - len(p["records"])),
                "obs_new": counts["obs_new"],
                "vehicles_new": counts["vehicles_new"],
                "duplicates": counts["obs_duplicate"],
                "incomplete": flagged,
                "days": sorted({r.capture_date for r in p["records"]}),
            })

        cur = store.conn.execute(
            """INSERT INTO bulk_runs (ts, label, backup_path, batches, before,
               files, rows_parsed, vehicles_new, obs_new, obs_duplicate)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(sep=" ", timespec="seconds"),
             label or f"{len(parsed)} file(s)", backup,
             json.dumps(batches), json.dumps(before), json.dumps(reports),
             totals["rows_parsed"], totals["vehicles_new"],
             totals["obs_new"], totals["obs_duplicate"]))
        store.conn.commit()
        run_id = cur.lastrowid

    # keep the per-file rows in the order they were dropped
    order = {str(f.get("name") or "pasted.txt"): i for i, f in enumerate(files)}
    reports.sort(key=lambda r: order.get(r["name"], 0))
    return {"ok": True, "wrote": True, "run": run_id, "backup": backup,
            "files": reports, "totals": _totals(reports),
            "cars": cars, "reason": ""}


def _totals(reports: list[dict]) -> dict:
    return {
        "files": len(reports),
        "failed": sum(1 for r in reports if not r["ok"]),
        "records": sum(r.get("records", 0) for r in reports),
        "obs_new": sum(r.get("obs_new", 0) for r in reports),
        "vehicles_new": sum(r.get("vehicles_new", 0) for r in reports),
        "duplicates": sum(r.get("duplicates", 0) for r in reports),
        "incomplete": sum(r.get("incomplete", 0) for r in reports),
    }


def history(db_path: str | Path = DEFAULT_DB, limit: int = 25) -> list[dict]:
    """Recent runs, newest first, each with what it wrote and whether it
    has been undone."""
    with Store(db_path) as store:
        rows = store.conn.execute(
            """SELECT id, ts, label, backup_path, rows_parsed, vehicles_new,
               obs_new, obs_duplicate, undone_at, files FROM bulk_runs
               ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            files = json.loads(d.pop("files") or "[]")
        except ValueError:
            files = []
        d["file_count"] = len(files)
        d["failed"] = sum(1 for f in files if not f.get("ok"))
        out.append(d)
    return out


def undo(db_path: str | Path = DEFAULT_DB, run_id: int = 0) -> dict:
    """Remove exactly what one run added.

    The same protocol as every other write in this project (repair / merge /
    edit): backup, BEGIN IMMEDIATE, invariants checked before COMMIT, full
    ROLLBACK on any failure.

    Three things happen, in this order, and the order matters:

      1. the run's observations are deleted, named by their ingest batch -
         never by date or by vehicle, either of which would also catch rows
         some OTHER import contributed on the same day;
      2. vehicles left with no observations at all are deleted. Those are
         exactly the vehicles the run introduced: a vehicle that existed
         before still has the sightings it had before;
      3. every vehicle that DID exist before is restored from the snapshot
         taken before the run, which puts back any column the ingest filled
         in with COALESCE.
    """
    with Store(db_path) as store:
        conn = store.conn
        row = conn.execute("SELECT * FROM bulk_runs WHERE id = ?",
                           (run_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"no upload run #{run_id}"}
        if row["undone_at"]:
            return {"ok": False, "error":
                    f"run #{run_id} was already undone on {row['undone_at']}"}
        batches = json.loads(row["batches"] or "[]")
        before = json.loads(row["before"] or "{}")
        if not batches:
            return {"ok": False, "error":
                    f"run #{run_id} recorded no ingest batches, so there is "
                    f"nothing it can be proved to have written"}

        backup = str(store.backup())
        marks = ",".join("?" * len(batches))
        expect_obs = conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE batch_id IN ({marks})",
            batches).fetchone()[0]
        total_before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        # The keys this run actually wrote to. Everything below is scoped to
        # these: "delete vehicles with no observations left" over the WHOLE
        # table would also delete any pre-existing orphan this run never
        # touched, which is someone else's bug to find, not this one's to
        # silently clean up.
        run_keys = [r[0] for r in conn.execute(
            f"SELECT DISTINCT key FROM observations WHERE batch_id IN ({marks})",
            batches)]

        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f"DELETE FROM observations WHERE batch_id IN ({marks})", batches)
            gone_veh = 0
            if run_keys:
                kmarks = ",".join("?" * len(run_keys))
                cur = conn.execute(
                    f"""DELETE FROM vehicles WHERE key IN ({kmarks})
                        AND key NOT IN (SELECT DISTINCT key FROM observations)""",
                    run_keys)
                gone_veh = cur.rowcount
            restored = 0
            for key, cols in before.items():
                # only restore a vehicle that is still here; one deleted by a
                # later merge or edit is not this run's to resurrect
                if not conn.execute("SELECT 1 FROM vehicles WHERE key = ?",
                                    (key,)).fetchone():
                    continue
                sets = ", ".join(f"{c} = ?" for c in _VEHICLE_COLS)
                conn.execute(f"UPDATE vehicles SET {sets} WHERE key = ?",
                             [cols.get(c) for c in _VEHICLE_COLS] + [key])
                restored += 1

            # ---- invariants, before COMMIT
            left = conn.execute(
                f"SELECT COUNT(*) FROM observations WHERE batch_id IN ({marks})",
                batches).fetchone()[0]
            if left:
                raise RuntimeError(f"{left} observation(s) from this run survived")
            orphans = conn.execute(
                """SELECT COUNT(*) FROM observations o
                   LEFT JOIN vehicles v ON v.key = o.key
                   WHERE v.key IS NULL""").fetchone()[0]
            if orphans:
                raise RuntimeError(f"{orphans} observation(s) left with no vehicle")
            total_after = conn.execute(
                "SELECT COUNT(*) FROM observations").fetchone()[0]
            if total_before - total_after != expect_obs:
                raise RuntimeError(
                    f"expected to remove {expect_obs} observation(s), "
                    f"removed {total_before - total_after}")
            conn.execute(
                "UPDATE bulk_runs SET undone_at = ? WHERE id = ?",
                (datetime.now().isoformat(sep=" ", timespec="seconds"), run_id))
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": f"undo rolled back: {exc}",
                    "backup": backup}
        finally:
            conn.isolation_level = ""

        return {"ok": True, "run": run_id, "backup": backup,
                "observations_removed": expect_obs,
                "vehicles_removed": gone_veh,
                "vehicles_restored": restored}


# ------------------------------------------------------------------ the api
def run_payload(payload: dict, db_path: str | Path) -> dict:
    """POST /api/bulk/run — the whole upload, in one request."""
    files = payload.get("files") or []
    if not isinstance(files, list):
        return {"ok": False, "error": "bad request: files must be a list"}
    return run(files, db_path,
               source=(payload.get("source") or None),
               fmt=(payload.get("format") or None),
               label=(payload.get("label") or ""))


# -------------------------------------------------------------------- page
#
# LAYOUT RULE, and it is the whole reason this page does not simply reuse the
# Import page's markup: every element is where it will be at the moment the
# page first paints, and stays there. Concretely -
#
#   * no panel is ever display:none and then shown. Anything that has nothing
#     to say yet is present, sized, and empty;
#   * the drop zone, the status strip, the totals row and the results list all
#     have a FIXED height. Results scroll inside their own box rather than
#     making the page taller, so the buttons do not travel down the screen as
#     files land;
#   * button labels never change. "Upload" stays "Upload" while it is
#     working - a label that becomes "uploading 7/40..." is a button that
#     changes width, and a button that changes width moves its neighbours.
#     Progress goes to the status strip, which has room reserved for it;
#   * the two buttons have a fixed min-width, so even the disabled state
#     cannot reflow the row.
#
# The cost is a page that looks slightly empty before you drop anything. That
# is the intended trade: this page's job is to be clicked quickly and
# repeatedly, and a control that has moved since you last looked is how the
# wrong one gets clicked.
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>vpa add data - bulk</title>
<style>
__CHROME_CSS__
 /* ---- the fixed frame -------------------------------------------------
    Widths and heights here are load-bearing, not decoration: they are what
    stops the controls moving. Changing one to `auto` or `max-height` will
    reintroduce exactly the behaviour this page was asked to avoid. */
 #drop{height:104px;border:1.5px dashed var(--field);border-radius:12px;
   display:flex;align-items:center;justify-content:center;gap:12px;text-align:left;
   padding:0 18px;color:var(--dim);cursor:pointer;background:var(--head);font-size:13px;
   transition:background .15s,border-color .15s,color .15s}
 #drop b{color:var(--ink);font-weight:600}
 #drop .dropico{color:var(--accent)}
 #drop:hover{background:#f3f7fe;border-color:var(--accent-line);color:var(--ink-2)}
 #drop.hover{background:var(--accent-soft);border-color:var(--accent)}
 #droplabel{max-width:100%;overflow:hidden;text-overflow:ellipsis;
   display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
 .controls{display:flex;gap:10px;align-items:flex-end;margin-top:12px;
   min-height:56px}
 .controls .grow{flex:1;min-width:0}
 /* fixed widths: a button that resizes with its label moves its neighbour */
 #goBtn{min-width:132px}
 #clearBtn{min-width:96px}
 #undoBtn{min-width:132px}
 /* status: always present, always the same height, empty when idle */
 #status{height:38px;display:flex;align-items:center;gap:10px;margin-top:10px;
   padding:0 12px;border:1px solid var(--line-2);border-radius:var(--r-sm);
   background:var(--head);font-size:12.5px;color:var(--ink-2);overflow:hidden}
 #status.work{background:var(--accent-soft);border-color:var(--accent-line)}
 #status.good{background:var(--good-soft);border-color:var(--good-line)}
 #status.bad{background:var(--bad-soft);border-color:var(--bad-line);color:#7a2f2f}
 #statustext{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
   white-space:nowrap}
 .bar{width:150px;height:6px;border-radius:3px;background:#dfe4ec;flex:none;
   overflow:hidden}
 .bar i{display:block;height:100%;width:0;background:var(--accent);
   transition:width .15s linear}
 /* totals: six slots, always six, showing a dash before there is a number */
 .tiles{grid-template-columns:repeat(6,1fr)}
 .tile{height:62px;overflow:hidden}
 /* the results box scrolls INSIDE itself - this is what keeps the page,
    and therefore every button on it, exactly as tall as it started */
 #resultsBox{height:290px;overflow:auto;border:1px solid var(--line);
   border-radius:var(--r-sm);background:#fff}
 #resultsBox th,#runsBox th{position:sticky;top:0;z-index:1}
 #resultsBox td{vertical-align:top}
 #runsBox td{vertical-align:middle;padding-top:4px;padding-bottom:4px}
 td.num{white-space:nowrap}
 tr.bad td{background:#fdf7f7}
 /* a refusal explains itself in full - it can afford to, because the box
    it lives in scrolls rather than growing the page */
 .why{margin-top:3px;font-size:11.5px;color:var(--warn);max-width:460px}
 .empty{text-align:left;padding:14px;font-size:12.5px}
 /* ---- watch a folder: fixed heights, like the rest of the page ---- */
 .wrow{margin-top:0}
 .wrow2{margin-top:10px}
 /* ---- split screen / narrow windows ---- */
 .controls{flex-wrap:wrap}
 #runtbl td{white-space:nowrap}
 #runtbl td:nth-child(2){max-width:220px;overflow:hidden;text-overflow:ellipsis}
 @media (max-width:980px){
   .tiles{grid-template-columns:repeat(3,1fr)}
   #rtbl .c-f,#rtbl td:nth-child(6){display:none}
   #runtbl th:nth-child(3),#runtbl td:nth-child(3){display:none}}
 @media (max-width:760px){
   #rtbl .c-d,#rtbl td:nth-child(5){display:none}
   #rtbl .c-k{width:92px}#rtbl .c-p{width:88px}
   #runtbl th:nth-child(5),#runtbl td:nth-child(5){display:none}
   #wstatus .bar{width:90px}}
 #wstatus{height:44px;display:flex;align-items:center;gap:12px;margin-top:12px;
   padding:0 14px;border:1px solid var(--line-2);border-radius:var(--r-sm);
   background:var(--head);font-size:13px;color:var(--ink-2);overflow:hidden}
 #wstatus .wdot{width:9px;height:9px;border-radius:50%;background:var(--faint);flex:none}
 #wstatus.on{background:var(--accent-soft);border-color:var(--accent-line)}
 #wstatus.on .wdot{background:var(--accent);animation:wpulse 1.4s ease-in-out infinite}
 #wstatus.good{background:var(--good-soft);border-color:var(--good-line)}
 #wstatus.good .wdot{background:var(--good)}
 #wstatus.bad{background:var(--bad-soft);border-color:var(--bad-line);color:#7a2f2f}
 #wstatus.bad .wdot{background:var(--bad)}
 @keyframes wpulse{0%,100%{box-shadow:0 0 0 0 rgba(43,95,196,.45)}50%{box-shadow:0 0 0 5px rgba(43,95,196,0)}}
 #wtext{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .wnums{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--dim);font-size:12.5px}
 .wnums b{color:var(--ink);font-size:15px;font-weight:650}
 #wstatus .bar{width:170px}
 .wfoot{display:flex;align-items:center;gap:12px;height:30px;margin-top:10px}
 #wnote{flex:1;min-width:0;font-size:12px;color:var(--dim);white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 #wUndo{min-width:120px}
 #resultsEmpty{height:100%;display:flex;align-items:center;justify-content:center;
   color:var(--faint);font-size:13px}
 /* A backup path is 90-odd characters and wraps to a second line the moment
    a run finishes - which grew this card and pushed the panel below it down
    the page. One line, clipped, with the full path on hover. */
 #backuphint{height:18px;line-height:18px;margin-top:10px;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 #runsBox{height:170px;overflow:auto;border:1px solid var(--line);
   border-radius:var(--r-sm);background:#fff}
 #runsBox button{min-width:96px}
 /* the session list: fixed columns and one line per car, so appending a
    hundred cars only ever lengthens the scroll inside the box */
 #sessCard{position:relative}
 /* text that changes with every upload is ONE line, clipped - a count that
    grows by a digit must never wrap and push the page down (it did, at
    narrow widths: the title went to two lines and everything below moved) */
 #sessCard>.ptitle{height:20px;line-height:20px;white-space:nowrap;overflow:hidden;
   text-overflow:ellipsis}
 .pagehead .sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #rtbl{table-layout:fixed}
 #rtbl td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #rtbl .c-n{width:48px}#rtbl .c-p{width:100px}#rtbl .c-k{width:104px}
 #rtbl .c-d{width:100px}#rtbl .c-f{width:190px}#rtbl .c-s{width:124px}
 #rtbl .c-a{width:86px}
 #rtbl tr.drop td:first-child{box-shadow:inset 3px 0 0 #e39a2b}
 #rtbl tr.drop .pr{color:var(--warn);font-weight:600}
 #rtbl tr.undone td{color:var(--faint);text-decoration:line-through}
 #rtbl tr.bad td{white-space:normal}
 /* The price-order flag floats over the card's top-right corner: it can
    appear and be dismissed without moving a single element under it. */
 #orderflag{position:absolute;top:10px;right:14px;z-index:3;max-width:min(620px,72%);
   display:flex;gap:10px;align-items:center;background:var(--warn-soft);
   border:1px solid var(--warn-line);color:var(--warn);border-radius:var(--r-sm);
   padding:5px 6px 5px 12px;font-size:12px;box-shadow:0 3px 10px rgba(120,80,10,.14)}
 #orderflag[hidden]{display:none}
 #orderflag .msg{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 #orderflag button.x{flex:none;background:#fff3de;color:var(--warn)}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Add data</h1>
 <div class="sub">No preview &mdash; files go straight in. Backed up first, and every run can be undone.</div>
</div>__ACTIONS__</div>
__MODES__

<div class="card" id="help" style="display:none">
 <p><b>This page does not show you the rows before it writes them.</b> That is
 the whole point of it: for a make, model or trim whose parsing you have
 already checked with <b>Import with preview</b>, dropping forty saved
 searches and having them land is the job. If you have not seen this kind of
 file work before, use <b>Import with preview</b> instead - it shows every row
 and writes nothing until you say so.</p>
 <ul style="font-size:13px">
  <li><b>The parsing is identical.</b> Same adapters, same normalisation, same
   duplicate rules, same <code>Store.ingest</code> as Import with preview and
   the command line. Only the preview step is missing.</li>
  <li><b>A backup is taken before the run</b> - one for the whole run, before
   the first write, and the file is named in the result.</li>
  <li><b>Each file keeps its own capture date</b>, taken from when you saved
   it. Forty pages saved across three evenings are three capture days, not
   one.</li>
  <li><b>Files that are not listings are refused</b>, not imported: a saved
   bot-protection page, an empty file, a Facebook shell with no listings in
   it. Each one is named in the results with the reason.</li>
  <li><b>Any run can be undone in full</b> - it removes exactly the sightings
   that run added, deletes only the vehicles it introduced, and puts back
   every vehicle that already existed. Recent runs are listed at the bottom
   of this page.</li>
  <li><b>Nothing moves on this page.</b> Every panel is where it will stay
   before you drop anything, results scroll inside their own box, and the
   buttons keep one label and one width, so the button under your cursor is
   the button you meant.</li>
 </ul>
</div>

<div class="card" id="watchCard">
 <div class="ptitle">watch a folder <span id="wspan">&mdash; save pages into it with Ctrl+S; each one imports itself</span></div>
 <div class="controls wrow">
  <div class="grow"><label for="wFolder">folder to save pages into</label>
   <input type="text" id="wFolder" spellcheck="false" style="width:100%"></div>
  <div><button id="wOpen" title="show this folder in Explorer">Open folder</button></div>
 </div>
 <div class="controls wrow2">
  <div style="width:96px"><label for="wPages">pages</label>
   <input type="number" id="wPages" min="1" max="500" value="20" style="width:100%"></div>
  <div class="grow"><label for="wLabel">label (optional)</label>
   <input type="text" id="wLabel" style="width:100%" placeholder="e.g. Falcon sweep"></div>
  <div><button class="primary" id="wStart">Start watching</button></div>
  <div><button id="wStop" disabled>Stop</button></div>
 </div>
 <div id="wstatus"><span class="wdot"></span><span id="wtext">not watching</span>
  <span class="wnums" id="wnums"></span><span class="bar"><i id="wbar"></i></span></div>
 <div class="wfoot"><span id="wnote">Each saved page is imported, then moved into
  <code>imported\</code> so the next save needs no new name. One backup is taken when you start.</span>
  <button class="danger mini" id="wUndo" disabled title="undo every page this session imported">Undo session</button></div>
</div>

<div class="card">
 <div class="ptitle">files <span>&mdash; each keeps its own capture date</span></div>
 <div id="drop"><svg class="ico dropico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V4m0 0 4 4m-4-4-4 4M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg><span id="droplabel"><b>Drop saved pages, CSV or JSON</b> or click to choose</span></div>
 <input type="file" id="file" multiple style="display:none"
   accept=".csv,.json,.html,.htm,.txt">
 <div class="controls">
  <div style="width:150px"><label for="source">source</label>
   <select id="source" style="width:100%">
    <option value="">detect from file</option>
    <option value="carsales">carsales</option>
    <option value="bikesales">bikesales</option>
    <option value="facebook">facebook</option>
    <option value="gumtree">gumtree</option>
    <option value="other">other</option>
   </select></div>
  <div style="width:150px"><label for="format">format</label>
   <select id="format" style="width:100%">
    <option value="">detect from file</option>
    <option value="html">saved HTML</option>
    <option value="csv">CSV</option>
    <option value="json">JSON</option>
    <option value="text">plain text</option>
   </select></div>
  <div class="grow"></div>
  <div><button class="primary" id="goBtn" disabled>Upload</button></div>
  <div><button id="clearBtn" disabled>Clear</button></div>
 </div>
 <div id="status"><span id="statustext">ready</span>
  <span class="bar"><i id="barfill"></i></span></div>
</div>

<div class="card" id="sessCard">
 <div class="ptitle">this session <span id="runspan">&mdash; every car uploaded since this page opened</span></div>
 <div id="orderflag" role="status" hidden><span class="msg" id="ordermsg"></span>
  <button class="x" id="orderX" title="dismiss - it comes back if another car arrives out of order">&times;</button></div>
 <div class="tiles" id="tiles"></div>
 <div id="resultsBox" style="margin-top:11px">
  <div class="empty" id="resultsEmpty">Nothing uploaded yet &mdash; cars appear here as they arrive.</div>
  <table id="rtbl" style="display:none"><thead><tr>
   <th class="num c-n">#</th><th>car</th><th class="num c-p">price</th>
   <th class="num c-k">odometer</th><th class="c-d">captured</th>
   <th class="c-f">file</th><th class="c-a">advert</th><th class="c-s">status</th>
  </tr></thead><tbody></tbody></table>
 </div>
 <div class="hint" id="backuphint">A backup is taken before every run.</div>
</div>

<div class="card">
 <div class="ptitle">recent runs <span>&mdash; each can be undone in full</span></div>
 <div id="runsBox">
  <div class="empty" id="runsEmpty">No direct uploads yet.</div>
  <table id="runtbl" style="display:none"><thead><tr>
   <th>when</th><th>label</th><th class="num">files</th>
   <th class="num">sightings</th><th class="num">new cars</th>
   <th>state</th><th style="width:150px"></th>
  </tr></thead><tbody></tbody></table>
 </div>
 <div class="hint note">Undo removes exactly the sightings that run added, deletes
  only the cars it introduced, and restores every car that already existed.
  It takes its own backup first and rolls back completely if anything about
  the result does not add up.</div>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",
  ">":"&gt;",'"':"&quot;"}[c]));
let pending=[], RUN=null, busy=false;

/* Local calendar date, not toISOString - that is UTC, and a file saved at
   9pm in Sydney would be filed under the previous day. */
function localISO(ms){const d=new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;}

/* The ONLY thing that ever changes in the status strip is its text, its
   class and the bar width - never its height, and never the buttons. */
function say(text,kind,pct){
  $("statustext").textContent=text;
  $("status").className=kind||"";
  $("barfill").style.width=(pct==null?0:pct)+"%";
}
function tiles(t){
  const cells=t&&t.files?[
    [t.files,"files"],[t.records,"listings read"],
    [t.obs_new,"new sightings"],[t.vehicles_new,"cars not seen before"],
    [t.duplicates,"already had"],[t.incomplete,"missing a field",t.incomplete>0],
  ]:[["-","files"],["-","listings read"],["-","new sightings"],
     ["-","cars not seen before"],["-","already had"],["-","missing a field"]];
  $("tiles").innerHTML=cells.map(([v,l,warn])=>
    `<div class="tile${warn?" warn":""}"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
}
tiles(null);                       // six tiles exist before anything happens

function takeFiles(list){
  const files=[...list];
  if(!files.length)return;
  Promise.all(files.map(f=>f.text().then(t=>({
    name:f.name,text:t,size:f.size,
    capture_date:f.lastModified?localISO(f.lastModified):null}))))
   .then(loaded=>{
     pending=pending.concat(loaded);
     const kb=pending.reduce((a,f)=>a+f.size,0)/1024;
     $("droplabel").textContent=pending.length===1
       ?`${pending[0].name} (${kb.toFixed(1)} kB) - ready to upload`
       :`${pending.length} files (${(kb/1024).toFixed(1)} MB) - ready to upload`;
     $("goBtn").disabled=false;$("clearBtn").disabled=false;
     say(`${pending.length} file(s) waiting. Upload writes them straight in.`,"");
     // clear the input so re-picking the same file fires 'change' again
     $("file").value="";});
}
$("drop").onclick=()=>{if(!busy)$("file").click();};
$("file").onchange=e=>takeFiles(e.target.files);
["dragover","dragenter"].forEach(ev=>$("drop").addEventListener(ev,e=>{
  e.preventDefault();if(!busy)$("drop").classList.add("hover");}));
["dragleave","drop"].forEach(ev=>$("drop").addEventListener(ev,e=>{
  e.preventDefault();$("drop").classList.remove("hover");}));
$("drop").addEventListener("drop",e=>{if(!busy)takeFiles(e.dataTransfer.files);});

$("clearBtn").onclick=()=>{
  pending=[];$("file").value="";
  $("droplabel").innerHTML="<b>Drop saved pages, CSV or JSON</b> or click to choose";
  $("goBtn").disabled=true;$("clearBtn").disabled=true;
  say("ready","");
};

$("goBtn").onclick=async()=>{
  if(!pending.length||busy)return;
  busy=true;
  // Buttons are DISABLED, never relabelled and never removed: a disabled
  // button occupies exactly the space it did enabled.
  $("goBtn").disabled=true;$("clearBtn").disabled=true;
  const n=pending.length;
  say(`uploading ${n} file(s) - writing to the database...`,"work",35);
  let res;
  try{
    res=await fetch("/api/bulk/run",{method:"POST",body:JSON.stringify({
      files:pending,source:$("source").value,format:$("format").value,
      label:""})}).then(r=>r.json());
  }catch(e){res={ok:false,error:String(e)};}
  busy=false;$("clearBtn").disabled=false;
  if(!res.ok){say(res.error||"upload failed","bad",0);
    $("goBtn").disabled=pending.length===0;return;}
  render(res);
  pending=[];$("goBtn").disabled=true;
  $("droplabel").innerHTML="<b>Drop saved pages, CSV or JSON</b> or click to choose";
  loadRuns();
};

/* ---- the session list --------------------------------------------------
   Every car uploaded while this page is open, in the order it arrived:
   file by file in the order they were dropped, and within a file in the
   order the site listed them. Nothing here is stored - reloading the page
   starts a new session. An undone run stays in the list, struck through,
   and stops counting. */
let SESSION=[];                    // cars and refused files, in arrival order
let RUNTOTALS=[];                  // [{run, t, undone}] - one per upload
let DISMISSED=new Set();           // out-of-order cars you have already waved off
const money=v=>v==null?"—":"$"+Math.round(v).toLocaleString();
const kmf=v=>v==null?"—":Math.round(v).toLocaleString()+" km";
const adSite=u=>{try{return new URL(u).hostname.replace(/^www\./,"").split(".")[0];}
  catch(e){return "ad";}};
const carName=c=>[c.year,c.make,c.model,c.variant].filter(Boolean).join(" ")
  ||c.title||"(no title)";

function render(res){
  const run=res.run;
  // file by file, in drop order; a refused file keeps its place in the list
  res.files.forEach(f=>{
    if(!f.ok){SESSION.push({run,refused:true,file:f.name,error:f.error,html:f.html});return;}
    (res.cars||[]).filter(c=>c.file===f.name).forEach(c=>SESSION.push({...c,run}));
  });
  RUNTOTALS.push({run,t:res.totals,undone:false});
  renderSession();
  const box=$("resultsBox");box.scrollTop=box.scrollHeight;   // newest in view
  $("backuphint").innerHTML=res.backup
    ?`Backup taken before this run: <code>${esc(res.backup)}</code>`
    :"A backup is taken before every run.";
  $("backuphint").title=res.backup||"";     // the clipped path, in full
  const t=res.totals;
  const kind=t.failed?(t.obs_new?"work":"bad"):"good";
  say(res.wrote
      ?`run #${run}: ${t.obs_new} new sighting(s) from ${t.files-t.failed} file(s)`
       +(t.failed?`, ${t.failed} refused`:"")+". Backed up first."
      :(res.reason||"nothing was written"),kind,100);
}

function isUndone(c){return RUNTOTALS.some(r=>r.run===c.run&&r.undone);}
function renderSession(){
  // session totals, leaving out undone runs
  const live=RUNTOTALS.filter(r=>!r.undone);
  const sum=k=>live.reduce((a,r)=>a+(r.t[k]||0),0);
  tiles(live.length?{files:sum("files"),records:sum("records"),obs_new:sum("obs_new"),
    vehicles_new:sum("vehicles_new"),duplicates:sum("duplicates"),
    incomplete:sum("incomplete")}:null);
  const nLive=SESSION.filter(c=>!c.refused&&!isUndone(c)).length;
  $("runspan").textContent=RUNTOTALS.length
    ?`— ${nLive} car(s) from ${live.length} upload(s) since this page was opened`
    :"— every car uploaded since this page was opened";
  // Uploads are meant to arrive cheapest first. Each live, priced car is
  // compared with the live, priced car before it.
  let prev=null,prevN=0,n=0;const drops=[];
  const rows=SESSION.map(c=>{
    if(c.refused)return `<tr class="bad"><td class="num">—</td><td colspan="6">
      <b>${esc(c.file)}</b> refused<div class="why">${c.html?c.error:esc(c.error)}</div></td>
      <td><span class="tag warn">refused</span></td></tr>`;
    n++;
    const gone=isUndone(c);
    let drop=false;
    if(!gone&&c.price!=null){
      if(prev&&c.price<prev.price){drop=true;drops.push({c,prev,n,pn:prevN,id:c.run+":"+n});}
      prev=c;prevN=n;
    }
    return `<tr class="${gone?"undone":drop?"drop":""}" title="${esc(c.title||"")}">
      <td class="num">${n}</td><td>${esc(carName(c))}</td>
      <td class="num pr">${money(c.price)}</td><td class="num">${kmf(c.odometer_km)}</td>
      <td>${esc(c.capture_date||"")}</td><td title="${esc(c.file)}">${esc(c.file)}</td>
      <td>${c.url?`<a class="ext" href="${esc(c.url)}" target="_blank"
        rel="noreferrer noopener" title="the advert">${esc(adSite(c.url))} &#8599;</a>`:""}</td>
      <td>${gone?'<span class="tag">run undone</span>'
        :drop?`<span class="tag warn">cheaper than #${drops[drops.length-1].pn}</span>`
        :c.known?'<span class="tag">seen before</span>'
        :'<span class="tag good">new car</span>'}</td></tr>`;
  });
  const any=SESSION.length>0;
  $("resultsEmpty").style.display=any?"none":"";
  $("rtbl").style.display=any?"":"none";
  $("rtbl").querySelector("tbody").innerHTML=rows.join("");
  // The flag: shown while any out-of-order car has not been waved off.
  // Remembered per car, not as a count - after an undo removes one drop, a
  // count would wrongly treat a different, never-dismissed drop as seen.
  const fresh=drops.filter(d=>!DISMISSED.has(d.id));
  if(fresh.length){
    const d=fresh[fresh.length-1];
    $("ordermsg").innerHTML=`<b>Out of price order:</b> ${drops.length} car(s) came in
      cheaper than the car before - latest #${d.n} ${esc(carName(d.c))} at
      ${money(d.c.price)} after #${d.pn} at ${money(d.prev.price)}. Uploads should run cheapest first.`;
    $("ordermsg").title=$("ordermsg").textContent.replace(/\s+/g," ");
    $("orderflag").hidden=false;
  }else $("orderflag").hidden=true;
  FLAG_IDS=drops.map(d=>d.id);
}
let FLAG_IDS=[];
function markUndone(run){
  RUNTOTALS.forEach(r=>{if(r.run===run)r.undone=true;});
  renderSession();
}
$("orderX").onclick=()=>{FLAG_IDS.forEach(id=>DISMISSED.add(id));
  $("orderflag").hidden=true;};

async function loadRuns(){
  vpaStatus();                       // the header's vehicle count moves with every run
  let d;
  try{d=await fetch("/api/bulk/history").then(r=>r.json());}catch(e){return;}
  const rows=(d&&d.runs)||[];
  if(!rows.length){$("runsEmpty").style.display="";
    $("runtbl").style.display="none";return;}
  $("runsEmpty").style.display="none";$("runtbl").style.display="";
  $("runtbl").querySelector("tbody").innerHTML=rows.map(r=>`
   <tr><td>${esc(r.ts)}</td><td>${esc(r.label||"")}</td>
    <td class="num">${r.file_count}${r.failed?` <span class="tag warn">${r.failed} refused</span>`:""}</td>
    <td class="num">${r.obs_new}</td><td class="num">${r.vehicles_new}</td>
    <td>${r.undone_at?`<span class="tag">undone ${esc(r.undone_at)}</span>`
      :'<span class="tag good">in the database</span>'}</td>
    <td>${r.undone_at?"":`<button class="danger mini" data-run="${r.id}">Undo run</button>`}</td>
   </tr>`).join("");
  document.querySelectorAll("#runtbl button[data-run]").forEach(b=>b.onclick=()=>undoRun(b));
}

async function undoRun(btn){
  const id=btn.dataset.run;
  if(!confirm(`Undo upload run #${id}?\n\nThis removes exactly the sightings `
    +`that run added and deletes only the cars it introduced. A backup is `
    +`taken first.`))return;
  btn.disabled=true;                      // disabled, not relabelled
  say(`undoing run #${id}...`,"work",50);
  let res;
  try{res=await fetch("/api/bulk/undo",{method:"POST",
    body:JSON.stringify({run:Number(id)})}).then(r=>r.json());
  }catch(e){res={ok:false,error:String(e)};}
  if(!res.ok){say(res.error||"undo failed","bad",0);btn.disabled=false;return;}
  markUndone(Number(id));           // strike its cars through in the session list
  say(`run #${id} undone: ${res.observations_removed} sighting(s) removed, `
     +`${res.vehicles_removed} car(s) deleted, ${res.vehicles_restored} `
     +`restored. Backed up first.`,"good",100);
  loadRuns();
}

/* ---- watch a folder ----------------------------------------------------
   The server does the watching (vpa/inbox.py) and imports each page through
   the same bulk run the Upload button uses; this polls for what arrived and
   feeds it into the session list exactly as an upload would. */
let WSEQ=0, WPOLL=null, WSESSION=null, WRUNS=[], WHOLD=0;
const mmss=s=>s==null?"":s>=3600?`${Math.floor(s/3600)}h ${Math.round(s%3600/60)}m`
  :s>=60?`${Math.floor(s/60)}m ${String(Math.round(s%60)).padStart(2,"0")}s`:`${Math.round(s)}s`;
function wsay(text,kind,pct,nums){
  $("wtext").textContent=text;$("wtext").title=text;
  $("wstatus").className=kind||"";
  $("wbar").style.width=(pct==null?0:pct)+"%";
  $("wnums").innerHTML=nums||"";
}
function wshow(st){
  WSESSION=st;
  const on=st&&st.status==="watching";
  $("wStart").disabled=on;$("wStop").disabled=!on;
  ["wFolder","wPages","wLabel"].forEach(id=>$(id).disabled=on);
  $("wUndo").disabled=on||!(st&&st.runs&&st.runs.length);
  if(!st){wsay("not watching","",0);return;}
  const pct=Math.round(100*st.done/st.target);
  const nums=`<b>${st.done}</b> of ${st.target} pages`
    +(st.pace_s?` &middot; ${mmss(st.pace_s)} a page`:"")
    +(st.eta_s!=null?(st.eta_s<5?" &middot; last page any moment"
      :` &middot; about <b>${mmss(st.eta_s)}</b> left (done ~${esc(st.finish)})`):"");
  const extra=[st.refused?`${st.refused} refused`:"",st.duplicates?`${st.duplicates} repeat(s) skipped`:""]
    .filter(Boolean).join(", ");
  if(on){
    wsay(st.done?`Watching ${st.folder}`+(extra?` - ${extra}`:"")
      :`Watching ${st.folder} - save the first page into it`,"on",pct,nums);
  }else if(st.status==="complete"){
    wsay(`Done: ${st.reason}`+(extra?` (${extra})`:"")+` in ${mmss(st.elapsed_s)}.`,"good",100,nums);
  }else if(st.status==="error"){
    wsay(st.reason,"bad",pct,nums);
  }else{
    wsay(`Stopped (${st.reason}) after ${st.done} of ${st.target} pages.`,"",pct,nums);
  }
  const ign=(st.ignored||[]).length;
  if(Date.now()<WHOLD)return;            // a warning stays up long enough to read
  $("wnote").innerHTML=ign&&on
    ?`${ign} file(s) were already in the folder when you started and are left alone: <b>${esc(st.ignored.slice(0,3).join(", "))}</b>`
    :st.backup?`Backup taken at the start: <code>${esc(st.backup)}</code>`
    :$("wnote").innerHTML;
  $("wnote").title=st.backup||"";
}
function wtake(evs){
  let any=false;
  evs.forEach(ev=>{
    if(ev.result){render(ev.result);any=true;}
    if(ev.run&&!WRUNS.includes(ev.run))WRUNS.push(ev.run);
    if(ev.archive_error){$("wnote").innerHTML=`<b style="color:var(--warn)">${esc(ev.name)}</b>: ${esc(ev.archive_error)}`;
      WHOLD=Date.now()+15000;}
    WSEQ=Math.max(WSEQ,ev.seq);
  });
  if(any)loadRuns();
}
async function wpoll(){
  let d;
  try{d=await fetch("/api/inbox?since="+WSEQ).then(r=>r.json());}catch(e){return;}
  const st=d&&d.session;
  if(st){wtake(st.events||[]);wshow(st);}
  if(!st||st.status!=="watching"){clearInterval(WPOLL);WPOLL=null;}
}
function wstartPolling(){if(!WPOLL)WPOLL=setInterval(wpoll,700);}
$("wStart").onclick=async()=>{
  $("wStart").disabled=true;
  let r;
  try{r=await fetch("/api/inbox/start",{method:"POST",body:JSON.stringify({
    folder:$("wFolder").value,pages:$("wPages").value,label:$("wLabel").value})}).then(x=>x.json());
  }catch(e){r={ok:false,error:String(e)};}
  if(!r.ok){wsay(r.error||"could not start","bad",0);$("wStart").disabled=false;return;}
  WSEQ=0;WRUNS=[];wshow(r.session);wstartPolling();
};
$("wStop").onclick=async()=>{
  const r=await fetch("/api/inbox/stop",{method:"POST",body:"{}"}).then(x=>x.json());
  await wpoll();if(r.session)wshow(r.session);
};
$("wOpen").onclick=async()=>{
  const r=await fetch("/api/inbox/open",{method:"POST",body:"{}"}).then(x=>x.json());
  if(!r.ok)wsay(r.error,"bad",0);
};
$("wUndo").onclick=async()=>{
  const runs=(WSESSION&&WSESSION.runs)||[];
  if(!runs.length)return;
  if(!confirm(`Undo this watch session?\n\nThis undoes all ${runs.length} page(s) it imported `
    +`(runs #${runs[0]} to #${runs[runs.length-1]}), newest first - each one exactly as `
    +`Undo run would. A backup is taken first.`))return;
  $("wUndo").disabled=true;wsay("undoing the session...","on",50);
  let r;
  try{r=await fetch("/api/inbox/undo",{method:"POST",body:"{}"}).then(x=>x.json());}
  catch(e){r={ok:false,error:String(e)};}
  (r.undone||[]).forEach(id=>markUndone(Number(id)));
  loadRuns();
  if(!r.ok){wsay("undo stopped: "+(r.error||"failed"),"bad",0);return;}
  wsay(`Session undone: ${r.undone.length} page(s) taken back out. Backed up first.`,"good",100);
  WSESSION.runs=[];$("wUndo").disabled=true;
};
(async()=>{                        // settings, and any session already running
  let d;
  try{d=await fetch("/api/inbox").then(r=>r.json());}catch(e){return;}
  const s=d.settings||{};
  $("wFolder").value=s.folder||"";$("wPages").value=s.pages||20;$("wLabel").value=s.label||"";
  if(d.session){wtake(d.session.events||[]);wshow(d.session);
    if(d.session.status==="watching")wstartPolling();}
})();

loadRuns();
__CHROME_JS__
</script></div></body></html>
"""

from .ui import chrome as _chrome   # noqa: E402

PAGE = _chrome(_PAGE, "add", mode="bulk")
