"""Verification for vpa/repair.py - the identity migration.

    python -m tests.check_repair       (from vehicle-price-analysis/)

This one guards the database itself, so the checks are about what must NEVER
happen, not just what should:

  * a dry run writes absolutely nothing;
  * after applying, the multiset of (date, price, note, odometer) is byte
    identical - no price invented, edited or lost;
  * merged identities go to zero, and vehicles only ever increase;
  * a car legitimately tracked across capture days keeps ONE identity (the
    500 km bucket has to tolerate real driving between captures);
  * the migration is idempotent;
  * if an invariant fails mid-flight the transaction rolls back and the
    database is left exactly as it was.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

from vpa import repair
from vpa.schema import Record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "repair_test.db"

RESULTS: list[tuple[bool, str]] = []
D1, D2 = "2026-02-08", "2026-02-22"


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def rec(**kw) -> Record:
    base = dict(source="examplecars", vehicle_type="car", make="Ford",
                model="Falcon", state="NSW")
    base.update(kw)
    return Record(**base)


def seed() -> None:
    """Known content:

    MERGE-A  two DIFFERENT cars, no ad id, same year/variant/state, odometers
             120,400 and 123,900 km -> same 5,000 km bucket (24), different
             500 km buckets (240 vs 247). Both on D1: one key, two prices.
    MERGE-B  same trap via a repeated ad id: two cars sharing 'IMG-dup'.
    DRIVEN   ONE car tracked across D1 and D2, odometer 80,000 -> 80,300
             (real driving). Must stay a single identity after migration.
    PLAIN    one ordinary car with a good ad id, must not be touched at all.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    recs = [   # written below through the OLD 5,000 km rule - see seed()
        rec(capture_date=D1, variant="XR6", year=2012, odometer_km=120400, price_aud=14000),
        rec(capture_date=D1, variant="XR6", year=2012, odometer_km=123900, price_aud=17500),
        rec(capture_date=D1, variant="G6E", year=2013, odometer_km=None,
            price_aud=9000, listing_id="IMG-dup", source="v2"),
        rec(capture_date=D1, variant="G6E", year=2013, odometer_km=None,
            price_aud=21000, listing_id="IMG-dup", source="v2"),
        rec(capture_date=D1, variant="XR8", year=2014, odometer_km=80000, price_aud=30000),
        rec(capture_date=D2, variant="XR8", year=2014, odometer_km=80300, price_aud=29000),
        rec(capture_date=D1, variant="XT", year=2011, odometer_km=200000,
            price_aud=8000, listing_id="EXB-AD-1"),
    ]
    # Seed as the OLD code would have: the point of the migration is legacy
    # data, and the current 500 km rule would never create MERGE-A at all.
    legacy, Record.ODO_BUCKET_KM = Record.ODO_BUCKET_KM, 5000
    try:
        with Store(DB) as store:
            store.ingest(recs, source_file="(repair-test)", adapter="test")
    finally:
        Record.ODO_BUCKET_KM = legacy


def snap(path):
    con = sqlite3.connect(path)
    obs = Counter(con.execute(
        "SELECT capture_date, price_aud, price_note, odometer_km FROM observations"))
    veh = con.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
    keys = {r[0] for r in con.execute("SELECT key FROM vehicles")}
    con.close()
    return obs, veh, keys


def merged_count(path) -> int:
    con = sqlite3.connect(path)
    n = con.execute("""SELECT COUNT(*) FROM (
        SELECT key, capture_date FROM observations WHERE price_aud IS NOT NULL
        GROUP BY key, capture_date HAVING COUNT(DISTINCT price_aud) > 1)""").fetchone()[0]
    con.close()
    return n


