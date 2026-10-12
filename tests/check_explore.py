"""Verification for the Explore page (vpa/explore.py) and data freshness.

    python -m tests.check_explore      (from vehicle-price-analysis/)

Builds a throwaway database with KNOWN content, including one deliberately
merged identity (two different cars that collapse onto one dedupe key), then
asserts:

  * the picker tree and the three selection levels pool exactly the rows they
    should - make, make+model, make+model+variant;
  * headline numbers equal the same computation done independently here, and
    the monthly buckets equal analyze.bucket_stats on the same rows, so the
    page cannot drift from `vpa report` or the workbook;
  * a genuine re-price IS reported as a movement, while the merged identity
    is flagged and excluded - the case that would otherwise publish a
    collision as a market signal;
  * exploring writes nothing;
  * data_version changes when new data lands, which is what lets an open page
    notice an import made somewhere else.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from vpa import analyze
from vpa.explore import detail_payload, picker_payload
from vpa.importui import status_payload
from vpa.schema import Record, make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "explore_test.db"

RESULTS: list[tuple[bool, str]] = []

DAY_A, DAY_B = "2026-04-05", "2026-04-19"


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def seed() -> None:
    """Known content:

    Ford Falcon XR6  - 3 cars with ad ids. Car #1 drops 20,000 -> 18,000
                       between the two days (a REAL movement). #2/#3 hold.
    Ford Falcon XR8  - 1 car, day B only (so trim selection must narrow).
    Ford Falcon G6E  - 2 DIFFERENT cars, no ad id, same year/variant/state,
                       odometers 300,100 and 302,900 km. Those share an
                       odometer bucket (both //5000 == 60), so the id-less
                       fingerprint merges them into ONE key holding two
                       prices on the SAME day - a collision, not a re-price.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []
    for i, (pa, pb) in enumerate([(20000, 18000), (15000, 15000), (17000, 17000)]):
        for day, price in ((DAY_A, pa), (DAY_B, pb)):
            raw.append({"listing_id": f"XR6-{i}", "make": "Ford", "model": "Falcon",
                        "variant": "XR6", "year": 2012, "price": price,
                        "odometer": 150000 + i * 20000, "state": "NSW",
                        "seller_type": "dealer", "capture_date": day})
    raw.append({"listing_id": "XR8-0", "make": "Ford", "model": "Falcon",
                "variant": "XR8", "year": 2014, "price": 41000,
                "odometer": 90000, "state": "VIC", "capture_date": DAY_B})
    for odo, price in ((300100, 9000), (302900, 12000)):
        raw.append({"make": "Ford", "model": "Falcon", "variant": "G6E",
                    "year": 2010, "price": price, "odometer": odo,
                    "state": "QLD", "capture_date": DAY_B})
    recs = [r for r in (make_record(d, source="examplecars") for d in raw) if r]
    # Seed through the OLD 5,000 km rule. Record.ODO_BUCKET_KM is 500 now, so
    # the G6E pair would no longer merge - but merged identities still exist
    # in databases written before the fix, and Explore has to handle them.
    legacy, Record.ODO_BUCKET_KM = Record.ODO_BUCKET_KM, 5000
    try:
        with Store(DB) as store:
            store.ingest(recs, source_file="(explore-test)", adapter="test")
    finally:
        Record.ODO_BUCKET_KM = legacy


def run_checks() -> int:
    print("== picker tree ==")
    tree = picker_payload(DB)
    ford = next((m for m in tree["makes"] if m["name"] == "Ford"), None)
    falcon = next((m for m in ford["models"] if m["name"] == "Falcon"), None)
    vnames = sorted(v["name"] for v in falcon["variants"])
    check("picker nests make -> model -> trim with counts",
          ford is not None and falcon is not None
          and vnames == ["G6E", "XR6", "XR8"],
          f"Ford/Falcon trims={vnames}, model n={falcon['n']}")

    print("\n== several trims pool into one set ==")
    both = detail_payload(DB, "Ford", "Falcon", variants=["XR6", "XR8"])
    only6 = detail_payload(DB, "Ford", "Falcon", variants=["XR6"])
    all_trims = detail_payload(DB, "Ford", "Falcon")
    check("two trims pooled hold exactly the sum of the two on their own",
          both["head"]["observations"] ==
          only6["head"]["observations"]
          + detail_payload(DB, "Ford", "Falcon", "XR8")["head"]["observations"],
          f"XR6 {only6['head']['observations']} + XR8 "
          f"{detail_payload(DB, 'Ford', 'Falcon', 'XR8')['head']['observations']}"
          f" = pooled {both['head']['observations']}")
    check("...and stay STRICTLY inside the model, not the whole line",
          both["head"]["observations"] < all_trims["head"]["observations"],
          f"XR6+XR8 {both['head']['observations']} < every Falcon trim "
          f"{all_trims['head']['observations']} (G6E excluded)")
    check("a pooled selection is labelled as pooled, not as one trim",
          both["head"]["level"] == "trims" and "2 trims" in both["head"]["label"]
          and only6["head"]["level"] == "trim",
          f"{both['head']['label']!r} at {both['head']['level']!r} level")
    check("one trim named in the list means the same as naming it singly",
          (only6["head"]["observations"], only6["head"]["median"])
          == (detail_payload(DB, "Ford", "Falcon", "XR6")["head"]["observations"],
              detail_payload(DB, "Ford", "Falcon", "XR6")["head"]["median"]),
          "variants=['XR6'] and variant='XR6' are one code path")
    check("an empty list means every trim, exactly as before",
          detail_payload(DB, "Ford", "Falcon", variants=[])["head"]["observations"]
          == all_trims["head"]["observations"],
          "no selection is still the whole model")
    check("the chips know which trims are in the pool",
          sorted(s["variant"] for s in both["siblings"] if s["selected"])
          == ["XR6", "XR8"],
          "both pooled trims are marked selected, G6E is not")
    check("a pooled median is computed over the pooled rows, not averaged",
          both["head"]["median"] == pd.Series(
              [20000, 18000, 15000, 15000, 17000, 17000, 41000]).median(),
          f"pooled median {both['head']['median']} equals pandas on the "
          f"same seven prices")

    print("\n== filters narrow a selection, and say that they did ==")
    yr = detail_payload(DB, "Ford", "Falcon", year_min=2013)
    check("a year floor keeps only the newer cars",
          yr["head"]["observations"] == 1
          and yr["filters"][0]["kind"] == "year",
          f"2013+ leaves the {yr['head']['observations']} XR8 sighting, "
          f"filter reported as {yr['filters'][0]['text']!r}")
    band = detail_payload(DB, "Ford", "Falcon", price_min=16000, price_max=21000)
    check("a price band keeps only the prices inside it",
          band["head"]["lo"] >= 16000 and band["head"]["hi"] <= 21000,
          f"${band['head']['lo']:,.0f}-${band['head']['hi']:,.0f} within the "
          f"$16,000-$21,000 asked for")
    kw = detail_payload(DB, "Ford", "Falcon", keyword="g6e")
    check("a keyword matches the trim regardless of case",
          kw["head"]["observations"] == 2
          and all(s["selected"] is False for s in kw["siblings"]),
          f"'g6e' found the {kw['head']['observations']} G6E sightings "
          f"without selecting a trim")
    check("filters report what they kept out of what they started with",
          yr["unfiltered"] == all_trims["head"]["observations"]
          and yr["kept"] == yr["head"]["observations"] < yr["unfiltered"],
          f"kept {yr['kept']} of {yr['unfiltered']}")
    none = detail_payload(DB, "Ford", "Falcon", keyword="nothing-like-this")
    check("a filter that matches nothing explains itself instead of erroring",
          none["ok"] and none["empty"] and "none of them match" in none["reason"],
          none["reason"])
    check("filters and pooled trims compose",
          detail_payload(DB, "Ford", "Falcon", variants=["XR6", "XR8"],
                         price_min=30000)["head"]["observations"] == 1,
          "XR6+XR8 above $30,000 is the single XR8")
    # VACUOUS-GUARD: a filter that silently dropped every unpriced or
    # yearless row would pass the checks above while changing the vehicle
    # count for an unrelated reason.
    noyear = detail_payload(DB, "Ford", "Falcon", price_max=99999999)
    check("a price filter does not quietly discard rows that have no price",
          noyear["head"]["observations"] == all_trims["head"]["observations"],
          "an absurd ceiling keeps every row, blanks included")

    print("\n== the three selection levels pool exactly what they should ==")
    mk = detail_payload(DB, "Ford")
    line = detail_payload(DB, "Ford", "Falcon")
    xr6 = detail_payload(DB, "Ford", "Falcon", "XR6")
    xr8 = detail_payload(DB, "Ford", "Falcon", "XR8")
    check("make and model line pool every trim (6 XR6 + 1 XR8 + 2 G6E = 9)",
          mk["head"]["observations"] == 9 and line["head"]["observations"] == 9,
          f"make={mk['head']['observations']}, line={line['head']['observations']}")
    check("trim narrows to exactly that trim, and levels are labelled",
          xr6["head"]["observations"] == 6 and xr8["head"]["observations"] == 1
          and line["head"]["level"] == "line" and xr6["head"]["level"] == "trim",
          f"XR6={xr6['head']['observations']} obs / {xr6['head']['vehicles']} cars, "
          f"XR8={xr8['head']['observations']}")
    check("a trim absent on day A shows only the day it was captured",
          xr8["head"]["days"] == 1 and xr8["byday"][0]["d"] == DAY_B,
          f"XR8 days={xr8['head']['days']} ({xr8['byday'][0]['d']})")

    print("\n== numbers match the shared engine, not a private copy ==")
    with Store(DB) as store:
        df = store.frame()
    own = df[(df["variant"] == "XR6")]["price_aud"]
    check("median/IQR equal an independent computation over the same rows",
          xr6["head"]["median"] == float(own.median())
          and xr6["head"]["lo"] == float(own.min())
          and xr6["head"]["hi"] == float(own.max()),
          f"page median {xr6['head']['median']} vs pandas {float(own.median())}")
    prepped = analyze.prep(df[df["variant"] == "XR6"], freq="MS")
    stats = analyze.bucket_stats(prepped)
    check("monthly buckets equal analyze.bucket_stats (same call as vpa report)",
          [b["median"] for b in xr6["bymonth"]] == [float(v) for v in stats["median"]],
          f"page={[b['median'] for b in xr6['bymonth']]} engine={list(stats['median'])}")
    dayA = next(b for b in xr6["byday"] if b["d"] == DAY_A)
    check("per-capture-day supply counts distinct vehicles",
          dayA["vehicles"] == 3 and dayA["median"] == 17000.0,
          f"{DAY_A}: {dayA['vehicles']} for sale, median {dayA['median']}")

    print("\n== the panels brought over from the workbook ==")
    sup = xr6["supply"]
    days_a = next(b for b in xr6["byday"] if b["d"] == DAY_A)
    days_b = next(b for b in xr6["byday"] if b["d"] == DAY_B)
    check("supply summary equals the per-day series it summarises",
          sup["captures"] == 2 and sup["now"] == days_b["vehicles"]
          and sup["most"] == max(days_a["vehicles"], days_b["vehicles"])
          and sup["fewest"] == min(days_a["vehicles"], days_b["vehicles"])
          and sup["change"] == days_b["vehicles"] - days_a["vehicles"],
          f"now={sup['now']}, most={sup['most']}, fewest={sup['fewest']}, "
          f"change={sup['change']} over {sup['captures']} captures")
    line_sup = line["supply"]
    check("one capture day reports no change rather than a fake zero",
          xr8["supply"]["change"] is None and xr8["supply"]["captures"] == 1
          and line_sup["change"] is not None,
          f"XR8 change={xr8['supply']['change']} (1 capture), "
          f"model line change={line_sup['change']}")

    sell = {r["name"]: r for r in xr6["splits"]["seller"]}
    check("seller split carries a median, not just a count",
          sell["dealer"]["n"] == 6 and sell["dealer"]["median"]
          == float(df[df["variant"] == "XR6"]["price_aud"].median()),
          f"dealer n={sell['dealer']['n']} median={sell['dealer']['median']}")
    check("state, source and year splits are present and add up",
          all(sum(r["n"] for r in xr6["splits"][k]) == xr6["head"]["observations"]
              for k in ("seller", "state", "source", "year")),
          "each split sums to the selection's 6 observations")
    check("a missing field becomes (unknown) rather than vanishing",
          any(r["name"] == "(unknown)" for r in xr8["splits"]["seller"]),
          f"XR8 seller split = "
          f"{[(r['name'], r['n']) for r in xr8['splits']['seller']]}")

    # The distribution is on ONE PRICE PER CAR - the same pool the verdict
    # card ranks against - not every sighting. A car seen on four capture
    # days must appear once in the tail, not four times.
    # the model line, not the trim: XR6 pools to 3 cars, below the histogram's
    # own 4-car floor - which is itself asserted two checks down
    pool = analyze.latest_per_vehicle(analyze.prep(df, freq="MS"))
    hh = line["hist"]
    check("the histogram is one price per car, not one per sighting",
          sum(b["n"] for b in hh["bins"]) == hh["n"] == int(len(pool))
          and hh["n"] < line["head"]["observations"],
          f"{hh['n']} cars binned, against "
          f"{line['head']['observations']} sightings")
    check("bins are contiguous and cover that pool's full range",
          hh["bins"][0]["lo"] == float(pool["price_aud"].min())
          and hh["bins"][-1]["hi"] == float(pool["price_aud"].max())
          and all(hh["bins"][i]["hi"] == hh["bins"][i + 1]["lo"]
                  for i in range(len(hh["bins"]) - 1)),
          f"{hh['bins'][0]['lo']} to {hh['bins'][-1]['hi']}, no gaps")
    check("a selection with too few cars gets no histogram, not a broken one",
          xr8["hist"] is None and xr6["hist"] is None,
          f"XR8 ({xr8['head']['vehicles']} car) and XR6 "
          f"({xr6['head']['vehicles']} cars) both below the 4-car floor")
    # VACUOUS-GUARD: `usable is False` would pass on an empty dict, so the
    # reason string and the count are asserted too
    check("a thin selection is refused a bell curve, with a stated reason",
          line["curve"]["usable"] is False
          and str(analyze.CURVE_MIN_N) in line["curve"]["reason"]
          and line["curve"]["n"] == len(pool),
          f"{line['curve']['n']} cars -> {line['curve']['reason']}")
    check("the live day is still reported when the curve is refused",
          line["standouts"] == []
          and line["distribution"]["live_day"] == DAY_B
          and line["distribution"]["pool_n"] == len(pool),
          f"live day {line['distribution']['live_day']}, "
          f"{line['distribution']['live_n']} of "
          f"{line['distribution']['pool_n']} cars in it")
    check("live counts each selection's OWN latest capture, not the database's",
          xr8["distribution"]["live_day"] == DAY_B
          and mk["distribution"]["live_day"] == DAY_B,
          f"XR8 (day B only) -> {xr8['distribution']['live_day']}; "
          f"make level -> {mk['distribution']['live_day']}")

    check("the scatter carries every priced row that has an odometer",
          xr6["scatter_total"] == int(len(
              df[(df["variant"] == "XR6") & df["odometer_km"].notna()]))
          and len(xr6["scatter"]) == xr6["scatter_total"]
          and all(p["odo"] and p["price"] for p in xr6["scatter"]),
          f"{len(xr6['scatter'])} points, all with a price and an odometer")

    sib = {s["variant"]: s for s in line["siblings"]}
    check("the trim table reports each trim's OWN latest capture, not the "
          "selection's",
          sib["XR6"]["last"] == DAY_B and sib["XR8"]["last"] == DAY_B
          and sib["XR6"]["now"] == days_b["vehicles"],
          f"XR6 last={sib['XR6']['last']} with {sib['XR6']['now']} for sale, "
          f"XR8 last={sib['XR8']['last']}")
    check("the trim table's range matches an independent min/max",
          sib["XR6"]["lo"] == float(df[df["variant"] == "XR6"]["price_aud"].min())
          and sib["XR6"]["hi"] == float(df[df["variant"] == "XR6"]["price_aud"].max()),
          f"XR6 {sib['XR6']['lo']}-{sib['XR6']['hi']}")

    print("\n== a real re-price is reported; a merged identity is not ==")
    movers = xr6["movers"]
    check("the car that dropped 20,000 -> 18,000 is reported as a mover",
          len(movers) == 1 and movers[0]["first_price"] == 20000
          and movers[0]["price"] == 18000 and movers[0]["change"] == -10.0,
          f"{len(movers)} mover(s): {[(m['first_price'], m['price'], m['change']) for m in movers]}")
    g6e = detail_payload(DB, "Ford", "Falcon", "G6E")
    clash = [v for v in g6e["vehicles"] if v["clash"]]
    check("two cars merged onto one key are detected and counted",
          g6e["head"]["collisions"] == 1 and len(clash) == 1
          and clash[0]["sightings"] == 2,
          f"collisions={g6e['head']['collisions']}, "
          f"the merged key holds {clash[0]['sightings'] if clash else 0} prices")
    check("the collision is NOT published as a price movement",
          g6e["movers"] == [] and g6e["mover_total"] == 0
          and clash and clash[0]["moved"] is False,
          f"movers={len(g6e['movers'])} (9,000 vs 12,000 on one day is two cars)")
    check("analyze.collided_keys agrees on which key is bad",
          len(analyze.collided_keys(df)) == 1,
          f"{len(analyze.collided_keys(df))} colliding key(s) in the whole DB")

    print("\n== exploring writes nothing, and new data is noticed ==")
    before = status_payload(DB)
    for sel in [(), ("Ford",), ("Ford", "Falcon"), ("Ford", "Falcon", "XR6")]:
        detail_payload(DB, *sel)
    picker_payload(DB)
    after = status_payload(DB)
    check("read-only: exploring changed no counts and no version",
          (before["vehicles"], before["observations"], before["data_version"])
          == (after["vehicles"], after["observations"], after["data_version"]),
          f"version {after['data_version']} unchanged across 5 reads")

    extra = make_record({"listing_id": "XR6-NEW", "make": "Ford", "model": "Falcon",
                         "variant": "XR6", "year": 2013, "price": 16500,
                         "odometer": 140000, "state": "NSW",
                         "capture_date": DAY_B}, source="examplecars")
    with Store(DB) as store:
        store.ingest([extra], source_file="(later-import)", adapter="test")
    later = status_payload(DB)
    check("data_version changes when an import lands (open pages can refresh)",
          later["data_version"] != after["data_version"]
          and later["observations"] == after["observations"] + 1,
          f"{after['data_version']} -> {later['data_version']}")
    check("the new car appears in the very next read, with no cache to clear",
          detail_payload(DB, "Ford", "Falcon", "XR6")["head"]["observations"] == 7,
          f"XR6 now {detail_payload(DB, 'Ford', 'Falcon', 'XR6')['head']['observations']} obs")
    check("status reports workbook age so the one non-live view can be spotted",
          isinstance(later.get("workbook"), dict)
          and "stale" in later["workbook"] and "built" in later["workbook"],
          f"workbook={later['workbook']}")

    print("\n== the pages: latest click wins, and they show they are working ==")
    from vpa import compare, explore
    from vpa.ui import CHROME_JS, CHROME_CSS
    check("the shared chrome has a busy bar and a latest-wins fetch",
          "function vpaLatest" in CHROME_JS and "function vpaBusy" in CHROME_JS
          and "#vpaload" in CHROME_CSS and "AbortController" in CHROME_JS,
          "vpaBusy, vpaLatest, #vpaload")
    check("Explore loads a selection and a car through it",
          'vpaLatest("detail"' in explore.PAGE and 'vpaLatest("car"' in explore.PAGE
          and 'fetch("/api/explore/detail' not in explore.PAGE,
          "no bare fetch left for either")
    check("Explore says what it is loading",
          "loading ${selLabel()}" in explore.PAGE, "selection line reads 'loading …'")
    check("Compare loads through it too",
          'vpaLatest("compare"' in compare.PAGE
          and 'fetch("/api/compare?' not in compare.PAGE, "no bare fetch left")
    check("the controls stay usable while results are faded",
          explore.PAGE.count('class="card keep"') >= 2
          and 'class="card keep"' in compare.PAGE,
          "picker cards are exempt from the fade")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_explore: seeding throwaway DB ==")
    seed()
    with Store(DB) as store:
        s = store.status()
    print(f"  {s['vehicles']} vehicles, {s['observations']} observations\n")
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
