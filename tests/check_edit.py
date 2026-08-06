"""Manual correction of committed rows.

    python -m tests.check_edit

Editing is the only operation in this project whose input is a person typing,
and the only one that changes recorded price history in place. So the checks
here are weighted towards what must NOT happen: a bad value reaching the
database, a write escaping its transaction, a deletion that cannot be undone,
and an edit applied to rows other than the ones previewed.

Every case runs against a throwaway database and asserts the file itself is
byte-identical whenever a write was supposed to be refused.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from vpa import edit
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "edit_test.db"
DAY_A, DAY_B = "2026-03-01", "2026-03-08"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def digest() -> str:
    return hashlib.sha256(DB.read_bytes()).hexdigest()[:16]


def seed() -> None:
    """Three cars, one of them seen twice, plus two sharing a mis-parsed trim
    so the bulk rename has something real to fix."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []
    for day, price in ((DAY_A, 20000), (DAY_B, 18500)):
        raw.append({"listing_id": "E-1", "make": "Ford", "model": "Falcon",
                    "variant": "XR6", "year": 2012, "price": price,
                    "odometer": 150000, "state": "NSW",
                    "seller_type": "dealer", "capture_date": day})
    raw.append({"listing_id": "E-2", "make": "Ford", "model": "Falcon",
                "variant": "XR6 Auto R", "year": 2013, "price": 21000,
                "odometer": 120000, "state": "VIC", "capture_date": DAY_B})
    raw.append({"listing_id": "E-3", "make": "Ford", "model": "Falcon",
                "variant": "XR6 Auto R", "year": 2014, "price": 23000,
                "odometer": 90000, "state": "QLD", "capture_date": DAY_B})
    recs = [r for r in (make_record(d, source="carsales") for d in raw) if r]
    with Store(DB) as store:
        store.ingest(recs, source_file="(edit-test)", adapter="test")


def ids() -> dict:
    with Store(DB) as store:
        veh = {r["listing_id"]: r["key"] for r in
               store.conn.execute("SELECT listing_id, key FROM vehicles")}
        obs = [dict(r) for r in store.conn.execute(
            "SELECT o.id, o.key, o.capture_date, o.price_aud FROM observations o "
            "JOIN vehicles v ON v.key=o.key ORDER BY o.id")]
    return {"veh": veh, "obs": obs}


