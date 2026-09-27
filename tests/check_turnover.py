"""Turnover: what left, what arrived, and every case where it must refuse.

    python -m tests.check_turnover

The whole value of this module is the refusals. Anyone can subtract two
capture counts and call the difference "sold"; the checks below are almost
all about the four ways that number is a lie, each seeded deliberately:

  1. a car with no site ad id cannot be recognised across captures at all;
  2. two captures of DIFFERENT SEARCHES share no cars, which is a scope
     change and not a market that emptied;
  3. one capture much bigger than its neighbour inflates arrivals (a wider
     search), and much smaller inflates exits (a narrower one) - and those
     two failures need OPPOSITE treatment, not one blanket rule;
  4. a page filter can push a car out of view and make it look delisted.

Plus the arithmetic that must be right for the numbers to be comparable at
all: exits per car-DAY, not per capture, because the real captures are 1 to
22 days apart.
"""
from __future__ import annotations

import sys
from pathlib import Path

from vpa import turnover
from vpa.explore import _frame, _select, detail_payload
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "turnover_test.db"

#: weekly captures, so a 30-day rate is an interpolation and not a leap
DAYS = ["2026-03-01", "2026-03-08", "2026-03-15", "2026-03-22", "2026-03-29"]

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def rec(listing, variant, day, price, odo=120000, year=2014, model="Falcon"):
    return {"listing_id": listing, "make": "Ford", "model": model,
            "variant": variant, "year": year, "price": price,
            "odometer": odo, "state": "NSW", "seller_type": "dealer",
            "capture_date": day}


