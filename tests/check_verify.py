"""Verification for the Data check (vpa/verify.py).

    python -m tests.check_verify      (from vehicle-price-analysis/)

A checker that always says "fine" is worse than no checker, so this suite
DELIBERATELY BREAKS things and asserts each check notices:

  * a healthy database passes every check;
  * a workbook built from a smaller dataset is caught as disagreeing, and a
    workbook rebuilt afterwards is caught as agreeing again;
  * an absurd price and a future capture date are caught;
  * a merged identity (two prices on one capture day) is caught;
  * the same car recorded twice on one day - once with an ad id, once without
    - is caught, because that is the dedupe rule's real blind spot;
  * `IMG-` style ids legitimately shared by two different cars are NOT
    flagged, since repair.py splits those on purpose and re-flagging them
    would report the fix as the fault;
  * an orphaned observation is caught;
  * `ok` is False only for a real failure, never for a warning;
  * checking writes nothing.

Uses a copy of each fixture so the breakage never touches a real file. Needs
openpyxl (already required) but not Excel itself.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from vpa.schema import Record, make_record
from vpa.store import Store
from vpa.verify import verify_payload

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "verify_work"
DB = WORK / "verify_test.db"
XLSX = WORK / "verify_viewer.xlsx"

RESULTS: list[tuple[bool, str]] = []

DAY_A, DAY_B = "2026-02-07", "2026-03-07"


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def status_of(payload: dict, cid: str) -> str:
    return next(c["status"] for c in payload["checks"] if c["id"] == cid)


def detail_of(payload: dict, cid: str) -> str:
    return next(c["detail"] for c in payload["checks"] if c["id"] == cid)


def seed(db: Path) -> None:
    """A clean, unremarkable dataset: 5 cars, two capture days, all with ad
    ids, sane prices and odometers."""
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    raw = []
    for i, price in enumerate((18000, 21000, 24000, 27000, 30000)):
        for day in (DAY_A, DAY_B):
            raw.append({"listing_id": f"AD-{i}", "make": "Ford",
                        "model": "Falcon", "variant": "XR6", "year": 2013,
                        "price": price, "odometer": 100000 + i * 12000,
                        "state": "NSW", "seller_type": "dealer",
                        "capture_date": day})
    recs = [r for r in (make_record(d, source="examplecars") for d in raw) if r]
    with Store(db) as store:
        store.ingest(recs, source_file="(verify-test)", adapter="test")


def add(db: Path, rows: list[dict], *, legacy_bucket: int | None = None) -> None:
    recs = [r for r in (make_record(d, source="examplecars") for d in rows) if r]
    old = Record.ODO_BUCKET_KM
    if legacy_bucket:
        Record.ODO_BUCKET_KM = legacy_bucket
    try:
        with Store(db) as store:
            store.ingest(recs, source_file="(verify-test)", adapter="test")
    finally:
        Record.ODO_BUCKET_KM = old


def fresh(name: str) -> Path:
    """A private copy of the clean database, so one broken case cannot leak
    into the next."""
    dst = WORK / f"{name}.db"
    if dst.exists():
        dst.unlink()
    shutil.copy2(DB, dst)
    return dst


def run_checks() -> int:
    print("== a healthy database passes everything ==")
    clean = verify_payload(DB, XLSX_MISSING)
    ids = [c["id"] for c in clean["checks"]]
    check("all eight checks run, in a stable order",
          ids == ["sqlite", "orphans", "engine", "identities", "workbook",
                  "values", "duplicates", "trims"],
          ", ".join(ids))
    check("nothing fails and nothing warns on clean data",
          clean["ok"] and clean["counts"]["fail"] == 0
          and clean["counts"]["warn"] == 0,
          f"{clean['counts']['pass']} passed, "
          f"{clean['counts']['skip']} skipped (no workbook yet)")
    check("SQL and the analysis frame agree, and the totals are the real ones",
          status_of(clean, "engine") == "pass"
          and "10 observations" in detail_of(clean, "engine"),
          detail_of(clean, "engine"))
    check("a missing workbook is skipped, not failed",
          status_of(clean, "workbook") == "skip",
          detail_of(clean, "workbook"))

    print("\n== the workbook is compared against the live database ==")
    from vpa.viewer_xlsx import generate
    generate(DB, XLSX)
    built = verify_payload(DB, XLSX)
    check("a workbook built from this database is reported as matching",
          status_of(built, "workbook") == "pass"
          and "identical" in detail_of(built, "workbook"),
          detail_of(built, "workbook"))
    grown = fresh("grown")
    add(grown, [{"listing_id": "AD-NEW", "make": "Ford", "model": "Falcon",
                 "variant": "XR6", "year": 2014, "price": 26000,
                 "odometer": 88000, "state": "VIC", "seller_type": "private",
                 "capture_date": DAY_B}])
    drifted = verify_payload(grown, XLSX)
    check("after an import, the older workbook is caught as disagreeing",
          status_of(drifted, "workbook") == "warn"
          and "rows: workbook 10 vs database 11" in detail_of(drifted, "workbook"),
          detail_of(drifted, "workbook"))
    generate(grown, XLSX)
    rebuilt = verify_payload(grown, XLSX)
    check("rebuilding the workbook clears the disagreement",
          status_of(rebuilt, "workbook") == "pass",
          detail_of(rebuilt, "workbook"))
    # A change no total can see: two sightings swap cars, as a duplicate
    # merge re-points rows. Row count, price total and date range all stay
    # put; the data stamp is what notices.
    with sqlite3.connect(grown) as conn:
        a = conn.execute("SELECT id, key FROM observations "
                         "ORDER BY id LIMIT 1").fetchone()
        keys = [k for (k,) in conn.execute("SELECT DISTINCT key FROM observations")]
        other = next(k for k in keys if k != a[1])
        conn.execute("UPDATE observations SET key = ? WHERE id = ?", (other, a[0]))
    moved = verify_payload(grown, XLSX)
    check("a change the totals cannot see (a row moved to another car) is caught",
          status_of(moved, "workbook") == "warn"
          and "totals agree" in detail_of(moved, "workbook"),
          detail_of(moved, "workbook"))
    generate(grown, XLSX)
    check("...and rebuilding clears that too",
          status_of(verify_payload(grown, XLSX), "workbook") == "pass",
          detail_of(verify_payload(grown, XLSX), "workbook"))

    print("\n== impossible values are caught ==")
    odd = fresh("odd")
    with sqlite3.connect(odd) as conn:
        conn.execute("UPDATE observations SET price_aud = 3 "
                     "WHERE id = (SELECT MIN(id) FROM observations)")
        conn.execute("UPDATE observations SET capture_date = '2099-01-01' "
                     "WHERE id = (SELECT MAX(id) FROM observations)")
    got = verify_payload(odd, XLSX_MISSING)
    check("a $3 price and a capture date in 2099 are both reported",
          status_of(got, "values") == "warn"
          and "1 price(s) outside" in detail_of(got, "values")
          and "1 capture date(s) in the future" in detail_of(got, "values"),
          detail_of(got, "values"))
    check("a warning does not condemn the dataset (ok stays True)",
          got["ok"] and got["counts"]["fail"] == 0,
          f"ok={got['ok']}, {got['counts']['warn']} warning(s)")

    print("\n== a merged identity is caught ==")
    merged = fresh("merged")
    # Seeded through the OLD 5,000 km rule: two different cars whose odometers
    # share a 5,000 km bucket collapse onto one key and land two prices on one
    # day. Record.ODO_BUCKET_KM is 500 now, so this is how a legacy database
    # looks, and the check has to see it.
    add(merged, [{"make": "Ford", "model": "Falcon", "variant": "G6E",
                  "year": 2011, "price": price, "odometer": odo,
                  "state": "QLD", "capture_date": DAY_B}
                 for odo, price in ((250100, 12000), (252900, 15000))],
        legacy_bucket=5000)
    got = verify_payload(merged, XLSX_MISSING)
    check("one identity holding two prices on one day is reported",
          status_of(got, "identities") == "warn"
          and "1 identity" in detail_of(got, "identities"),
          detail_of(got, "identities"))
    check("and it names the command that fixes it",
          "repair-identities" in next(c["fix"] for c in got["checks"]
                                      if c["id"] == "identities"),
          "fix names `python -m vpa repair-identities --apply`")

    print("\n== double counting is caught, but a correct split is not ==")
    dup = fresh("dup")
    # the real blind spot: the same car captured once WITH an ad id and once
    # without, so an id-based key and a fingerprint key never collide
    add(dup, [{"make": "Ford", "model": "Falcon", "variant": "XR6",
               "year": 2013, "price": 18000, "odometer": 100000,
               "state": "NSW", "seller_type": "dealer", "capture_date": DAY_B}])
    got = verify_payload(dup, XLSX_MISSING)
    check("one car recorded with and without an ad id is reported once",
          status_of(got, "duplicates") == "warn"
          and "1 car(s) captured both with and without an ad id"
          in detail_of(got, "duplicates"),
          detail_of(got, "duplicates"))

    # The case the first version of this check MISSED. Grouping by capture
    # day hid it, and on the real database that hid 186 surplus identities
    # behind a reported 26 - this is the regression guard for that.
    cross = fresh("crossday")
    add(cross, [{"make": "Ford", "model": "Falcon", "variant": "XR6",
                 "year": 2013, "price": 18000, "odometer": 100000,
                 "state": "VIC", "capture_date": "2026-04-11"}])
    got = verify_payload(cross, XLSX_MISSING)
    check("a car re-captured on a LATER day without its ad id is still caught",
          status_of(got, "duplicates") == "warn"
          and "captured both with and without an ad id"
          in detail_of(got, "duplicates"),
          f"seeded on 2026-04-11 against a stored {DAY_A}/{DAY_B} car: "
          f"{detail_of(got, 'duplicates')}")
    check("the surplus COPY count is reported, not just the group count",
          "1 surplus identit" in detail_of(got, "duplicates"),
          "one extra identity for one real car")

    split = fresh("split")
    # two genuinely different cars that shared an image-derived id and were
    # correctly split onto separate identities - this must NOT be flagged
    add(split, [{"listing_id": "IMG-abc123", "make": "Ford", "model": "Falcon",
                 "variant": "XT", "year": 2012, "price": 11000,
                 "odometer": 190000, "state": "WA", "capture_date": DAY_B},
                {"listing_id": "IMG-abc123", "make": "Ford", "model": "Falcon",
                 "variant": "XT", "year": 2012, "price": 13500,
                 "odometer": 140000, "state": "WA", "capture_date": DAY_B}])
    got = verify_payload(split, XLSX_MISSING)
    check("two different cars sharing an IMG- id are NOT called duplicates",
          status_of(got, "duplicates") == "pass",
          f"{detail_of(got, 'duplicates')} "
          f"(they differ in odometer and price, so they are two cars)")

    print("\n== one trim under two names is caught ==")
    # The real case, reproduced: 'XR6 Auto R' is the SAME trim as 'XR6', and
    # the two only exist separately because one capture went through a parser
    # that did not strip the tail.
    split = fresh("splittrim")
    add(split, [{"listing_id": "AD-TAIL", "make": "Ford", "model": "Falcon",
                 "variant": "XR6 Auto R", "year": 2013, "price": 22000,
                 "odometer": 91000, "state": "NSW", "seller_type": "dealer",
                 "capture_date": DAY_B}])
    got = verify_payload(split, XLSX_MISSING)
    check("a variant that is one gearbox tail away from another is reported",
          status_of(got, "trims") == "warn"
          and "'XR6' x5" in detail_of(got, "trims")
          and "gearbox or drive" in detail_of(got, "trims"),
          detail_of(got, "trims"))
    check("...and the report counts the cars stranded in the smaller spelling",
          "1 vehicle(s) sitting in the smaller spelling" in detail_of(got, "trims"),
          "one car is in 'XR6 Auto R' and five in 'XR6'")
    check("...while the clean database says one trim, one name",
          status_of(clean, "trims") == "pass",
          detail_of(clean, "trims"))
    # VACUOUS-GUARD: a check that warned on everything would pass the case
    # above by accident, so a genuinely DIFFERENT trim must stay quiet.
    other = fresh("othertrim")
    add(other, [{"listing_id": "AD-OTHER", "make": "Ford", "model": "Falcon",
                 "variant": "XR8", "year": 2013, "price": 34000,
                 "odometer": 70000, "state": "NSW", "seller_type": "dealer",
                 "capture_date": DAY_B}])
    got2 = verify_payload(other, XLSX_MISSING)
    check("a genuinely different trim is NOT reported as a split",
          status_of(got2, "trims") == "pass",
          f"XR6 and XR8 coexist quietly: {detail_of(got2, 'trims')}")
    # Series codes are real cars, not noise - 'XR6 FG X' must not be folded
    # into 'XR6'. This is the line between a cosmetic split and a real trim.
    series = fresh("seriestrim")
    add(series, [{"listing_id": "AD-SER", "make": "Ford", "model": "Falcon",
                  "variant": "XR6 FG X", "year": 2015, "price": 29000,
                  "odometer": 60000, "state": "NSW", "seller_type": "dealer",
                  "capture_date": DAY_B}])
    got3 = verify_payload(series, XLSX_MISSING)
    check("a series code is treated as a real difference, not a spelling",
          status_of(got3, "trims") == "pass",
          "'XR6' and 'XR6 FG X' are different cars and are left alone")

    print("\n== structural damage is caught ==")
    orphan = fresh("orphan")
    with sqlite3.connect(orphan) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE observations SET key = 'ghost-key' "
                     "WHERE id = (SELECT MIN(id) FROM observations)")
    got = verify_payload(orphan, XLSX_MISSING)
    check("an observation pointing at no vehicle FAILS (not just warns)",
          status_of(got, "orphans") == "fail" and not got["ok"],
          f"{detail_of(got, 'orphans')}; ok={got['ok']}")
    check("the same damage also shows as a foreign-key violation",
          status_of(got, "sqlite") == "fail",
          detail_of(got, "sqlite"))

    print("\n== checking writes nothing ==")
    before = DB.stat().st_mtime_ns, DB.stat().st_size
    for _ in range(3):
        verify_payload(DB, XLSX)
    after = DB.stat().st_mtime_ns, DB.stat().st_size
    check("three checks left the database byte-identical",
          before == after,
          f"mtime and size unchanged ({after[1]:,} bytes)")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


#: a path that deliberately does not exist, for the "no workbook" cases
XLSX_MISSING = WORK / "not-built.xlsx"


def main() -> int:
    print("== check_verify: seeding throwaway DB ==")
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    seed(DB)
    with Store(DB) as store:
        s = store.status()
    print(f"  {s['vehicles']} vehicles, {s['observations']} observations\n")
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