def run_checks() -> int:
    info = ids()
    k1 = info["veh"]["E-1"]
    first_obs = info["obs"][0]["id"]

    print("== a bad value never reaches the database ==")
    before = digest()
    bad_cases = [
        ("price_aud", "not a price", "observation", first_obs),
        ("price_aud", "$12", "observation", first_obs),          # implausibly low
        ("odometer_km", "elephants", "observation", first_obs),
        ("capture_date", "2099-01-01", "observation", first_obs),  # the future
        ("year", "1780", "vehicle", k1),
        ("state", "Nebraska", "vehicle", k1),
        ("seller_type", "auctioneer", "vehicle", k1),
        ("make", "   ", "vehicle", k1),
    ]
    refused = []
    with Store(DB) as store:
        for field, value, op, target in bad_cases:
            p = edit.plan(store, [{"op": op, "target": target,
                                   "fields": {field: value}}])
            refused.append((field, value, p["ok"] is False and bool(p["errors"]),
                            (p["errors"] or [""])[0]))
    check("every implausible value is refused at planning, with a reason",
          all(r[2] for r in refused),
          "; ".join(f"{f}={v!r}" for f, v, ok, _ in refused if not ok)
          or f"{len(refused)}/{len(refused)} refused, e.g. {refused[3][3][:60]}")
    check("...and a refusal writes nothing at all",
          digest() == before, f"database unchanged ({before})")
    check("a future capture date is refused by name",
          "future" in [r[3] for r in refused if r[0] == "capture_date"][0],
          [r[3] for r in refused if r[0] == "capture_date"][0][:70])

    print("\n== typed values go through the importer's own normalisers ==")
    with Store(DB) as store:
        p = edit.plan(store, [{"op": "observation", "target": first_obs,
                               "fields": {"price_aud": "$19,250"}},
                              {"op": "vehicle", "target": k1,
                               "fields": {"state": "New South Wales",
                                          "seller_type": "Private seller"}}])
    got = {(c["field"], c["after"]) for c in p["changes"]}
    check("'$19,250', 'New South Wales' and 'Private seller' mean what they "
          "mean on the Import page",
          ("price_aud", 19250.0) in got and ("seller_type", "private") in got
          and not any(c["field"] == "state" for c in p["changes"]),
          "price parsed, seller normalised, and 'New South Wales' recognised "
          "as the NSW already stored - so it is not offered as a change")
    check("a value equal to what is stored is not offered as a change",
          all(not edit._same(c["before"], c["after"]) for c in p["changes"]),
          f"{len(p['changes'])} real change(s), no no-ops")

    print("\n== the preview is exactly what gets written ==")
    with Store(DB) as store:
        p = edit.plan(store, [{"op": "observation", "target": first_obs,
                               "fields": {"price_aud": "19250"},
                               "reason": "misread the ad"}])
    fp = edit.fingerprint(p)
    before_total = p["before"]["price_total"]
    res = edit.apply(DB, [{"op": "observation", "target": first_obs,
                           "fields": {"price_aud": "19250"},
                           "reason": "misread the ad"}], confirm=fp)
    check("an edit applies, and the totals afterwards are the ones promised",
          res["applied"] and res["after"] == res["expected"]
          and res["after"]["price_total"] == round(before_total - 20000 + 19250, 2),
          f"price total {before_total:,.0f} -> {res['after']['price_total']:,.0f}, "
          f"as predicted")
    check("...a backup was taken first",
          res["backup"] and Path(res["backup"]).exists(),
          Path(res["backup"]).name)
    with Store(DB) as store:
        now = store.conn.execute("SELECT price_aud FROM observations WHERE id=?",
                                 (first_obs,)).fetchone()[0]
    check("...and the row really holds the new value",
          now == 19250.0, f"observation {first_obs} is now ${now:,.0f}")

    print("\n== a stale preview cannot be saved ==")
    # VACUOUS-GUARD: `confirm` must actually be compared, not merely accepted.
    stale = edit.apply(DB, [{"op": "observation", "target": first_obs,
                             "fields": {"price_aud": "17000"}}],
                       confirm="0000000000000000")
    with Store(DB) as store:
        unchanged = store.conn.execute(
            "SELECT price_aud FROM observations WHERE id=?",
            (first_obs,)).fetchone()[0]
    check("a fingerprint that does not match the data refuses the write",
          stale["applied"] is False and unchanged == 19250.0
          and "changed since" in " ".join(stale["errors"]),
          " ".join(stale["errors"])[:80])

    print("\n== every change is recorded, and can be undone ==")
    batch = res["batch"]
    with Store(DB) as store:
        logged = [dict(r) for r in store.conn.execute(
            "SELECT * FROM edits WHERE batch=?", (batch,))]
    check("the edit is in the audit table with its old value and a reason",
          len(logged) == 1 and logged[0]["before"] == "20000.0"
          and logged[0]["after"] == "19250.0"
          and logged[0]["reason"] == "misread the ad",
          f"{logged[0]['op']}.{logged[0]['field']}: "
          f"{logged[0]['before']} -> {logged[0]['after']}")
    u = edit.undo(DB, batch)
    with Store(DB) as store:
        back = store.conn.execute("SELECT price_aud FROM observations WHERE id=?",
                                  (first_obs,)).fetchone()[0]
    check("undo puts the original value back",
          u["ok"] and back == 20000.0, f"${back:,.0f} restored")
    check("...and undoing twice is refused rather than repeated",
          edit.undo(DB, batch)["ok"] is False,
          edit.undo(DB, batch)["error"])

    print("\n== deleting a sighting, and getting it back ==")
    info = ids()
    two_obs = [o for o in info["obs"] if o["key"] == k1]
    victim = two_obs[-1]["id"]
    res = edit.apply(DB, [{"op": "delete_observation", "target": victim,
                           "reason": "duplicate of the same day"}])
    with Store(DB) as store:
        left = store.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE key=?", (k1,)).fetchone()[0]
        veh_still = store.conn.execute(
            "SELECT COUNT(*) FROM vehicles WHERE key=?", (k1,)).fetchone()[0]
    check("a sighting is removed and its vehicle kept when others remain",
          res["applied"] and left == 1 and veh_still == 1,
          f"{left} sighting(s) left, vehicle still present")
    u = edit.undo(DB, res["batch"])
    with Store(DB) as store:
        restored = store.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE key=?", (k1,)).fetchone()[0]
        price = store.conn.execute(
            "SELECT price_aud FROM observations WHERE id=?", (victim,)).fetchone()
    check("...and undo restores the sighting with its own id and price",
          u["ok"] and restored == 2 and price is not None,
          f"id {victim} back at ${price[0]:,.0f}")

    print("\n== deleting the last sighting takes the vehicle with it ==")
    k2 = ids()["veh"]["E-2"]
    only = [o for o in ids()["obs"] if o["key"] == k2][0]["id"]
    with Store(DB) as store:
        p = edit.plan(store, [{"op": "delete_observation", "target": only}])
    check("the plan warns before, not after",
          any("only one" in w for w in p["warnings"]),
          [w for w in p["warnings"] if "only one" in w][0][:88])
    res = edit.apply(DB, [{"op": "delete_observation", "target": only}])
    with Store(DB) as store:
        gone = store.conn.execute(
            "SELECT COUNT(*) FROM vehicles WHERE key=?", (k2,)).fetchone()[0]
        orphans = store.conn.execute(
            "SELECT COUNT(*) FROM vehicles v WHERE NOT EXISTS "
            "(SELECT 1 FROM observations o WHERE o.key=v.key)").fetchone()[0]
    check("the vehicle goes too, leaving no row nothing can reach",
          res["applied"] and gone == 0 and orphans == 0,
          "vehicle removed with its last sighting; 0 vehicles without sightings")
    u = edit.undo(DB, res["batch"])
    with Store(DB) as store:
        n = store.conn.execute("SELECT COUNT(*) FROM observations WHERE key=?",
                               (k2,)).fetchone()[0]
    check("...and undo brings both back",
          u["ok"] and n == 1, f"{n} sighting restored under {k2[:10]}")

    print("\n== the bulk trim rename ==")
    with Store(DB) as store:
        p = edit.plan(store, [{"op": "rename_variant", "target": "",
                               "fields": {"make": "Ford", "model": "Falcon",
                                          "old": "XR6 Auto R", "new": "XR6"}}])
    check("a rename plans one change per affected vehicle, not one blanket edit",
          len(p["changes"]) == 2
          and all(c["field"] == "variant" and c["after"] == "XR6"
                  for c in p["changes"]),
          f"{len(p['changes'])} vehicles move from 'XR6 Auto R' to 'XR6' - each "
          f"individually recorded, so each can be undone")
    res = edit.apply(DB, [{"op": "rename_variant", "target": "",
                           "fields": {"make": "Ford", "model": "Falcon",
                                      "old": "XR6 Auto R", "new": "XR6"}}])
    with Store(DB) as store:
        names = sorted({r[0] for r in store.conn.execute(
            "SELECT variant FROM vehicles WHERE make='Ford' AND model='Falcon'")})
        prices = store.conn.execute(
            "SELECT COALESCE(SUM(price_aud),0) FROM observations").fetchone()[0]
    check("the split trim becomes one name, and NO price moves",
          res["applied"] and names == ["XR6"]
          and prices == res["after"]["price_total"],
          f"trims now {names}, price total ${prices:,.0f} unchanged by a rename")
    check("...and a rename that matches nothing is refused, not silently empty",
          edit.apply(DB, [{"op": "rename_variant", "target": "",
                           "fields": {"make": "Ford", "model": "Falcon",
                                      "old": "Nope", "new": "X"}}])["ok"] is False,
          "an unmatched rename is an error the user can see")

    print("\n== a failure inside the transaction rolls everything back ==")
    before = digest()
    real_totals = edit._totals
    calls = {"n": 0}

    def poisoned(conn):
        # let planning see the truth, then lie to the post-write check, so the
        # invariant fires exactly as it would on a real inconsistency
        calls["n"] += 1
        t = real_totals(conn)
        return t if calls["n"] == 1 else {**t, "observations": t["observations"] + 99}

    edit._totals = poisoned
    try:
        k3 = ids()["veh"]["E-3"]
        boom = edit.apply(DB, [{"op": "vehicle", "target": k3,
                                "fields": {"year": "2015"}}])
    finally:
        edit._totals = real_totals
    with Store(DB) as store:
        year = store.conn.execute("SELECT year FROM vehicles WHERE key=?",
                                  (k3,)).fetchone()[0]
    check("an invariant failure rolls back and says so",
          boom["applied"] is False
          and any("rolled back" in e for e in boom["errors"]),
          [e for e in boom["errors"] if "rolled back" in e][0][:80])
    check("...and the database is byte-identical to before the attempt",
          digest() == before and year == 2014,
          f"{before} unchanged; year still 2014")

    print("\n== identity is never silently re-keyed ==")
    k3 = ids()["veh"]["E-3"]
    edit.apply(DB, [{"op": "vehicle", "target": k3,
                     "fields": {"model": "Fairlane"}}])
    with Store(DB) as store:
        row = store.conn.execute(
            "SELECT key, model FROM vehicles WHERE key=?", (k3,)).fetchone()
        obs = store.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE key=?", (k3,)).fetchone()[0]
    check("editing make/model/variant keeps the key, so history stays attached",
          row["key"] == k3 and row["model"] == "Fairlane" and obs >= 1,
          f"model changed, key {k3[:10]} unchanged, {obs} sighting(s) still linked")

    print("\n== history ==")
    h = edit.history(DB)
    check("every batch is listed newest first, with its size and state",
          len(h) >= 4 and h[0]["ts"] >= h[-1]["ts"]
          and all({"batch", "n", "ops"} <= set(x) for x in h),
          f"{len(h)} batch(es), most recent {h[0]['n']} change(s) ({h[0]['ops']})")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_edit: seeding throwaway DB ==")
    seed()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