def seed() -> None:
    """Four trims, each seeded to exercise one behaviour.

    STEADY  - 12 cars, weekly. Three leave, cheapest first, so the price
              signal has a known sign. Two arrive late.
    NOID    - the same shape but with ids the sites never issue, so nothing
              is trackable.
    SCOPE   - two captures that share no car at all (different searches).
    NARROW  - a big capture followed by a tiny one: the "missing" cars were
              never looked for.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []

    # -- STEADY: cars 0..11 up on every day, except three that leave.
    # Leavers are the three CHEAPEST, so `exit_rel` must come out negative.
    # Car 2 both ARRIVES and LEAVES inside the window, which is the only
    # shape that has a measured upper edge on its time listed - without it
    # the range check below would pass on an empty list.
    leaves = {0: DAYS[1], 1: DAYS[2], 2: DAYS[2]}     # key -> last day seen
    arrives = {2: DAYS[1], 10: DAYS[3], 11: DAYS[3]}  # key -> first day seen
    for i in range(12):
        price = 20000 + i * 1000                     # 0 is cheapest
        for day in DAYS:
            if i in leaves and day > leaves[i]:
                continue
            if i in arrives and day < arrives[i]:
                continue
            raw.append(rec(f"SSE-AD-{9000 + i}", "STEADY", day, price,
                           odo=100000 + i * 1000))

    # -- NOID: image-derived ids, which are not unique per listing
    for i in range(12):
        for day in DAYS:
            raw.append(rec(f"IMG-{7000 + i:x}", "NOID", day, 20000 + i * 100,
                           odo=140000 + i * 700))

    # -- SCOPE: eight cars on day 0, eight DIFFERENT cars on day 3
    for i in range(8):
        raw.append(rec(f"SSE-AD-{5000 + i}", "SCOPE", DAYS[0], 30000 + i * 500))
    for i in range(8):
        raw.append(rec(f"SSE-AD-{6000 + i}", "SCOPE", DAYS[3], 31000 + i * 500))

    # -- NARROW: 12 cars, then a capture holding only 2 of them
    for i in range(12):
        raw.append(rec(f"SSE-AD-{4000 + i}", "NARROW", DAYS[0], 25000 + i * 400))
    for i in range(2):
        raw.append(rec(f"SSE-AD-{4000 + i}", "NARROW", DAYS[2], 25000 + i * 400))

    recs = [r for r in (make_record(d, source="carsales") for d in raw) if r]
    with Store(DB) as store:
        store.ingest(recs, source_file="(turnover-test)", adapter="test")


def sub(variant):
    return _select(_frame(DB), "Ford", "Falcon", variant)


def run_checks() -> int:
    df = _frame(DB)

    # ---------------------------------------------------------------- steady
    print("\n== a clean run of captures: what left, what arrived ==")
    s = sub("STEADY")
    t = turnover.turnover(s, s, label="STEADY")
    check("a trim captured weekly with stable coverage IS rateable",
          t["usable"] and t["span"]["captures"] == 5,
          f"{t['span']['captures']} captures, {t['span']['days']} days, "
          f"usable={t['usable']}")
    check("the three delisted cars are found, and only those three",
          t["exits"] == 3, f"exits={t['exits']}, arrivals={t['arrivals']}, "
          f"still up={t['still']}")
    check("the three cars that appeared later are counted as arrivals",
          t["arrivals"] == 3, f"arrivals={t['arrivals']}")
    check("nothing that is still listed is counted as having left",
          t["still"] == 9 and len(t["departed"]) == 3,
          f"{t['still']} still up at {t['span']['end']}, "
          f"{len(t['departed'])} departed")

    # ---- the arithmetic that makes two trims comparable
    print("\n== the rate is per car-DAY, not per capture ==")
    # 12 at risk on day 0, then 11, 11, 11 (car 0 gone, 10/11 arrive late and
    # are not at risk until they appear) - exercised as a property, not a
    # hand-computed constant, so the check survives a reseeding
    car_days = sum(i["start_n"] * i["elapsed"] for i in t["intervals"]
                   if i["counts_exits"])
    check("car-days equal the sum of (cars up) x (days to the next capture)",
          t["car_days"] == car_days and t["car_days"] > 0,
          f"car_days={t['car_days']} over {len(t['intervals'])} intervals")
    check("hazard is exits divided by car-days",
          abs(t["hazard"] - t["exits"] / t["car_days"]) < 1e-4,
          f"{t['exits']}/{t['car_days']} = {t['exits'] / t['car_days']:.5f} "
          f"vs reported {t['hazard']}")
    check("the 30-day rate follows the hazard, and stays under 100%",
          0 < t["rate30"] < 100 and t["half_life"] > 0,
          f"rate30={t['rate30']}%, half-life={t['half_life']} days")

    print("\n== time listed is a RANGE, never a single measured number ==")
    d0 = [v for v in t["departed"] if v["first_day"] == DAYS[0]]
    check("a car already up at the first capture has no upper edge",
          all(v["censored"] and v["days_max"] is None for v in d0),
          f"{len(d0)} left-censored of {len(t['departed'])}")
    later = [v for v in t["departed"] if not v["censored"]]
    check("a car that arrived AND left is bracketed, floor below ceiling",
          bool(later) and all(0 <= v["days_listed"] < v["days_max"]
                              and v["days_listed"] <= v["days_est"] <= v["days_max"]
                              for v in later),
          "; ".join(f"{v['days_listed']}-{v['days_max']}d (est {v['days_est']})"
                    for v in later) or "NONE - the check would be vacuous")

    print("\n== was price what decided which cars went ==")
    ps = t["price_signal"]
    check("the cheapest cars leaving shows up as a NEGATIVE relative price",
          ps["exit_rel"] is not None and ps["exit_rel"] < 0,
          f"what left sat {ps['exit_rel']}% against its own day's median, "
          f"what stayed {ps['still_rel']}%")
    check("the spread points the same way",
          ps["spread"] is not None and ps["spread"] < 0,
          f"spread={ps['spread']}%")

    # ------------------------------------------------------------ refusal 1
    print("\n== a car with no site ad id cannot be tracked at all ==")
    n = sub("NOID")
    tn = turnover.turnover(n, n, label="NOID")
    check("a trim whose ids are image-derived is refused, not estimated",
          not tn["usable"] and "site ad id" in tn["reason"],
          tn["reason"][:70])
    check("the untrackable cars are counted out loud",
          tn["cohort_cars"] == 0 and tn["excluded_no_id"] == 12,
          f"cohort={tn['cohort_cars']}, excluded={tn['excluded_no_id']}")

    # ------------------------------------------------------------ refusal 2
    print("\n== two captures of different searches are not a market event ==")
    sc = sub("SCOPE")
    ts = turnover.turnover(sc, sc, label="SCOPE")
    check("captures sharing NO car break the span instead of reporting 100%",
          not ts["usable"] and len(ts["breaks"]) == 1
          and ts["breaks"][0]["carry"] == 0,
          f"breaks={len(ts['breaks'])}, carry={ts['breaks'][0]['carry'] if ts['breaks'] else '-'}, "
          f"reason={ts['reason'][:50]}")
    check("and no exit is invented for the eight cars that 'vanished'",
          ts.get("exits", 0) == 0 and not ts.get("departed"),
          f"exits={ts.get('exits', 0)}")

    # ------------------------------------------------------------ refusal 3
    print("\n== a smaller capture did not clear the market ==")
    nw = sub("NARROW")
    tw = turnover.turnover(nw, nw, label="NARROW")
    iv = tw.get("intervals") or []
    check("a capture a sixth the size of the one before is marked narrowed",
          bool(iv) and iv[0]["scope"] == "narrowed"
          and not iv[0]["counts_exits"],
          f"scope={iv[0]['scope'] if iv else '-'}, "
          f"{iv[0]['start_n'] if iv else '-'} then {iv[0]['end_n'] if iv else '-'}")
    check("its ten 'exits' are shown but never rated",
          not tw["usable"] and iv and iv[0]["exits"] == 10,
          f"shown={iv[0]['exits'] if iv else '-'}, usable={tw['usable']}, "
          f"reason={tw['reason'][:44]}")

    print("\n== the two scope failures get OPPOSITE treatment ==")
    check("widened keeps exits and drops arrivals; narrowed does the reverse",
          turnover._scope(10, 40) == "widened" and turnover._scope(40, 10) == "narrowed"
          and turnover._scope(20, 22) == "steady",
          "10->40 widened, 40->10 narrowed, 20->22 steady")

    # ------------------------------------------------------------ refusal 4
    print("\n== a filter must not manufacture a delisting ==")
    # Every STEADY car is priced 20,000-31,000. Cap the page at 27,000 and
    # cars 8..11 leave the VIEW on every day - which is not leaving the market
    hi = detail_payload(DB, "Ford", "Falcon", "STEADY", price_max="27000")
    lo = detail_payload(DB, "Ford", "Falcon", "STEADY")
    check("a price filter does not change how many cars left the market",
          hi["churn"]["exits"] == lo["churn"]["exits"] == 3,
          f"filtered={hi['churn']['exits']}, unfiltered={lo['churn']['exits']}")
    check("the filtered-out cars are reported as vetoed, not hidden",
          hi["churn"]["veto_checked"] and hi["churn"]["vetoed"] == 0,
          f"vetoed={hi['churn']['vetoed']} (none, because a capped price still "
          f"shows the car on later days)")

    # a car whose PRICE MOVES out of the cap is the real artefact: it is in
    # view on day 0 and out of view for ever after, which looks exactly like
    # a delisting until the unfiltered selection is consulted
    print("\n== a car that repriced out of the filter is not a delisting ==")
    with Store(DB) as store:
        store.ingest([make_record(rec("SSE-AD-9099", "STEADY", d,
                                      23000 if d == DAYS[0] else 29000),
                                  source="carsales") for d in DAYS],
                     source_file="(repricer)", adapter="test")
    cap = detail_payload(DB, "Ford", "Falcon", "STEADY", price_max="27000")
    check("it is vetoed by the unfiltered selection, not counted as an exit",
          cap["churn"]["vetoed"] == 1 and cap["churn"]["exits"] == 3,
          f"vetoed={cap['churn']['vetoed']}, exits={cap['churn']['exits']} "
          f"(unchanged)")

    # ------------------------------------------------------- the page's data
    print("\n== the chart gets a composition for every capture day ==")
    p = detail_payload(DB, "Ford", "Falcon", "STEADY")
    bd = p["byday"]
    check("each day carries cohort / carried over / new / last-seen",
          all(set(b) >= {"cohort", "carry", "new", "exits"} for b in bd),
          f"{len(bd)} days, first={bd[0]['d']} cohort={bd[0]['cohort']} "
          f"carry={bd[0]['carry']}")
    check("the first capture has nothing to be carried from, and says so",
          bd[0]["carry"] is None and bd[1]["carry"] is not None,
          f"day1 carry={bd[0]['carry']}, day2 carry={bd[1]['carry']}")
    check("carried over + new never exceeds the trackable cars that day",
          all((b["carry"] or 0) + (b["new"] or 0) <= b["cohort"] for b in bd),
          "; ".join(f"{b['d']}: {b['carry']}+{b['new']}<={b['cohort']}"
                    for b in bd[1:3]))
    check("the untrackable cars are the gap between cohort and the bar total",
          all(b["cohort"] <= b["vehicles"] for b in bd),
          f"e.g. {bd[-1]['d']}: {bd[-1]['cohort']} of {bd[-1]['vehicles']} trackable")

    print("\n== trims are compared on their own captures, then pooled ==")
    line = _select(df, "Ford", "Falcon", None)
    bv = turnover.by_variant(line, selected=["STEADY"], full_line=line)
    got = {r["variant"]: r for r in bv["rows"]}
    check("every trim appears, rateable or not, with its reason",
          set(got) == {"STEADY", "NOID", "SCOPE", "NARROW"}
          and got["STEADY"]["usable"] and not got["NOID"]["usable"],
          ", ".join(f"{k}={'ok' if v['usable'] else 'refused'}"
                    for k, v in sorted(got.items())))
    check("the selected trim is marked so the page can highlight it",
          got["STEADY"]["selected"] and not got["NARROW"]["selected"],
          "STEADY selected")
    check("the pooled row is built only from the trims that could be rated",
          bv["pooled"]["trims"] == 1 and bv["pooled"]["exits"] == got["STEADY"]["exits"],
          f"pooled over {bv['pooled']['trims']} trim(s), "
          f"{bv['pooled']['exits']} exits, {bv['pooled']['rate30']}%/30d")

    print("\n== nothing here claims a sale ==")
    words = " ".join([t["verdict"]] + t["notes"] + [t["reason"], tn["reason"],
                                                   ts["reason"], tw["reason"]])
    check("the word 'sold' appears nowhere in anything shown to the user",
          "sold" not in words.lower(),
          f"{len(words)} characters of verdict and notes checked")

    print("\n== an empty or single-capture selection degrades quietly ==")
    one = turnover.turnover(sub("STEADY").head(0), None)
    check("no rows is a stated reason, not an exception",
          not one["usable"] and one["reason"],
          one["reason"][:50])

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_turnover: seeding throwaway DB ==")
    seed()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
