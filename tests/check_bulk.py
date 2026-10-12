"""Direct upload: writing without a preview, and undoing it completely.

    python -m tests.check_bulk

Every other write in this project is preview-first or dry-run-first. This one
is not, by request, which moves the entire safety net to either side of the
write - a backup before, and a provable undo after. So the checks are mostly
about those two, plus the one property that makes the page safe to use at
speed: **the same file must produce the same rows here as on the Import
page.** A bulk path that normalised differently would be a second, quieter
importer, and the difference would only ever show up in the statistics.

Undo is the hard part and is tested against the cases that actually break it:

  * a car that ALREADY existed must survive the undo, keeping the sightings
    it had before, while a car the run introduced is deleted;
  * a column the run filled in on a pre-existing car (`Store.ingest` uses
    COALESCE and never blanks) must go back to NULL;
  * an unrelated vehicle with no observations - someone else's orphan - must
    NOT be swept up by "delete cars with nothing left";
  * a failed invariant must roll the whole thing back, leaving the file
    byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from vpa import bulk
from vpa.importui import parse_payload
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "bulk_test.db"
D1, D2 = "2026-05-04", "2026-05-11"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def csv_file(name, day, rows):
    """A CSV the real adapter reads, as the browser would send it."""
    head = "listing_id,make,model,variant,year,price,odometer,state,seller_type"
    body = "\n".join(",".join(str(c) for c in r) for r in rows)
    return {"name": name, "text": f"{head}\n{body}\n", "capture_date": day}


#: eight cars, first capture
FILE_A = csv_file("week1.csv", D1, [
    (f"EXA-AD-{700 + i}", "Ford", "Falcon", "XR6", 2013, 18000 + i * 500,
     120000 + i * 5000, "NSW", "private") for i in range(8)])
#: the same eight a week later (two repriced) plus two genuinely new cars
FILE_B = csv_file("week2.csv", D2,
                  [(f"EXA-AD-{700 + i}", "Ford", "Falcon", "XR6", 2013,
                    18000 + i * 500 - (1000 if i < 2 else 0),
                    120000 + i * 5000, "NSW", "private") for i in range(8)]
                  + [(f"EXA-AD-{900 + i}", "Ford", "Falcon", "XR6", 2014,
                      21000 + i * 500, 90000, "VIC", "dealer")
                     for i in range(2)])

#: a saved bot-protection interstitial, the shape the real ones take
CHALLENGE = {"name": "blocked.htm", "capture_date": D2, "text": (
    "<html><head><title>Ford cars for sale in Australia</title></head><body>"
    "<script src='https://ct.captcha-delivery.com/c.js'></script>"
    "</body></html>")}
EMPTY = {"name": "nothing.csv", "text": "", "capture_date": D2}


def fresh() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    Store(DB).close()


def counts() -> tuple[int, int]:
    with Store(DB) as s:
        return (s.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0],
                s.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])


def run_checks() -> int:
    print("\n== a run writes, with no preview and no confirmation ==")
    r1 = bulk.run([FILE_A], DB, label="week 1")
    v1, o1 = counts()
    check("one file lands in a single call, with no intermediate step",
          r1["ok"] and r1["wrote"] and (v1, o1) == (8, 8),
          f"{r1['totals']['obs_new']} sightings written, db now {v1} "
          f"vehicles / {o1} observations")
    check("a backup was taken BEFORE the write, and its path is reported",
          bool(r1["backup"]) and Path(r1["backup"]).exists(),
          Path(r1["backup"]).name)
    # The backup must predate the write, not merely exist: a copy taken
    # afterwards would restore the very state you are trying to undo.
    with Store(r1["backup"]) as b:
        bv = b.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
    check("the backup holds the state from BEFORE the run, not after",
          bv == 0, f"backup has {bv} vehicles, live database has {v1}")

    print("\n== the parsing is the Import page's, not a second one ==")
    # Same file through the preview path; the rows it would have committed
    # must be exactly the rows bulk wrote. This is the check that stops the
    # fast path quietly becoming a different importer.
    fresh()
    prev = parse_payload({"name": FILE_A["name"], "text": FILE_A["text"],
                          "capture_date": D1, "format": "csv"}, DB)
    r = bulk.run([FILE_A], DB)
    with Store(DB) as s:
        keys = {k for (k,) in s.conn.execute("SELECT key FROM vehicles")}
    check("bulk writes exactly the keys the preview would have shown",
          {row["key"] for row in prev["rows"]} == keys
          and len(prev["rows"]) == r["totals"]["records"],
          f"{len(prev['rows'])} preview rows, {len(keys)} keys written, "
          f"identical set: {({row['key'] for row in prev['rows']} == keys)}")

    print("\n== files that are not listings are refused, never imported ==")
    before = counts()
    r2 = bulk.run([CHALLENGE, EMPTY], DB)
    check("a bot-protection page and an empty file are both refused by name",
          counts() == before and r2["totals"]["failed"] == 2
          and not r2["wrote"],
          "; ".join(f"{f['name']}: {f['error'][:44]}" for f in r2["files"]))
    check("a run where nothing parsed does not touch the database at all",
          r2["run"] is None and r2["backup"] is None,
          f"no run recorded, no backup taken, db still {counts()}")

    print("\n== a mixed run writes the good files and names the bad ==")
    fresh()
    r3 = bulk.run([FILE_A, CHALLENGE, FILE_B], DB, label="mixed")
    ok_files = [f for f in r3["files"] if f["ok"]]
    check("good files land while the refused one is reported beside them",
          r3["wrote"] and len(ok_files) == 2 and r3["totals"]["failed"] == 1
          and counts() == (10, 18),
          f"{r3['totals']['obs_new']} sightings from {len(ok_files)} files, "
          f"1 refused, db {counts()}")
    check("the results keep the order the files were dropped in",
          [f["name"] for f in r3["files"]]
          == [FILE_A["name"], CHALLENGE["name"], FILE_B["name"]],
          " -> ".join(f["name"] for f in r3["files"]))
    check("each file reports its own capture date, not the run's",
          ok_files[0]["days"] == [D1] and ok_files[1]["days"] == [D2],
          f"{ok_files[0]['name']}={ok_files[0]['days']}, "
          f"{ok_files[1]['name']}={ok_files[1]['days']}")
    check("a re-seen car is counted as a sighting, not as a new car",
          ok_files[1]["records"] == 10 and ok_files[1]["vehicles_new"] == 2,
          f"week2: {ok_files[1]['records']} listings, only "
          f"{ok_files[1]['vehicles_new']} of them cars never seen before")

    print("\n== every car read comes back, in arrival order, for the session list ==")
    cars = r3.get("cars") or []
    got_ids = [c["price"] for c in cars]
    check("one entry per listing read, file by file in drop order",
          len(cars) == 18 and [c["file"] for c in cars]
          == [FILE_A["name"]] * 8 + [FILE_B["name"]] * 10,
          f"{len(cars)} cars; files {sorted({c['file'] for c in cars})}")
    check("...and within a file, in the order the file lists them",
          got_ids[:8] == [18000 + i * 500 for i in range(8)]
          and got_ids[8:10] == [17000, 17500],
          f"first prices {got_ids[:4]}, week2 starts {got_ids[8:11]}")
    check("'seen before' is true for a car already read earlier in the run",
          [c["known"] for c in cars] == [False] * 8 + [True] * 8 + [False] * 2,
          "week1 all new; week2 re-sightings known, its two new cars not")
    with Store(DB) as s:
        stored = s.conn.execute("SELECT files FROM bulk_runs WHERE id=?",
                                (r3["run"],)).fetchone()[0]
    check("the car list is returned, never stored with the run",
          '"cars"' not in stored and "EXA-AD-700" not in stored,
          f"bulk_runs.files is {len(stored)} chars of per-file summary only")
    # the same cars a week on: new sightings of cars already stored (the
    # identical file on its own day would now be skipped as a page saved
    # twice - tests/check_savewarn.py)
    r_pre = bulk.run([dict(FILE_A, name="week3.csv", capture_date="2026-05-18")], DB)
    check("a car already in the database before the run is 'seen before'",
          r_pre["cars"] and all(c["known"] for c in r_pre["cars"]),
          f"{sum(c['known'] for c in r_pre['cars'])}/{len(r_pre['cars'])} known")
    check("a run that wrote nothing returns an empty car list, not none",
          bulk.run([CHALLENGE], DB).get("cars") == [],
          "refused-only run: cars == []")

    print("\n== undo removes exactly what the second run added ==")
    # Set up the case undo actually has to get right: run 1 puts eight cars
    # in, run 2 re-sights those AND introduces two. Undoing run 2 must leave
    # the first eight with their first-week sightings intact.
    fresh()
    bulk.run([FILE_A], DB, label="week 1")
    before_v, before_o = counts()
    with Store(DB) as s:
        keep = {k for (k,) in s.conn.execute("SELECT key FROM vehicles")}
    r4 = bulk.run([FILE_B], DB, label="week 2")
    mid = counts()
    u = bulk.undo(DB, r4["run"])
    after = counts()
    with Store(DB) as s:
        still = {k for (k,) in s.conn.execute("SELECT key FROM vehicles")}
    check("the database returns to exactly the counts it had before the run",
          u["ok"] and after == (before_v, before_o),
          f"{(before_v, before_o)} -> {mid} -> {after}")
    check("cars that existed before survive; only the run's own are deleted",
          still == keep and u["vehicles_removed"] == 2,
          f"{len(still)} cars left, identical to the {len(keep)} before; "
          f"{u['vehicles_removed']} introduced by the run were removed")
    with Store(DB) as s:
        per_car = {k: s.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE key=?",
            (k,)).fetchone()[0] for k in still}
    check("the surviving cars keep the sightings they already had",
          bool(per_car) and set(per_car.values()) == {1},
          f"all {len(per_car)} pre-existing cars still have exactly their "
          f"week-1 observation")
    check("undo takes its own backup before touching anything",
          bool(u["backup"]) and Path(u["backup"]).exists(),
          Path(u["backup"]).name)
    check("an undone run cannot be undone twice",
          bulk.undo(DB, r4["run"])["ok"] is False,
          bulk.undo(DB, r4["run"])["error"][:60])

    print("\n== undo puts back a column the run filled in ==")
    # Store.ingest fills NULL vehicle columns with COALESCE and never blanks
    # one, so a run CHANGES a pre-existing car. Undo has to restore it, or
    # "removes exactly what the run added" is not true.
    fresh()
    thin = csv_file("thin.csv", D1,
                    [("EXA-AD-555", "Ford", "Falcon", "", 2013, 17000,
                      100000, "", "")])
    fat = csv_file("fat.csv", D2,
                   [("EXA-AD-555", "Ford", "Falcon", "XR6 Turbo", 2013, 17000,
                     100000, "QLD", "dealer")])
    bulk.run([thin], DB)
    with Store(DB) as s:
        was = dict(s.conn.execute(
            "SELECT variant, state, seller_type FROM vehicles").fetchone())
    r5 = bulk.run([fat], DB)
    with Store(DB) as s:
        during = dict(s.conn.execute(
            "SELECT variant, state, seller_type FROM vehicles").fetchone())
    bulk.undo(DB, r5["run"])
    with Store(DB) as s:
        now = dict(s.conn.execute(
            "SELECT variant, state, seller_type FROM vehicles").fetchone())
    check("a gap the run filled is empty again afterwards",
          was == now and during != was and during["state"] == "QLD",
          f"before={was} -> during={during} -> after={now}")

    print("\n== undo does not sweep up anything it did not put there ==")
    # A vehicle with no observations that the run never touched. "Delete
    # every car with nothing left" would take it; the delete is scoped to
    # the run's own keys precisely so it cannot.
    fresh()
    bulk.run([FILE_A], DB)
    with Store(DB) as s:
        s.conn.execute(
            """INSERT INTO vehicles (key, source, vehicle_type, make, model,
               first_seen, last_seen) VALUES
               ('ORPHAN','examplecars','car','Holden','Commodore',?,?)""",
            (D1, D1))
        s.conn.commit()
    r6 = bulk.run([FILE_B], DB)
    bulk.undo(DB, r6["run"])
    with Store(DB) as s:
        orphan = s.conn.execute(
            "SELECT COUNT(*) FROM vehicles WHERE key='ORPHAN'").fetchone()[0]
    check("an unrelated car with no sightings is left alone",
          orphan == 1,
          "the pre-existing orphan survived an undo that deleted 2 other cars")

    print("\n== a failure part-way through an undo rolls the whole thing back ==")
    # Undo deletes observations, then deletes the run's own cars, THEN
    # restores the pre-existing ones. A failure in that last step is the
    # dangerous one: the deletes have already happened. Forced here by
    # pointing the restore at a column that does not exist, which is the
    # only way to reach that path without corrupting the file first.
    fresh()
    bulk.run([FILE_A], DB)
    r_roll = bulk.run([FILE_B], DB)
    digest = sha(DB)
    before_counts = counts()
    real_cols = bulk._VEHICLE_COLS
    bulk._VEHICLE_COLS = real_cols + ("no_such_column",)
    try:
        failed = bulk.undo(DB, r_roll["run"])
    finally:
        bulk._VEHICLE_COLS = real_cols
    check("a mid-undo failure leaves the database byte-identical",
          not failed["ok"] and "rolled back" in failed["error"]
          and sha(DB) == digest and counts() == before_counts,
          f"{failed['error'][:52]}; digest {digest} unchanged, "
          f"counts {before_counts} unchanged")
    check("and the run is still undoable afterwards, not left half-done",
          bulk.undo(DB, r_roll["run"])["ok"] and counts() == (8, 8),
          f"the retry succeeded: db back to {counts()}")

    print("\n== a refused undo changes nothing ==")
    digest = sha(DB)
    bad = bulk.undo(DB, 99999)
    check("undoing a run that does not exist is an error, not a no-op write",
          not bad["ok"] and sha(DB) == digest,
          f"{bad['error'][:44]}; file digest unchanged ({digest})")

    print("\n== the run log is the record of what was done ==")
    runs = bulk.history(DB)
    check("every run is listed newest first with its counts and state",
          len(runs) >= 2 and runs[0]["id"] > runs[-1]["id"]
          and all("obs_new" in r and "undone_at" in r for r in runs),
          f"{len(runs)} run(s); newest #{runs[0]['id']} "
          f"{runs[0]['obs_new']} sightings, undone={bool(runs[0]['undone_at'])}")
    check("an undone run stays in the log, marked, rather than disappearing",
          any(r["undone_at"] for r in runs),
          f"{sum(1 for r in runs if r['undone_at'])} of {len(runs)} marked undone")

    print("\n== the guard against a whole folder dropped by mistake ==")
    many = [dict(FILE_A, name=f"f{i}.csv") for i in range(bulk.MAX_FILES + 1)]
    digest = sha(DB)
    r7 = bulk.run(many, DB)
    check("more files than the limit is refused before anything is written",
          not r7["ok"] and str(bulk.MAX_FILES) in r7["error"]
          and sha(DB) == digest,
          r7["error"][:70])
    check("no files at all is a message, not a crash",
          bulk.run([], DB)["ok"] is False,
          bulk.run([], DB)["error"])

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_bulk: seeding throwaway DB ==")
    fresh()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
