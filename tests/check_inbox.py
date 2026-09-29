"""Watch folder (vpa/inbox.py): saved pages that import themselves.

    python -m tests.check_inbox      (from vehicle-price-analysis/)

A real session against a throwaway database and a throwaway folder, with
files written the way a browser writes them - including the same file name
saved over and over, which is how the page is actually used:

  * a file already in the folder when the session starts is NOT imported;
  * a file still being written is not read until it stops changing;
  * each saved page is imported through bulk.run (it appears in Recent runs)
    and moved to imported/<date>/, so the same name can be saved again with
    no "replace?" prompt;
  * the same page saved twice is skipped; a bot-check page is refused and
    does not count; both still leave the folder;
  * one backup for the whole session, recorded on every page's run;
  * pace and time left are worked out from the gaps between saves;
  * the session stops itself at the page count and imports nothing after;
  * "Undo session" leaves the database exactly as it was before;
  * folders that must never be watched are refused.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from vpa import inbox
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "inbox_work"
DB = WORK / "inbox_test.db"
FOLDER = WORK / "carsales inbox"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def page(n: int) -> str:
    """One results 'page' of three cars, as a CSV the real adapter reads."""
    head = "listing_id,make,model,variant,year,price,odometer,state,seller_type"
    rows = [f"SSE-AD-{n * 10 + i},Ford,Falcon,XR6,2013,{15000 + n * 1000 + i * 100},"
            f"{120000 + i * 777},NSW,private" for i in range(3)]
    return head + "\n" + "\n".join(rows) + "\n"


def save(name: str, text: str, when: float | None = None):
    p = FOLDER / name
    p.write_text(text, encoding="utf-8")
    if when is not None:
        os.utime(p, (when, when))


def wait_for(fn, timeout=20.0):
    t = time.time()
    while time.time() - t < timeout:
        if fn():
            return True
        time.sleep(0.1)
    return False


def db_rows():
    with sqlite3.connect(DB) as c:
        return (c.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0])


def content_hash():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT key, capture_date, price_aud, odometer_km FROM observations "
                         "ORDER BY key, capture_date").fetchall()
        veh = c.execute("SELECT * FROM vehicles ORDER BY key").fetchall()
    return hashlib.sha256(repr((rows, veh)).encode()).hexdigest()[:16]


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    FOLDER.mkdir(parents=True)
    with Store(DB) as store:                      # a database with one car in it
        from vpa.schema import make_record
        store.ingest([make_record({"listing_id": "SSE-AD-1", "make": "Ford",
                                   "model": "Falcon", "variant": "XR6", "year": 2012,
                                   "price": 14000, "odometer": 150000,
                                   "capture_date": "2026-05-01"}, source="carsales")],
                     source_file="(seed)", adapter="test")
    before = content_hash()
    backups_before = len(list((WORK / "backups").glob("*.db"))) if (WORK / "backups").exists() else 0

    print("\n== refusing folders that must never be watched ==")
    bad = [inbox.start(DB, str(DB.parent), 5),
           inbox.start(DB, "relative\\folder", 5),
           inbox.start(DB, str(FOLDER), 0),
           inbox.start(DB, str(Path(DB.anchor)), 5)]
    check("the data folder, a relative path, 0 pages and a drive root are refused",
          all(r["ok"] is False and r["error"] for r in bad),
          " | ".join(r["error"][:40] for r in bad))

    print("\n== a session ==")
    save("old leftover.html", page(99))            # there before we start
    r = inbox.start(DB, str(FOLDER), 3, "test sweep")
    s = inbox._CUR["s"]
    check("a session starts, takes one backup, and leaves an existing file alone",
          r["ok"] and s.backup and Path(s.backup).is_file()
          and r["session"]["ignored"] == ["old leftover.html"],
          f"backup {Path(s.backup).name}, ignored {r['session']['ignored']}")

    t0 = time.time() - 120
    # page 1 - written in two halves, like a browser still saving it
    (FOLDER / "carsales.csv").write_text(page(1)[:40], encoding="utf-8")
    time.sleep(0.5)
    check("a file still being written is not read yet",
          s.done == 0 and not s.events, f"done={s.done}")
    save("carsales.csv", page(1), when=t0)          # ...and now it is finished
    ok1 = wait_for(lambda: s.done == 1)
    arch = list((FOLDER / "imported").rglob("*carsales.csv"))
    check("the saved page is imported, then moved out of the folder",
          ok1 and not (FOLDER / "carsales.csv").exists() and len(arch) == 1,
          f"done={s.done}, archived as {arch[0].relative_to(FOLDER) if arch else None}")
    ev = s.events[-1]
    check("...through a normal bulk run you could undo on its own",
          ev["kind"] == "imported" and ev["run"] and ev["obs_new"] == 3,
          f"run #{ev['run']}, {ev['obs_new']} new sightings")

    # the SAME name again - no replace prompt because the folder is empty
    save("carsales.csv", page(1), when=t0 + 20)     # identical page re-saved
    wait_for(lambda: len(s.events) >= 2)
    check("the same page saved twice is skipped, and still leaves the folder",
          s.events[-1]["kind"] == "duplicate" and s.done == 1
          and not (FOLDER / "carsales.csv").exists(),
          s.events[-1]["error"][:60])

    save("carsales.html", "<html><head><title>Cars</title></head><body>"
         "<script src='https://ct.captcha-delivery.com/c.js'></script></body></html>",
         when=t0 + 25)
    wait_for(lambda: len(s.events) >= 3)
    check("a bot-check page is refused, does not count, and leaves the folder",
          s.events[-1]["kind"] == "refused" and s.done == 1
          and not (FOLDER / "carsales.html").exists(),
          "refused: " + s.events[-1]["error"][:50].replace("<", "&lt;"))

    save("carsales.csv", page(2), when=t0 + 30)
    wait_for(lambda: s.done == 2)
    st = inbox.state(DB)["session"]
    check("pace and time left come from the gaps between saves",
          st["pace_s"] == 30 and st["eta_s"] is not None and st["finish"],
          f"{st['pace_s']} s a page, about {st['eta_s']} s left, done ~{st['finish']}")

    save("carsales.csv", page(3), when=t0 + 60)
    wait_for(lambda: s.status == "complete")
    time.sleep(1.2)
    save("carsales.csv", page(4), when=t0 + 90)      # after the count is reached
    time.sleep(1.8)
    st = inbox.state(DB)["session"]
    check("it stops by itself at the page count and imports nothing more",
          st["status"] == "complete" and st["done"] == 3
          and (FOLDER / "carsales.csv").exists(),
          f"status {st['status']}, {st['done']} of {st['target']}; the 4th save is left in the folder")
    with sqlite3.connect(DB) as c:
        runs = c.execute("SELECT id, backup_path FROM bulk_runs").fetchall()
    check("each page is its own run, all naming the one session backup",
          len(runs) == 3 and {b for _, b in runs} == {s.backup},
          f"{len(runs)} runs, backups named: {len({b for _, b in runs})}")
    n_backups = len(list((WORK / "backups").glob("*.db"))) - backups_before
    check("one backup for the whole session, not one per page",
          n_backups == 1, f"{n_backups} backup file(s) made")
    check("the file already there at the start was never imported",
          (FOLDER / "old leftover.html").exists()
          and db_rows()[0] == 1 + 9, f"observations now {db_rows()[0]} (1 seed + 3 pages x 3)")

    print("\n== undo the session ==")
    u = inbox.undo_session(DB)
    check("undo session takes every page back out, newest first",
          u["ok"] and len(u["undone"]) == 3 and u["undone"] == sorted(u["undone"], reverse=True),
          f"undone runs {u['undone']}")
    check("...leaving the cars and prices exactly as before the session",
          content_hash() == before and db_rows() == (1, 1),
          f"content hash {content_hash()} == {before}")

    print("\n== a second session, stopped by hand ==")
    (FOLDER / "carsales.csv").unlink()
    r2 = inbox.start(DB, str(FOLDER), 5)
    again = inbox.start(DB, str(FOLDER), 5)
    check("only one session at a time", r2["ok"] and again["ok"] is False,
          again.get("error", ""))
    inbox.stop()
    save("carsales.csv", page(7))
    time.sleep(1.5)
    check("after Stop, a new save is left alone",
          inbox._CUR["s"].done == 0 and (FOLDER / "carsales.csv").exists(),
          inbox.state(DB)["session"]["status"])
    check("the folder and page count are remembered for next time",
          inbox.load_settings(DB)["folder"] == str(FOLDER)
          and inbox.load_settings(DB)["pages"] == 5, str(inbox.load_settings(DB)))

    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
