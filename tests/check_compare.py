"""Verification for the Compare page (vpa/compare.py).

    python -m tests.check_compare      (from vehicle-price-analysis/)

Builds a throwaway database with KNOWN content and asserts:

  * the catalogue lists every selection at all three levels, and each level
    pools exactly the rows it should - make, make+model, make+model+trim;
  * every stat in the side-by-side block equals the same computation done
    independently here. These are the numbers the workbook's Comparison sheet
    computes with formulas, so if they drift the two views disagree;
  * rank and "vs cheapest" are relative to the columns on screen, which is
    what the workbook's RANK over $B$10:$G$10 does;
  * a label that does not exist is REPORTED, not silently dropped - a typo
    that quietly compares nothing is worse than an error;
  * duplicates and over-long selection lists are handled;
  * the month series uses the deduped basis (a vehicle seen twice in a month
    counts once), matching analyze.bucket_stats and `vpa report`;
  * comparing writes nothing.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from vpa import analyze
from vpa.compare import (CMP_ROWS, MAX_SCATTER_PER_COLUMN, MAX_SELECTIONS,
                         catalogue_payload, compare_payload, resolve,
                         selection_index)
from vpa.importui import status_payload
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "compare_test.db"

RESULTS: list[tuple[bool, str]] = []

DAY_A, DAY_B = "2026-03-08", "2026-04-12"
#: four separate months, so at least one selection can be forecast
LONG_DAYS = ["2026-01-11", "2026-02-08", DAY_A, DAY_B]


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def seed() -> None:
    """Known content, chosen so every stat has a hand-checkable answer.

    Ford Falcon XR6  4 cars, both days, one drops 20,000 -> 18,000
    Ford Falcon XR8  2 cars, day B only, dealer
    Ford Territory   2 cars, day A only  (so 'Ford' > 'Ford Falcon')
    Holden Commodore 3 cars, both days, cheaper than everything Ford
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []
    for i, (pa, pb) in enumerate([(20000, 18000), (16000, 16000),
                                  (22000, 22000), (14000, 14000)]):
        for day, price in ((DAY_A, pa), (DAY_B, pb)):
            raw.append({"listing_id": f"XR6-{i}", "make": "Ford",
                        "model": "Falcon", "variant": "XR6", "year": 2012 + i % 2,
                        "price": price, "odometer": 120000 + i * 15000,
                        "state": "NSW", "seller_type": "private",
                        "capture_date": day})
    for i, price in enumerate((41000, 45000)):
        raw.append({"listing_id": f"XR8-{i}", "make": "Ford", "model": "Falcon",
                    "variant": "XR8", "year": 2014, "price": price,
                    "odometer": 90000 + i * 10000, "state": "VIC",
                    "seller_type": "dealer", "capture_date": DAY_B})
    for i, price in enumerate((9000, 11000)):
        raw.append({"listing_id": f"TER-{i}", "make": "Ford",
                    "model": "Territory", "variant": "TX", "year": 2010,
                    "price": price, "odometer": 210000, "state": "QLD",
                    "seller_type": "private", "capture_date": DAY_A})
    for i, price in enumerate((8000, 9500, 12000)):
        for day in (DAY_A, DAY_B):
            raw.append({"listing_id": f"VE-{i}", "make": "Holden",
                        "model": "Commodore", "variant": "Omega", "year": 2011,
                        "price": price, "odometer": 180000 + i * 5000,
                        "state": "SA", "seller_type": "dealer", "capture_date": day})
    # A selection with FOUR monthly buckets and 16 prices, so the forecast
    # ladder actually climbs past "insufficient". Without one, every
    # assertion about forecast points passes vacuously on an empty list.
    for i in range(4):
        for k, day in enumerate(LONG_DAYS):
            raw.append({"listing_id": f"HLX-{i}", "make": "Toyota",
                        "model": "Hilux", "variant": "SR5", "year": 2016,
                        "price": 40000 + i * 2000 - k * 500,
                        "odometer": 95000 + i * 8000 + k * 1500,
                        "state": "WA", "seller_type": "dealer",
                        "capture_date": day})
    # A trim with enough distinct cars to carry a fitted bell curve
    # (analyze.CURVE_MIN_N is 12); nothing else in this fixture reaches it,
    # so without this the curve assertions would only ever see a refusal.
    # 18 cars, of which 14 appear again on DAY_B: the filter genuinely bites
    # (18 -> 14) while BOTH sides still clear CURVE_MIN_N, so the "refit on
    # the survivors" assertion has two real curves to compare.
    for i in range(18):
        days = (DAY_A, DAY_B) if i >= 4 else (DAY_A,)
        for day in days:
            raw.append({"listing_id": f"BT-{i}", "make": "Mazda",
                        "model": "BT-50", "variant": "XT", "year": 2015,
                        "price": 28000 + i * 1100, "odometer": 90000 + i * 6000,
                        "state": "NSW", "seller_type": "dealer",
                        "capture_date": day})

    recs = [r for r in (make_record(d, source="carsales") for d in raw) if r]
    with Store(DB) as store:
        store.ingest(recs, source_file="(compare-test)", adapter="test")


