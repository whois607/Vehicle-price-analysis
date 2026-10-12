"""Saves that went wrong (vpa/bulk.py cut_off / the repeat check, the
preview import, the watch folder).

    python -m tests.check_savewarn      (from vehicle-price-analysis/)

What happened on 2026-10-09, on a poor connection: a results page was saved
twice (the copy became "page 13" and the real next page was never saved),
and a download stalled at 640 KB and was imported as the last page - prices
right, body type / state / seller missing. Now:

  * a saved web page that stops partway is refused with the reason, on
    every import path, and writes nothing - and no complete page is ever
    taken for a cut-off one;
  * a page whose every car is already recorded for its day is skipped -
    no run, nothing written - with a warning that the next page may have
    been skipped; a page with even one new sighting still imports;
  * in the watch folder neither counts towards the page total, so the
    session keeps waiting for the real pages.

Throwaway database and folder only. ASCII output; exit 0 only if every check
passes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from vpa import bulk, importui, inbox
from vpa.store import Store
from tests.check_carsales import flat, ld, tiles

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "savewarn_work"
DB = WORK / "savewarn_test.db"
FOLDER = WORK / "carsales inbox"
RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def html(cars, pad: int = 0) -> str:
    """A carsales results page, as saved. `pad` puts filler where the rest
    of a real page sits, so cutting it off leaves the listings readable."""
    return ("<!DOCTYPE html><html><head><title>Ford Falcon cars for sale - carsales.com.au</title>"
            "</head><body>" + ld(cars) + tiles(cars) + "".join(flat(c, state="NSW") for c in cars)
            + "<div>" + "x" * pad + "</div></body></html>\n")


def cars(*ids):
    return [{"id": f"SSE-AD-{i}", "price": 20000 + i * 10, "km": 100000 + i * 100} for i in ids]


def state():
    with sqlite3.connect(DB) as c:
        obs = c.execute("SELECT key, capture_date, price_aud, odometer_km FROM observations "
                        "ORDER BY 1, 2").fetchall()
        veh = c.execute("SELECT * FROM vehicles ORDER BY key").fetchall()
        runs = c.execute("SELECT COUNT(*) FROM bulk_runs").fetchone()[0]
        logs = c.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0]
    return hashlib.sha256(repr((obs, veh)).encode()).hexdigest()[:12], len(obs), runs, logs


def run(*files):
    return bulk.run([{"name": n, "text": t, "capture_date": d} for n, t, d in files], DB)


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    FOLDER.mkdir(parents=True)
    with Store(DB):
        pass
    p1, p2 = html(cars(1, 2, 3)), html(cars(4, 5, 6))

    print("\n== a page cut off mid-download ==")
    full = html(cars(7, 8, 9), pad=20000)
    cut = full[:len(full) // 2]
    check("the check flags the cut copy and passes the whole page and a CSV",
          bulk.cut_off(cut, "x.htm") and bulk.cut_off(full, "x.htm") is None
          and bulk.cut_off("listing_id,price\nA,1", "x.csv") is None
          and bulk.cut_off(full.replace("</html>", "</HTML >"), "x.htm") is None,
          (bulk.cut_off(cut, "x.htm") or "")[:60])
    s0 = state()
    r = run(("Ford Falcon page.htm", cut, "2026-10-09"))
    f = r["files"][0]
    check("Bulk upload refuses it with the reason and writes nothing",
          r["wrote"] is False and f["ok"] is False and f["warn"] == "cutoff"
          and "cut off" in f["error"] and state() == s0, f["error"][:70])
    pv = importui.parse_payload({"name": "Ford Falcon page.htm", "text": cut,
                                 "capture_date": "2026-10-09"}, DB)
    check("Import with preview refuses it the same way",
          pv["ok"] is False and pv.get("cutoff") and "cut off" in pv["error"], pv["error"][:50])
    r = run(("Ford Falcon page.htm", full, "2026-10-09"))
    check("...and the complete save of the same page imports normally",
          r["wrote"] and r["files"][0]["obs_new"] == 3, f"{r['files'][0]['obs_new']} sightings")

    print("\n== a page saved twice ==")
    r = run(("page1.htm", p1, "2026-10-09"))
    s1 = state()
    r = run(("page1 again.htm", p1, "2026-10-09"))
    f = r["files"][0]
    check("the same cars again on the same day: skipped, no run, nothing written",
          r["wrote"] is False and r["run"] is None and f["warn"] == "repeat"
          and "already recorded for 2026-10-09" in f["error"] and "skipped" in f["error"]
          and state() == s1, f["error"][:80])
    pv = importui.parse_payload({"name": "page1 again.htm", "text": p1,
                                 "capture_date": "2026-10-09"}, DB)
    check("Import with preview says so, with every row unticked",
          pv["ok"] and "already recorded" in (pv["note"] or "")
          and not any(x["checked"] for x in pv["rows"]), (pv["note"] or "")[:60])
    r = run(("page1 next day.htm", p1, "2026-10-10"))
    check("the same page on a NEW day is a new sweep and imports",
          r["wrote"] and r["files"][0]["obs_new"] == 3, f"{r['files'][0]['obs_new']} sightings")
    mixed = html(cars(1, 2, 10))
    r = run(("page shifted.htm", mixed, "2026-10-09"))
    check("a page with even one new car imports (pages shift between saves)",
          r["wrote"] and r["files"][0]["obs_new"] == 1 and r["files"][0]["duplicates"] == 2,
          f"{r['files'][0]['obs_new']} new, {r['files'][0]['duplicates']} already there")
    s2 = state()
    r = run(("p2.htm", p2, "2026-10-09"), ("p2 copy.htm", p2, "2026-10-09"))
    fs = {x["name"]: x for x in r["files"]}
    with sqlite3.connect(DB) as c:
        batches = c.execute("SELECT batches FROM bulk_runs WHERE id = ?", (r["run"],)).fetchone()[0]
    check("dropped twice in ONE upload: the first imports, the copy is skipped",
          r["wrote"] and fs["p2.htm"]["ok"] and fs["p2 copy.htm"]["warn"] == "repeat"
          and len(json.loads(batches)) == 1 and state()[1] == s2[1] + 3,
          f"run {r['run']}, batches {batches}")

    print("\n== the watch folder ==")
    def page_csv(n):
        return ("listing_id,make,model,variant,year,price,odometer,state,seller_type\n"
                + "\n".join(f"SSE-AD-{900 + n * 10 + i},Ford,Falcon,XR6,2013,{15000 + n * 100 + i},"
                            f"{120000 + i},NSW,private" for i in range(3)) + "\n")

    def save(name, text):
        (FOLDER / name).write_text(text, encoding="utf-8")

    def wait(fn, t=20):
        end = time.time() + t
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.1)
        return False

    st = inbox.start(DB, str(FOLDER), 3)
    assert st["ok"], st
    S = inbox._CUR["s"]
    save("carsales.csv", page_csv(1))
    wait(lambda: S.done == 1 and not (FOLDER / "carsales.csv").exists())
    # the same cars, saved again - different bytes (so not caught as an
    # identical file), same listings
    # the file leaves the folder a moment BEFORE its result is recorded
    # (inbox._take archives, then appends), so wait for the result itself
    n = len(S.events)
    save("carsales.csv", page_csv(1).replace("\n", "\r\n"))
    wait(lambda: len(S.events) > n)
    ev = S.events[-1]
    check("a page saved twice is not counted, and the page says the next may be missing",
          S.done == 1 and ev["kind"] == "refused" and ev["result"]["files"][0]["warn"] == "repeat",
          f"done {S.done}, {ev['kind']}: {ev['error'][:50]}")
    big = html([{"id": "SSE-AD-777", "price": 20000, "km": 90000}], pad=40000)
    n = len(S.events)
    save("carsales.htm", big[:len(big) // 2])
    wait(lambda: len(S.events) > n)
    ev = S.events[-1]
    check("a cut-off page is refused and not counted",
          S.done == 1 and ev["kind"] == "refused" and ev["result"]["files"][0]["warn"] == "cutoff",
          f"done {S.done}, {ev['error'][:50]}")
    save("carsales.htm", big)
    wait(lambda: S.done == 2)
    save("carsales.csv", page_csv(3))
    wait(lambda: S.done == 3)
    check("...so the session waits for, and takes, the real pages",
          S.done == 3 and S.status == "complete", f"{S.done} of 3, {S.status}")
    inbox.stop()

    with sqlite3.connect(DB) as c:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    check("the database is intact", ok == "ok", ok)
    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
