"""The watchlist: following specific cars across captures.

    python -m tests.check_starred

A star is the user's own annotation, so the properties that matter are as
much about what it must NOT do as what it does: no statistic may move because
a car was starred, re-importing must not disturb it, and a car that has gone
must be reported rather than quietly dropped.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from vpa import starred
from vpa.explore import detail_payload
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "starred_test.db"
D1, D2, D3 = "2026-03-01", "2026-03-08", "2026-03-15"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def seed() -> None:
    """Ten XR6s so the rank gate (8 peers) opens, captured on three days.

    Car 0 holds its price while the trim falls, which is the case the
    watchlist exists for: flat in a falling market is a relative RISE, and a
    bare percentage would call it "unchanged".
    Car 1 is dropped from the last sweep, so it must read as gone.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []
    for i in range(10):
        for day in (D1, D2, D3):
            if i == 1 and day == D3:
                continue                      # delisted before the last sweep
            price = 30000 - i * 1000
            if day != D1 and i != 0:
                price = int(price * 0.90)     # the trim falls 10%
            raw.append({"listing_id": f"S-{i}", "make": "Ford",
                        "model": "Falcon", "variant": "XR6", "year": 2014,
                        "price": price, "odometer": 100000 + i * 5000,
                        "state": "NSW", "seller_type": "dealer",
                        "capture_date": day})
    recs = [r for r in (make_record(d, source="examplecars") for d in raw) if r]
    with Store(DB) as store:
        store.ingest(recs, source_file="(star-test)", adapter="test")


def key_of(listing: str) -> str:
    with Store(DB) as store:
        return store.conn.execute(
            "SELECT key FROM vehicles WHERE listing_id = ?", (listing,)).fetchone()[0]


def run_checks() -> int:
    k0, k1 = key_of("S-0"), key_of("S-1")

    print("== starring is a write, and only to the watchlist ==")
    before = hashlib.sha256(DB.read_bytes()).hexdigest()[:16]
    stats_before = detail_payload(DB, "Ford", "Falcon", "XR6")["head"]
    r = starred.star(DB, k0, note="the one I drove")
    stats_after = detail_payload(DB, "Ford", "Falcon", "XR6")["head"]
    check("a car can be starred",
          r["ok"] and r["total"] == 1, f"{r['total']} car followed")
    check("...and NOTHING the engines compute changes because of it",
          stats_before == stats_after,
          f"median ${stats_after['median']:,.0f}, {stats_after['vehicles']} "
          f"vehicles, {stats_after['observations']} observations - identical")
    check("starring an unknown car is refused, not silently stored",
          starred.star(DB, "no-such-key")["ok"] is False,
          starred.star(DB, "no-such-key")["error"])
    check("the star is visible to the Explore payload, per row",
          [v["starred"] for v in
           detail_payload(DB, "Ford", "Falcon", "XR6")["vehicles"]
           if v["key"] == k0] == [True],
          "the row carries starred=True; every other row is False")

    print("\n== what happened to it since the last capture ==")
    starred.star(DB, k1, note="delisted one")
    w = starred.watchlist_payload(DB)
    car0 = next(c for c in w["cars"] if c["key"] == k0)
    car1 = next(c for c in w["cars"] if c["key"] == k1)
    check("a followed car reports its position in its own trim",
          car0["rank"] and car0["rank"]["rank"] == 10 and car0["peers"] == 9,
          f"{car0['label']}: {car0['rank']['rank']} of {car0['rank']['n'] + 1}, "
          f"${car0['price']:,.0f} against a trim median of "
          f"${car0['trim_median']:,.0f}")
    s0 = car0["since"]
    check("a car that held its price in a falling market shows the RELATIVE move",
          s0["move"] == 0.0 and s0["trim_move"] < 0
          and s0["vs_trim"] == round(s0["move"] - s0["trim_move"], 1)
          and s0["vs_trim"] > 0,
          f"its own move {s0['move']}%, its trim {s0['trim_move']}%, so it "
          f"rose {s0['vs_trim']}% against the trim - the number a bare "
          f"percentage reports as 'unchanged'")
    check("a car missing from the latest sweep is reported as such",
          car1["present"] and car1["live"] is False
          and car1["trim_last_day"] == D3,
          f"{car1['label']} last seen {car1['last_day']}, its trim swept {D3}")
    check("...and the summary counts it",
          w["not_in_latest"] == 1 and w["total"] == 2,
          f"{w['not_in_latest']} of {w['total']} not in their trim's latest sweep")

    print("\n== a new capture updates the watchlist, nothing else needed ==")
    add = [make_record({"listing_id": "S-0", "make": "Ford", "model": "Falcon",
                        "variant": "XR6", "year": 2014, "price": 26000,
                        "odometer": 100000, "state": "NSW",
                        "seller_type": "dealer",
                        "capture_date": "2026-03-22"}, source="examplecars")]
    with Store(DB) as store:
        store.ingest(add, source_file="(star-test-2)", adapter="test")
    w2 = starred.watchlist_payload(DB)
    c0 = next(c for c in w2["cars"] if c["key"] == k0)
    check("the newest sighting is picked up with no further action",
          c0["price"] == 26000.0 and c0["last_day"] == "2026-03-22"
          and c0["since"]["capture_day"] == "2026-03-22",
          f"now ${c0['price']:,.0f}, moved {c0['since']['move']}% since "
          f"{c0['since']['previous_day']}")
    check("...and its change since you first saw it is kept separately",
          c0["change"] == -13.3 and c0["first_price"] == 30000.0,
          f"first seen ${c0['first_price']:,.0f} on {c0['first_day']}, "
          f"{c0['change']}% since")

    print("\n== a star survives what happens to the data ==")
    # VACUOUS-GUARD: re-ingesting must not disturb the watchlist. If starring
    # were stored on `vehicles`, an UPDATE on re-import could clear it.
    with Store(DB) as store:
        store.ingest(add, source_file="(star-test-3)", adapter="test")
    check("re-importing the same rows leaves the stars alone",
          starred.keys(DB) == {k0, k1},
          f"{len(starred.keys(DB))} star(s) intact after a re-ingest")
    with Store(DB) as store:
        store.conn.execute("DELETE FROM vehicles WHERE key = ?", (k1,))
        store.conn.commit()
    w3 = starred.watchlist_payload(DB)
    lost = next(c for c in w3["cars"] if c["key"] == k1)
    check("a star whose car has gone is REPORTED, not dropped",
          lost["present"] is False and w3["gone"] == 1
          and "removed or merged" in lost["why"],
          "the disappearance is the finding, so it survives to be shown")
    check("...and unfollowing it removes it cleanly",
          starred.star(DB, k1, on=False)["total"] == 1
          and starred.keys(DB) == {k0},
          "one star left")

    print("\n== an empty watchlist says so, rather than erroring ==")
    starred.star(DB, k0, on=False)
    empty = starred.watchlist_payload(DB)
    check("no stars is a normal state with a message",
          empty["ok"] and empty["total"] == 0 and "nothing starred" in empty["reason"],
          empty["reason"][:64])

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_starred: seeding throwaway DB ==")
    seed()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