def frame() -> pd.DataFrame:
    from vpa.explore import _frame
    return _frame(DB)


def _frame_for(df, make, model, variant):
    from vpa.explore import _select
    return _select(df, make, model, variant)


def run_checks() -> int:
    df = frame()

    print("== the catalogue lists every selection, at all three levels ==")
    cat = catalogue_payload(DB)
    labels = {r["label"]: r for r in cat["rows"]}
    check("makes, model lines and trims are all listed and labelled",
          cat["levels"] == {"make": 4, "line": 5, "trim": 6}
          and labels["Ford"]["level"] == "make"
          and labels["Ford Falcon"]["level"] == "line"
          and labels["Ford Falcon XR6"]["level"] == "trim",
          f"{cat['levels']}, {len(cat['rows'])} rows")
    check("a make pools every model under it (Falcon 10 + Territory 2 = 12)",
          labels["Ford"]["n"] == 12 and labels["Ford Falcon"]["n"] == 10
          and labels["Ford Territory"]["n"] == 2,
          f"Ford={labels['Ford']['n']}, Falcon={labels['Ford Falcon']['n']}, "
          f"Territory={labels['Ford Territory']['n']}")
    check("catalogue counts equal an independent count over the same rows",
          labels["Ford Falcon XR6"]["n"] == int(len(df[df["variant"] == "XR6"]))
          and labels["Ford Falcon XR6"]["vehicles"] == 4
          and labels["Ford Falcon XR6"]["days"] == 2,
          f"XR6 n={labels['Ford Falcon XR6']['n']}, "
          f"cars={labels['Ford Falcon XR6']['vehicles']}, "
          f"days={labels['Ford Falcon XR6']['days']}")

    print("\n== a label resolves the same way the workbook's Match would ==")
    index = selection_index(df)
    check("all three levels resolve, and case/space differences still resolve",
          resolve("Ford", index) == ("Ford", None, None, "make")
          and resolve("Ford Falcon", index) == ("Ford", "Falcon", None, "line")
          and resolve("  ford   falcon xr8 ", index)
          == ("Ford", "Falcon", "XR8", "trim"),
          "make / line / trim all resolved, including a messy trim label")
    check("a label that does not exist resolves to nothing",
          resolve("Ford Falcon XR9", index) is None
          and resolve("", index) is None,
          "unknown and empty both return None")

    print("\n== every stat equals an independent computation ==")
    d = compare_payload(DB, ["Ford Falcon XR6", "Holden Commodore",
                             "Ford Falcon XR8"])
    col = {c["label"]: c for c in d["columns"]}
    xr6 = col["Ford Falcon XR6"]
    own = df[df["variant"] == "XR6"]
    p = own["price_aud"]
    check("counts: n, full/brief, distinct vehicles and capture days",
          (xr6["n"], xr6["full"], xr6["brief"], xr6["vehicles"], xr6["days"])
          == (int(len(own)), int(len(own)), 0, 4, 2),
          f"n={xr6['n']} full={xr6['full']} brief={xr6['brief']} "
          f"cars={xr6['vehicles']} days={xr6['days']}")
    check("price shape: median, mean, quartiles, range and spread",
          xr6["median"] == float(p.median()) and xr6["mean"] == round(float(p.mean()), 2)
          and xr6["q25"] == float(p.quantile(0.25))
          and xr6["q75"] == float(p.quantile(0.75))
          and xr6["lo"] == float(p.min()) and xr6["hi"] == float(p.max())
          and xr6["spread"] == float(p.max() - p.min()),
          f"median {xr6['median']} (pandas {float(p.median())}), "
          f"range {xr6['lo']}-{xr6['hi']}, spread {xr6['spread']}")
    exp_odo = float(own["odometer_km"].median())
    check("wear and age: median odometer, $ per 10,000 km, median year",
          xr6["median_odo"] == exp_odo
          and xr6["per_10k"] == round(xr6["median"] / (exp_odo / 10000), 2)
          and xr6["median_year"] == float(own["year"].median()),
          f"odo {xr6['median_odo']:,.0f} km, ${xr6['per_10k']:,.0f}/10k km, "
          f"year {xr6['median_year']}")
    check("seller split counts dealer and private separately",
          (xr6["dealer"], xr6["private"]) == (0, 8)
          and (col["Ford Falcon XR8"]["dealer"],
               col["Ford Falcon XR8"]["private"]) == (2, 0),
          f"XR6 {xr6['dealer']}/{xr6['private']} dealer/private, "
          f"XR8 {col['Ford Falcon XR8']['dealer']}/"
          f"{col['Ford Falcon XR8']['private']}")

    print("\n== movement uses the first and latest capture day ==")
    first = float(own[own["day"] == DAY_A]["price_aud"].median())
    last = float(own[own["day"] == DAY_B]["price_aud"].median())
    check("first/latest capture, their medians, and the change between them",
          xr6["first_day"] == DAY_A and xr6["last_day"] == DAY_B
          and xr6["median_first"] == first and xr6["median_last"] == last
          and xr6["change"] == round((last - first) / first * 100, 1),
          f"{first} on {DAY_A} -> {last} on {DAY_B} = {xr6['change']}%")
    xr8 = col["Ford Falcon XR8"]
    check("one capture day reports no change rather than 0%",
          xr8["change"] is None and xr8["first_day"] == xr8["last_day"],
          f"XR8 seen only on {xr8['first_day']}, change={xr8['change']}")

    print("\n== rank and 'vs cheapest' are relative to the columns on screen ==")
    meds = {c["label"]: c["median"] for c in d["columns"]}
    cheap = min(meds.values())
    check("rank 1 is the cheapest median of the selections compared",
          col["Holden Commodore"]["rank"] == 1 and xr6["rank"] == 2
          and xr8["rank"] == 3,
          f"{ {k: v['rank'] for k, v in col.items()} } for medians {meds}")
    check("'vs cheapest' is measured against that same set",
          col["Holden Commodore"]["vs_cheapest"] == 0.0
          and xr6["vs_cheapest"] == round((xr6["median"] / cheap - 1) * 100, 1),
          f"XR6 is {xr6['vs_cheapest']}% above the cheapest (${cheap:,.0f})")
    two = compare_payload(DB, ["Ford Falcon XR6", "Ford Falcon XR8"])
    two_col = {c["label"]: c for c in two["columns"]}
    check("dropping a column re-ranks the rest (it is not a global rank)",
          two_col["Ford Falcon XR6"]["rank"] == 1
          and two_col["Ford Falcon XR6"]["vs_cheapest"] == 0.0,
          f"without Holden, XR6 becomes rank "
          f"{two_col['Ford Falcon XR6']['rank']}")

    print("\n== the month series uses the same engine as vpa report ==")
    prepped = analyze.prep(df[df["variant"] == "XR6"], freq="MS")
    stats = analyze.bucket_stats(prepped)
    check("monthly medians equal analyze.bucket_stats on the same rows",
          [m["median"] for m in xr6["months"]] == [float(v) for v in stats["median"]],
          f"page={[m['median'] for m in xr6['months']]} "
          f"engine={list(stats['median'])}")
    check("a vehicle seen twice in one month is counted once there",
          all(m["n"] <= xr6["vehicles"] for m in xr6["months"]),
          f"months n={[m['n'] for m in xr6['months']]}, "
          f"{xr6['vehicles']} distinct cars")
    check("the shared month axis covers history AND projected months",
          d["months"] == sorted({m["d"] for c in d["columns"] for m in c["months"]}
                                | {p["d"] for c in d["columns"]
                                   for p in c["fc_points"]}),
          f"axis={d['months']}")

    print("\n== the chart series carry what the charts need ==")
    check("per-capture-day supply and median are sent for every column",
          [b["d"] for b in xr6["byday"]] == [DAY_A, DAY_B]
          and xr6["byday"][0]["vehicles"] == 4
          and xr6["byday"][0]["median"] == float(
              own[own["day"] == DAY_A]["price_aud"].median()),
          f"XR6 {[(b['d'], b['vehicles'], b['median']) for b in xr6['byday']]}")
    check("the shared day axis is the union of every column's capture days",
          d["days"] == sorted({b["d"] for c in d["columns"] for b in c["byday"]})
          and d["days"] == [DAY_A, DAY_B],
          f"axis={d['days']}")
    check("scatter points carry a price and an odometer, capped per column",
          all(p["odo"] and p["price"] for p in xr6["scatter"])
          and len(xr6["scatter"]) == xr6["scatter_total"] == 8
          and len(xr8["scatter"]) <= MAX_SCATTER_PER_COLUMN,
          f"XR6 {len(xr6['scatter'])}/{xr6['scatter_total']} points, "
          f"cap {MAX_SCATTER_PER_COLUMN}")
    # A selection that CAN be forecast, so these assertions are not `all()`
    # over an empty list - which is how a forecast check quietly passes while
    # the chart draws nothing.
    hilux = compare_payload(DB, ["Toyota Hilux", "Ford Falcon XR8"])
    hx = next(c for c in hilux["columns"] if c["label"] == "Toyota Hilux")
    hx8 = next(c for c in hilux["columns"] if c["label"] == "Ford Falcon XR8")
    check("a selection with 4 monthly buckets IS forecast, with an interval",
          hx["fc_method"] != "insufficient" and len(hx["fc_points"]) == 3
          and all(p["lo"] is not None and p["hi"] is not None
                  and p["lo"] <= p["v"] <= p["hi"] for p in hx["fc_points"])
          and hx["fc_interval"] == 80,
          f"Hilux {hx['fc_method']}: "
          f"{[(p['d'], p['v'], p['lo'], p['hi']) for p in hx['fc_points']]}")
    check("a thin selection is refused rather than guessed at",
          hx8["fc_points"] == [] and hx8["fc_method"] == "insufficient",
          f"XR8 ({hx8['n']} prices, {hx8['days']} day) -> {hx8['fc_method']}")
    check("a projected month never collides with an observed one",
          not ({p["d"] for p in hx["fc_points"]}
               & {m["d"] for m in hx["months"]}),
          f"history {[m['d'] for m in hx['months']]} then "
          f"projection {[p['d'] for p in hx['fc_points']]}")
    check("the month axis places the projection AFTER the history",
          hilux["months"] == sorted(hilux["months"])
          and hilux["months"][-3:] == [p["d"] for p in hx["fc_points"]],
          f"axis={hilux['months']}")

    print("\n== bell curves, and the 'latest capture only' filter ==")
    bt = compare_payload(DB, ["Mazda BT-50 XT", "Ford Falcon XR8"])
    bcol = next(c for c in bt["columns"] if c["label"] == "Mazda BT-50 XT")
    b8 = next(c for c in bt["columns"] if c["label"] == "Ford Falcon XR8")
    bt_pool = analyze.latest_per_vehicle(
        analyze.prep(_frame_for(df, "Mazda", "BT-50", "XT"), freq="MS"))
    own = analyze.price_curve(bt_pool["price_aud"])
    check("each column's curve is the shared engine's, over one price per car",
          bcol["curve"]["usable"] and bcol["curve"]["mu"] == own["mu"]
          and bcol["curve"]["n"] == own["n"] == int(len(bt_pool))
          and bcol["curve"]["n"] < bcol["n"],
          f"n={bcol['curve']['n']} cars (not {bcol['n']} sightings), "
          f"median ${bcol['curve']['median']:,.0f}")
    # VACUOUS-GUARD: "not usable" alone would pass on a missing key, so the
    # reason and the count are asserted too
    check("a thin column is refused a curve rather than given a fake one",
          b8["curve"]["usable"] is False
          and str(analyze.CURVE_MIN_N) in b8["curve"]["reason"],
          f"XR8 ({b8['curve']['n']} car(s)) -> {b8['curve']['reason']}")

    live = compare_payload(DB, ["Mazda BT-50 XT", "Ford Falcon XR6"],
                           live_only=True)
    lb = next(c for c in live["columns"] if c["label"] == "Mazda BT-50 XT")
    check("the live filter is reported back, so the page cannot mislabel itself",
          live["live_only"] is True
          and compare_payload(DB, ["Mazda BT-50 XT"])["live_only"] is False,
          f"live_only={live['live_only']}")
    check("only the cars in the last capture survive the filter",
          lb["vehicles"] == 14 and bcol["vehicles"] == 18
          and lb["live_day"] == DAY_B,
          f"BT-50: {bcol['vehicles']} cars all time -> {lb['vehicles']} in "
          f"the {lb['live_day']} capture")
    lxr6 = next(c for c in live["columns"] if c["label"] == "Ford Falcon XR6")
    axr6 = next(c for c in compare_payload(
        DB, ["Ford Falcon XR6"])["columns"] if c["label"] == "Ford Falcon XR6")
    check("filtering keeps a live car's FULL history, not just the last row",
          lxr6["n"] == axr6["n"] and lxr6["days"] == 2,
          f"XR6 has all 4 cars on both days -> {lxr6['n']} sightings kept "
          f"across {lxr6['days']} capture days")
    check("the filtered curve is refit on the survivors, not reused",
          lb["curve"]["usable"] and lb["curve"]["n"] == 14
          and lb["curve"]["mu"] != bcol["curve"]["mu"],
          f"all-time mu {bcol['curve']['mu']:.4f} over {bcol['curve']['n']} "
          f"cars -> live mu {lb['curve']['mu']:.4f} over {lb['curve']['n']}")

    print("\n== bad input is reported, not silently swallowed ==")
    bad = compare_payload(DB, ["Ford Falcon XR6", "Ford Falcon XR9"])
    check("an unknown label is listed in `unknown` and produces no column",
          bad["unknown"] == ["Ford Falcon XR9"] and len(bad["columns"]) == 1,
          f"unknown={bad['unknown']}, {len(bad['columns'])} column(s)")
    dup = compare_payload(DB, ["Ford Falcon XR6", "ford falcon xr6"])
    check("the same selection asked for twice yields one column",
          len(dup["columns"]) == 1,
          f"{len(dup['columns'])} column from two spellings of one selection")
    many = compare_payload(DB, [r["label"] for r in cat["rows"]])
    check(f"no more than {MAX_SELECTIONS} columns are ever returned",
          len(many["columns"]) <= MAX_SELECTIONS,
          f"asked for {len(cat['rows'])}, got {len(many['columns'])}")
    check("every stat row the page renders exists on every column",
          all(k in c for k, _lab, _kind, _grp in CMP_ROWS
              for c in d["columns"]),
          f"{len(CMP_ROWS)} declared rows, all present on "
          f"{len(d['columns'])} columns")

    print("\n== comparing writes nothing ==")
    before = status_payload(DB)
    for labels_ in ([], ["Ford"], ["Ford Falcon", "Holden Commodore"]):
        compare_payload(DB, labels_)
    catalogue_payload(DB)
    after = status_payload(DB)
    check("read-only: no counts and no version changed across 4 reads",
          (before["vehicles"], before["observations"], before["data_version"])
          == (after["vehicles"], after["observations"], after["data_version"]),
          f"version {after['data_version']} unchanged")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_compare: seeding throwaway DB ==")
    seed()
    with Store(DB) as store:
        s = store.status()
    print(f"  {s['vehicles']} vehicles, {s['observations']} observations\n")
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