def run_checks() -> int:
    before_obs, before_veh, before_keys = snap(DB)
    print("== the trap is set ==")
    # 4 identities for 6 real cars: MERGE-A and MERGE-B each hide two cars
    # behind one key, plus the driven XR8 and the plain XT.
    check("legacy seeding produced the two merged identities to repair",
          merged_count(DB) == 2 and before_veh == 4
          and sum(before_obs.values()) == 7,
          f"{merged_count(DB)} merged key(s), {before_veh} vehicles, "
          f"{sum(before_obs.values())} observations")

    print("\n== a dry run must not touch the database ==")
    plan = repair.apply(DB, dry_run=True)
    after_dry = snap(DB)
    check("dry run reports work but writes nothing",
          plan["observations_rekeyed"] > 0 and after_dry == (before_obs, before_veh, before_keys),
          f"planned {plan['observations_rekeyed']} re-keys, database unchanged")

    print("\n== apply ==")
    rep = repair.apply(DB, dry_run=False)
    after_obs, after_veh, _ = snap(DB)
    check("a backup was taken before any change",
          rep["backup"] and Path(rep["backup"]).exists(),
          Path(rep["backup"]).name if rep["backup"] else "NONE")
    check("every price row survives byte for byte",
          after_obs == before_obs,
          f"{sum(after_obs.values())} rows, multiset identical")
    check("merged identities are gone", merged_count(DB) == 0,
          f"{rep['merged_before']} -> {merged_count(DB)}")
    check("vehicles only increased (a split can never merge)",
          after_veh > before_veh, f"{before_veh} -> {after_veh}")

    con = sqlite3.connect(DB)
    orph = con.execute("""SELECT COUNT(*) FROM observations o WHERE NOT EXISTS
        (SELECT 1 FROM vehicles v WHERE v.key=o.key)""").fetchone()[0]
    empty = con.execute("""SELECT COUNT(*) FROM vehicles v WHERE NOT EXISTS
        (SELECT 1 FROM observations o WHERE o.key=v.key)""").fetchone()[0]
    check("no orphaned observations and no empty vehicle rows",
          orph == 0 and empty == 0, f"orphans={orph}, empty={empty}")

    print("\n== the split must not fragment a real price history ==")
    driven = con.execute("""
        SELECT o.key, COUNT(*) FROM observations o JOIN vehicles v ON v.key=o.key
        WHERE v.variant='XR8' GROUP BY o.key""").fetchall()
    check("a car driven 300 km between captures keeps ONE identity",
          len(driven) == 1 and driven[0][1] == 2,
          f"XR8 across {D1}/{D2}: {len(driven)} identity, {driven[0][1] if driven else 0} sightings")
    untouched = con.execute("""SELECT COUNT(*) FROM vehicles
                               WHERE listing_id='EXB-AD-1'""").fetchone()[0]
    check("a healthy ad-id vehicle is left completely alone",
          untouched == 1 and any(k in before_keys for k in
                                 [r[0] for r in con.execute(
                                     "SELECT key FROM vehicles WHERE listing_id='EXB-AD-1'")]),
          "its key is unchanged")
    con.close()

    print("\n== repeatable ==")
    again = repair.apply(DB, dry_run=True)
    check("migration is idempotent (a second run has nothing to do)",
          again["observations_rekeyed"] == 0 and again["merged_before"] == 0,
          f"{again['observations_rekeyed']} moves planned")

    print("\n== a failure must roll back, not half-write ==")
    guard_obs, guard_veh, guard_keys = snap(DB)
    real_fp = repair._fingerprint
    repair._fingerprint = lambda rows: ["deliberately-wrong"]   # force the guard
    try:
        # give it something to do first, so the transaction is non-empty
        with Store(DB) as store:
            store.ingest([rec(capture_date=D2, variant="XR6", year=2012,
                              odometer_km=121000, price_aud=13500)],
                         source_file="(rollback-test)", adapter="test")
        pre = snap(DB)
        repair.ODO_BUCKET_FORCE = None
        raised = False
        try:
            # re-key something by pretending the stored keys are stale
            orig = repair._target_key
            repair._target_key = lambda r, bad: "0" * 16
            repair.apply(DB, dry_run=False)
        except Exception:
            raised = True
        finally:
            repair._target_key = orig
        post = snap(DB)
        check("an invariant failure raises and rolls the whole thing back",
              raised and post == pre,
              "database identical to before the failed attempt")
    finally:
        repair._fingerprint = real_fp

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_repair: seeding throwaway DB ==")
    seed()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
