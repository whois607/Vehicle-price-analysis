"""Verification for merging duplicate identities (vpa/merge.py).

    python -m tests.check_merge      (from vehicle-price-analysis/)

Merging is the DANGEROUS direction - a wrong merge welds two cars' prices
into one history and cannot be undone by inspection - so this suite is built
around what must NOT happen. It seeds cars that look mergeable in every way
except one, and asserts each is left alone:

  * a rounded odometer (two cars really can share "150,000 km" and a price);
  * a year or state that actually conflicts;
  * two site ad ids (the site itself says they are different ads);
  * no site ad id at all (nothing stable to survive onto);
  * a merge that would put two prices on one capture day;
  * a merge whose odometer would run backwards through time.

Then it asserts the safe case IS merged, that no price datum moves, and that
a forced invariant failure rolls the whole thing back.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

from vpa import analyze
from vpa.merge import apply, plan
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "merge_work"
DB = WORK / "merge_test.db"

RESULTS: list[tuple[bool, str]] = []
D1, D2, D3 = "2026-05-10", "2026-06-14", "2026-07-19"


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def seed(db: Path) -> None:
    """Every group below is a PAIR that shares an exact price and odometer.
    Only the first two should ever merge; each of the rest violates exactly
    one condition, so a failure names the condition that broke."""
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    raw = []

    def car(tag, lid, day, price, odo, **kw):
        raw.append({"make": "Ford", "model": "Falcon", "variant": tag,
                    "listing_id": lid, "capture_date": day, "price": price,
                    "odometer": odo, "year": kw.pop("year", 2013),
                    "state": kw.pop("state", "NSW"), **kw})

    # 1. SAFE pair: precise odometer, one site id, no conflicts.
    # One row carries a price_note, because price_note is NULL on most rows
    # and a word on a few - and the invariant fingerprint has to sort a
    # mixture of the two without raising.
    car("Safe", "EXA-AD-1001", D1, 18000, 167_431)
    car("Safe", "EXA-AD-1001", D3, "17500 negotiable", 167_431)
    car("Safe", None, D2, 18000, 167_431, state=None)
    # 2. SAFE triple spanning three id eras
    car("Trio", "EXB-AD-2002", D3, 24990, 98_744)
    car("Trio", "IMG-aaaa1111", D1, 24990, 98_744)
    car("Trio", None, D2, 24990, 98_744, year=None)
    # 3. ROUND odometer - two different cars really can share this
    car("Round", "EXA-AD-3003", D1, 15000, 150_000)
    car("Round", None, D2, 15000, 150_000)
    # 4. YEAR conflicts
    car("YearClash", "EXA-AD-4004", D1, 21000, 143_216, year=2012)
    car("YearClash", None, D2, 21000, 143_216, year=2015)
    # 5. TWO site ids - the site says these are two ads
    car("TwoIds", "EXA-AD-5005", D1, 19000, 132_887)
    car("TwoIds", "EXB-AD-5006", D2, 19000, 132_887)
    # 6. NO site id at all - nothing stable to survive onto
    car("NoId", "IMG-bbbb2222", D1, 16000, 121_559)
    car("NoId", None, D2, 16000, 121_559)
    # 7. Same day, two different prices -> merging would corrupt.
    # They must LINK on one day (same price and odometer) and DISAGREE on
    # another, so the partner needs a stable id of its own - an id-less pair
    # at two odometers would simply be two different keys and never link.
    car("DayClash", "EXA-AD-7007", D1, 22000, 176_338)
    car("DayClash", "EXA-AD-7007", D2, 21000, 176_338)
    car("DayClash", "IMG-dddd4444", D1, 22000, 176_338)
    car("DayClash", "IMG-dddd4444", D2, 20500, 176_338)
    # 8. Odometer would run backwards ONLY once merged: the partner's later
    # sighting shows fewer kilometres than the survivor's earlier one.
    car("Backwards", "EXA-AD-8008", D2, 26000, 205_998)
    car("Backwards", "IMG-cccc3333", D1, 26000, 205_998)
    car("Backwards", "IMG-cccc3333", D3, 25000, 180_447)

    recs = [r for r in (make_record(d, source="examplecars") for d in raw) if r]
    with Store(db) as store:
        store.ingest(recs, source_file="(merge-test)", adapter="test")


def variants_in(groups) -> set:
    return {g["label"].split()[-1] for g in groups}


def held_reason(p, variant) -> str:
    with Store(DB) as store:
        rows = {r["key"]: r["variant"] for r in store.conn.execute(
            "SELECT key, variant FROM vehicles")}
    for h in p["held"]:
        if any(rows.get(k) == variant for k in h["keys"]):
            return h["reason"]
    return ""


def run_checks() -> int:
    with Store(DB) as store:
        p = plan(store)
        before_veh = store.conn.execute(
            "SELECT COUNT(*) FROM vehicles").fetchone()[0]
        before_obs = store.conn.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0]

    print("== only the provably-safe groups are planned ==")
    merged = variants_in(p["merges"])
    check("the two safe groups merge, and nothing else does",
          merged == {"Safe", "Trio"} and p["groups"] == 2,
          f"planned: {sorted(merged)}")
    check("a three-identity group folds two into one, across id eras",
          any(len(m["absorbed"]) == 2 for m in p["merges"])
          and p["absorbed"] == 3,
          f"{p['absorbed']} surplus identities over {p['groups']} groups")
    check("the surviving key is always the one with the site's own ad id",
          all(m["listing_id"].startswith(("EXA-AD-", "EXB-AD-"))
              for m in p["merges"]),
          ", ".join(m["listing_id"] for m in p["merges"]))
    check("the plan quotes the shared reading that linked them, as evidence",
          all(m["odometer_km"] % 1000 != 0 for m in p["merges"]),
          "; ".join(f"${m['price']:,.0f} @ {m['odometer_km']:,.0f} km"
                    for m in p["merges"]))

    print("\n== every unsafe group is held back, with the reason named ==")
    # VACUOUS-GUARD: each of these would pass if the group were simply absent
    # from the data, so the reason string is asserted too - it can only exist
    # if the group was actually found and then rejected.
    for variant, needle in (("Round", "whole thousand"),
                            ("YearClash", "year"),
                            ("TwoIds", "site ad id"),
                            ("NoId", "site ad id"),
                            ("DayClash", "two prices"),
                            ("Backwards", "backwards")):
        reason = held_reason(p, variant)
        check(f"{variant} is not merged, and the reason says so",
              variant not in merged and needle in reason.lower()
              and len(reason) > 10,
              reason or "NOT FOUND IN held[] AT ALL")
    check("held groups are counted and none silently vanished",
          len(p["held"]) == 6,
          f"{len(p['held'])} held, {p['groups']} merged, "
          f"8 seeded groups in total")

    print("\n== a dry run changes nothing at all ==")
    before_hash = hashlib.sha256(DB.read_bytes()).hexdigest()
    with Store(DB) as store:
        plan(store)
        plan(store)
    check("two more dry runs left the file byte-identical",
          hashlib.sha256(DB.read_bytes()).hexdigest() == before_hash
          and DB.stat().st_size > 0,
          f"sha256 {before_hash[:16]}... ({DB.stat().st_size:,} bytes)")

    print("\n== applying keeps every price datum ==")
    live = WORK / "applied.db"
    shutil.copy2(DB, live)
    with sqlite3.connect(live) as conn:
        before_fp = sorted(conn.execute(
            "SELECT capture_date, price_aud, price_note, odometer_km "
            "FROM observations").fetchall())
    res = apply(live)
    with sqlite3.connect(live) as conn:
        after_fp = sorted(conn.execute(
            "SELECT capture_date, price_aud, price_note, odometer_km "
            "FROM observations").fetchall())
        after_veh = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        after_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM observations o LEFT JOIN vehicles v "
            "ON v.key = o.key WHERE v.key IS NULL").fetchone()[0]
    check("every (date, price, note, odometer) survives byte-for-byte",
          after_fp == before_fp and after_obs == before_obs,
          f"{len(after_fp)} observations, multiset identical")
    check("vehicle count falls by exactly the number folded in",
          after_veh == before_veh - p["absorbed"] and orphans == 0,
          f"{before_veh} -> {after_veh} (-{p['absorbed']}), {orphans} orphans")
    check("a backup was taken before anything was touched",
          Path(res["backup"]).exists()
          and Path(res["backup"]).stat().st_size > 0,
          Path(res["backup"]).name)
    with Store(live) as store:
        after_bad = analyze.collided_keys(store.frame())
    check("no identity now holds two prices on one capture day",
          not after_bad, f"{len(after_bad)} colliding key(s)")

    print("\n== the merged car really is one car with one history ==")
    with sqlite3.connect(live) as conn:
        conn.row_factory = sqlite3.Row
        safe = [dict(r) for r in conn.execute("""
            SELECT o.capture_date, o.price_aud FROM observations o
            JOIN vehicles v ON v.key = o.key WHERE v.variant = 'Safe'
            ORDER BY o.capture_date""")]
    check("three separate identities became one three-day price history",
          len(safe) == 3 and [r["capture_date"] for r in safe] == [D1, D2, D3]
          and safe[-1]["price_aud"] == 17500,
          f"{[(r['capture_date'], r['price_aud']) for r in safe]}")

    print("\n== a second run is a no-op, and failure rolls everything back ==")
    again = apply(live)
    check("re-running finds nothing left to merge",
          again["groups"] == 0 and again["applied"] is False,
          again.get("note", ""))

    broken = WORK / "rollback.db"
    shutil.copy2(DB, broken)
    before_broken = hashlib.sha256(broken.read_bytes()).hexdigest()
    import vpa.merge as M
    orig = M._fingerprint
    calls = {"n": 0}

    def poisoned(store):
        # only the AFTER reading is corrupted; poisoning both would make them
        # agree with each other and the invariant would never fire
        calls["n"] += 1
        return orig(store) if calls["n"] == 1 else ["deliberately wrong"]

    M._fingerprint = poisoned
    try:
        apply(broken)
        raised = False
    except RuntimeError:
        raised = True
    finally:
        M._fingerprint = orig
    check("an invariant failure raises and leaves the file byte-identical",
          raised
          and hashlib.sha256(broken.read_bytes()).hexdigest() == before_broken,
          "forced fingerprint mismatch -> ROLLBACK, file unchanged")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_merge: seeding throwaway DB ==")
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
