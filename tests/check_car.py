"""Verification for judging ONE car against its trim (explore.car_payload).

    python -m tests.check_car      (from vehicle-price-analysis/)

The fixture is built to fire every gate, because on the real data the gates
fire constantly - the median trim holds four cars. It asserts:

  * the peer pool is one price per PHYSICAL CAR and excludes the subject, so
    a car seen on four capture days cannot vote four times and no car can
    grade itself;
  * the gates: no rank below 8 peers, a literal count below 20, a percentile
    above it - all counted in distinct vehicles, never rows;
  * the subject cannot bend the fit that judges it, bit-for-bit;
  * the model line's mileage fit is NEVER substituted when a trim's refuses;
  * the extrapolation guard withholds the adjusted figure outside the range
    the fit actually covers;
  * a typed car is judged by the identical code and never joins the pool;
  * comparables are ranked by log ratio, and duplicate identities are flagged
    rather than merged;
  * like-for-like splits refuse below their gate instead of printing a number;
  * a merged identity reports no price change;
  * nothing is written.

Several assertions here would pass vacuously on an empty payload; each one is
paired with a positive assertion that makes that impossible. They are marked
VACUOUS-GUARD in the source.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pandas as pd

from vpa import analyze
from vpa.explore import (CAR_COMPARABLES, CAR_MIN_PEERS, CAR_PERCENTILE_MIN,
                         _frame, _select, car_payload)
from vpa.schema import Record, make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "car_test.db"

RESULTS: list[tuple[bool, str]] = []

DAYS = ["2026-01-10", "2026-02-14", "2026-03-14", "2026-04-11"]


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def seed() -> None:
    """Known content, each trim built to exercise one gate.

    WIDE   24 cars, odometers 60k-290k, prices falling with km so the fit is
           usable. Car #0 is seen on all four days at one price; car #1
           re-prices. One duplicate PAIR (two identities, same price and
           odometer). One car with no odometer.
    MID    11 cars -> rank as a count, no percentile.
    THIN    5 cars -> no rank at all.
    FLAT   22 cars whose odometers are all within 4,000 km, so the trim's own
           fit refuses on spread while the MODEL LINE's fit is usable - which
           is what makes the never-substitute check non-vacuous.
    MERGED  2 different cars that collapse onto one identity under the legacy
           5,000 km rule.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []

    def add(**kw):
        raw.append({"make": "Ford", "model": "Falcon", **kw})

    # --- WIDE: 24 cars, price falls with odometer
    for i in range(24):
        odo = 60000 + i * 10000
        price = 40000 - i * 900
        days = DAYS if i == 0 else ([DAYS[0], DAYS[3]] if i == 1 else [DAYS[3]])
        for d in days:
            p = price if i != 1 else (price if d == DAYS[0] else price - 3000)
            add(listing_id=f"W-{i}", variant="Wide", year=2013,
                price=p, odometer=odo, state="NSW",
                seller_type="dealer" if i % 2 else "private", capture_date=d)
    # a duplicate PAIR: same price and odometer, two identities, different days
    add(listing_id="W-DUP", variant="Wide", year=2013, price=31000,
        odometer=100000, state="NSW", seller_type="dealer", capture_date=DAYS[3])
    add(variant="Wide", year=2013, price=31000, odometer=100000,
        state="NSW", seller_type="dealer", capture_date=DAYS[2])
    # a car with no odometer at all
    add(listing_id="W-NOODO", variant="Wide", year=2013, price=25000,
        state="VIC", seller_type="private", capture_date=DAYS[3])
    # One low-km car OFF the 10,000 km grid. Without it every odometer is
    # symmetric about any grid point, so ranking by |odo - subject| and by
    # |ln(odo / subject)| give the same answer and the log-ratio test passes
    # without testing anything.
    add(listing_id="W-LOW", variant="Wide", year=2013, price=42000,
        odometer=45000, state="NSW", seller_type="dealer", capture_date=DAYS[3])

    # --- MID: 11 cars -> count, not percentile
    for i in range(11):
        add(listing_id=f"M-{i}", variant="Mid", year=2012,
            price=20000 + i * 700, odometer=150000 + i * 4000,
            state="QLD", seller_type="dealer", capture_date=DAYS[3])

    # --- THIN: 5 cars -> no rank
    for i in range(5):
        add(listing_id=f"T-{i}", variant="Thin", year=2011,
            price=9000 + i * 500, odometer=200000 + i * 3000,
            state="SA", seller_type="private", capture_date=DAYS[3])

    # --- FLAT: odometers within 4,000 km -> trim fit refuses on spread
    for i in range(22):
        add(listing_id=f"F-{i}", variant="Flat", year=2014,
            price=26000 + (i % 5) * 400, odometer=120000 + i * 180,
            state="WA", seller_type="dealer", capture_date=DAYS[3])

    # Ingested ONE CAPTURE DAY PER BATCH, the way the app is actually used:
    # a sweep is pasted in, then another a week later. A single bulk ingest
    # would give every sighting the same batch_id and quietly make the
    # provenance checks below vacuous - they exist to prove that three
    # capture days really came from three separate imports.
    recs = [r for r in (make_record(d, source="carsales") for d in raw) if r]
    with Store(DB) as store:
        for day in sorted({r.capture_date for r in recs}):
            store.ingest([r for r in recs if r.capture_date == day],
                         source_file=f"(car-test {day})", adapter="test")

    # --- MERGED: seeded through the OLD 5,000 km rule so two different cars
    # collapse onto one identity, exactly as a legacy database holds them
    legacy, Record.ODO_BUCKET_KM = Record.ODO_BUCKET_KM, 5000
    try:
        merged = [make_record({"make": "Ford", "model": "Falcon",
                               "variant": "Wide", "year": 2013, "price": pr,
                               "odometer": odo, "state": "TAS",
                               "capture_date": DAYS[3]}, source="carsales")
                  for odo, pr in ((310100, 12000), (312900, 15000))]
        with Store(DB) as store:
            store.ingest([m for m in merged if m], source_file="(car-test)",
                         adapter="test")
    finally:
        Record.ODO_BUCKET_KM = legacy


def wide_keys(df):
    pool = analyze.latest_per_vehicle(
        analyze.prep(_select(df, "Ford", "Falcon", "Wide"), freq="MS"))
    return pool


def run_checks() -> int:
    df = _frame(DB)
    W = ("Ford", "Falcon", "Wide")
    pool = wide_keys(df)
    sub_rows = _select(df, *W)

    print("== the peer pool is one price per car, and excludes the subject ==")
    target = pool[pool["key"] == "".join(
        pool.sort_values("price_aud")["key"].iloc[2:3])]
    third = pool.sort_values("price_aud")["key"].iloc[2]
    p = car_payload(DB, *W, key=third)
    n_cars = int(pool["key"].nunique())
    n_sightings = int(len(sub_rows))
    check("rank is over distinct cars, self-excluded, with a true denominator",
          p["raw"]["rank"] == 3 and p["pool"]["n"] == n_cars - 1,
          f"3rd cheapest of {p['pool']['n'] + 1}; pool holds "
          f"{p['pool']['n']} peers = {n_cars} cars - 1")
    # VACUOUS-GUARD: assert BOTH numbers, so a pass cannot come from the
    # sighting count and the car count happening to be equal
    check("a car seen on four capture days is counted once, not four times",
          p["pool"]["n"] == n_cars - 1 and n_sightings > n_cars,
          f"{n_sightings} sightings collapse to {n_cars} cars "
          f"({n_sightings - n_cars} repeat sightings dropped)")
    check("the subject is absent from its own comparables",
          all(c["key"] != third for c in p["comparables"])
          and len(p["comparables"]) == CAR_COMPARABLES,
          f"{len(p['comparables'])} comparables, none of them the subject")

    print("\n== the gates, counted in cars ==")
    thin = car_payload(DB, "Ford", "Falcon", "Thin", price=9500, odometer=205000)
    # VACUOUS-GUARD: `rank is None` would pass on an empty payload, so the
    # reason string and the still-served comparables are asserted too
    check(f"below {CAR_MIN_PEERS} peers there is no rank, but a stated reason",
          not thin["usable"] and "rank" not in thin["raw"]
          and str(CAR_MIN_PEERS) in thin["raw"]["reason"]
          and len(thin["raw"]["reason"]) > 20,
          f"5 cars -> {thin['raw']['reason'][:60]}...")
    check("...and the comparables are still served when the rank is refused",
          len(thin["comparables"]) > 0,
          f"{len(thin['comparables'])} comparable(s) despite no rank")
    mid = car_payload(DB, "Ford", "Falcon", "Mid", price=23000, odometer=165000)
    check(f"between {CAR_MIN_PEERS} and {CAR_PERCENTILE_MIN} peers: a count, "
          f"never a percentile",
          mid["raw"]["usable"] and mid["raw"]["as_count"]
          and "percentile" not in mid["raw"] and mid["raw"]["n"] == 11,
          f"{mid['raw']['n']} peers -> rank {mid['raw']['rank']}, "
          f"percentile absent")
    check(f"at {CAR_PERCENTILE_MIN}+ peers a percentile appears",
          p["raw"].get("percentile") is not None and not p["raw"]["as_count"],
          f"{p['raw']['n']} peers -> {p['raw']['percentile']}th percentile")

    print("\n== the subject cannot bend the fit that judges it ==")
    own_fit = analyze.fit_mileage(
        analyze.prep(sub_rows[sub_rows["key"] != third], freq="MS"))
    check("the fit reported is the fit computed WITHOUT the subject",
          p["fit"]["pct_per_10k_km"] == round(own_fit["pct_per_10k_km"], 2)
          and p["fit"]["n"] == own_fit["n"],
          f"{p['fit']['pct_per_10k_km']}% per 10,000 km, n={p['fit']['n']}")
    # A typed car is not in the data, so nothing is excluded for it - its fit
    # is over the WHOLE trim. Comparing it to the stored subject's fit (which
    # is over the trim minus that one car) would be comparing two different
    # row sets; the invariant that matters is that the typed price, however
    # absurd, contributes nothing.
    full_fit = analyze.fit_mileage(analyze.prep(sub_rows, freq="MS"))
    cheap = car_payload(DB, *W, price=1000, odometer=100000)
    dear = car_payload(DB, *W, price=999000, odometer=100000)
    check("a typed car's price never reaches the fit, however absurd",
          cheap["fit"]["pct_per_10k_km"] == dear["fit"]["pct_per_10k_km"]
          == round(full_fit["pct_per_10k_km"], 2)
          and cheap["fit"]["n"] == dear["fit"]["n"] == full_fit["n"],
          f"$1,000 and $999,000 subjects both report the untouched trim fit "
          f"{dear['fit']['pct_per_10k_km']}% over n={dear['fit']['n']}")
    check("a STORED subject is excluded from its own fit, so it fits one fewer",
          p["fit"]["n"] == full_fit["n"] - 1,
          f"stored subject fits n={p['fit']['n']}, whole trim n={full_fit['n']}")

    print("\n== the model line's fit is never substituted ==")
    flat = car_payload(DB, "Ford", "Falcon", "Flat", price=27000, odometer=122000)
    line_fit = analyze.fit_mileage(
        analyze.prep(_select(df, "Ford", "Falcon", None), freq="MS"))
    # VACUOUS-GUARD: without the second half this passes trivially whenever
    # there was no line fit available to substitute in the first place
    check("a trim whose own fit refuses gets NO adjustment, though the line "
          "fit is usable",
          not flat["fit"]["usable"] and flat["adjusted"] is None
          and line_fit["usable"] is True,
          f"trim: {flat['fit']['reason'][:44]}... | "
          f"line fit WAS available ({line_fit['pct_per_10k_km']:.2f}%/10k)")
    check("...and the refusal reason is the engine's own words",
          flat["fit"]["reason"] == analyze.fit_mileage(
              analyze.prep(_select(df, "Ford", "Falcon", "Flat"),
                           freq="MS"))["reason"],
          f"verbatim: {flat['fit']['reason'][:60]}")

    print("\n== where one car sat over time, and where each sighting came from ==")
    # W-0 is the fixture's four-day car at one unchanged price; W-1 re-prices.
    with Store(DB) as store:
        w0 = store.conn.execute(
            "SELECT key FROM vehicles WHERE listing_id='W-0'").fetchone()["key"]
        w1 = store.conn.execute(
            "SELECT key FROM vehicles WHERE listing_id='W-1'").fetchone()["key"]
    h0 = car_payload(DB, *W, key=w0)["history"]
    h1 = car_payload(DB, *W, key=w1)["history"]
    trim_days = sorted(sub_rows.dropna(subset=["price_aud"])["day"].unique())
    check("the chart spans the TRIM's capture days, not just this car's",
          [r["d"] for r in h0["byday"]] == [str(d) for d in trim_days],
          f"{len(h0['byday'])} day(s): {', '.join(r['d'] for r in h0['byday'])}")
    # VACUOUS-GUARD: asserted on W-1, which is seen on the first and last day
    # only. On W-0, which appears on every day, "gaps land in the right place"
    # is satisfied by a payload with no gaps in it at all.
    seen = {r["d"] for r in h1["rows"]}
    check("...so a day the trim was captured without this car reads as a gap",
          len(seen) < len(h1["byday"])
          and all((r["mine"] is not None) == (r["d"] in seen)
                  for r in h1["byday"]),
          f"seen on {len(seen)} of {len(h1['byday'])} day(s); the other "
          f"{len(h1['byday']) - len(seen)} carry mine=None and break the line")

    # the daily peer stats must exclude the subject, like every other panel
    day = h0["byday"][-1]
    same = sub_rows[(sub_rows["day"] == day["d"])
                    & (sub_rows["key"] != w0)].dropna(subset=["price_aud"])
    same = same.sort_values("capture_date").groupby("key").tail(1)
    check("the daily band is computed on the peers, with the subject removed",
          day["n"] == len(same)
          and abs(day["median"] - float(same["price_aud"].median())) < 0.01,
          f"{day['d']}: n={day['n']} median ${day['median']:,.0f} "
          f"(pandas on the same rows: {len(same)}, "
          f"${float(same['price_aud'].median()):,.0f})")
    cheaper = int((same["price_aud"].astype(float) < day["mine"]).sum())
    check("...and the daily rank counts only cars captured THAT day",
          day["rank"] == cheaper + 1 and day["of"] == len(same) + 1,
          f"rank {day['rank']} of {day['of']} on {day['d']}")

    # provenance: the whole point is that a capture day is checkable
    check("every sighting names the import that recorded it",
          len(h0["rows"]) == h0["sightings"]
          and all(r["imported_at"] and r["batch"] for r in h0["rows"])
          and h0["orphans"] == 0,
          f"{len(h0['rows'])} sighting(s), batches "
          f"{[r['batch'] for r in h0['rows']]}")
    with Store(DB) as store:
        real = {r["batch_id"] for r in store.sightings(w0)}
    check("...and the count of separate imports matches the ingest log",
          h0["imports"] == len(real) == len(h0["rows"]),
          f"{h0['imports']} import(s) for {len(h0['rows'])} sighting(s) - "
          f"separate captures, not one paste counted repeatedly")
    check("an unchanged ad is called unchanged, not a price history",
          h0["unchanged"] is True and h0["moved"] is False,
          f"same price and odometer on all {len(h0['rows'])} sightings")

    steps = [r for r in h1["rows"] if r.get("d_price")]
    check("a car that RE-PRICED shows the change and the day it happened",
          h1["unchanged"] is False and h1["moved"] is True and len(steps) == 1
          and steps[0]["d_price"] == -3000,
          f"{steps[0]['d']}: {steps[0]['d_price']:+,.0f} "
          f"({h1['rows'][0]['price']:,.0f} -> {h1['rows'][-1]['price']:,.0f})")
    one_day = car_payload(DB, *W, key=str(
        sub_rows[sub_rows["listing_id"] == "W-LOW"]["key"].iloc[0]))["history"]
    check("a car seen once gets a row and no chart, rather than a false line",
          len(one_day["rows"]) == 1 and one_day["imports"] == 1,
          "one sighting, one import - the front end draws no chart below 2 days")

    print("\n== the extrapolation guard ==")
    far = car_payload(DB, *W, price=20000, odometer=600000)
    used = analyze.prep(sub_rows, freq="MS").dropna(
        subset=["price_aud", "odometer_km"])
    want = [round(float(used["odometer_km"].quantile(0.05)), 2),
            round(float(used["odometer_km"].quantile(0.95)), 2)]
    check("outside the fitted range the adjusted figure is withheld, with the "
          "range stated",
          far["fit"].get("outside") is True and far["adjusted"] is None
          and far["fit"]["covers"] == want,
          f"600,000 km vs covered {far['fit']['covers']} (independently "
          f"computed {want})")
    inside = car_payload(DB, *W, price=30000, odometer=150000)
    check("...and inside the range it is produced",
          inside["adjusted"] is not None
          and inside["adjusted"]["rank"]["usable"],
          f"150,000 km -> adjusted {inside['adjusted']['price']}")

    print("\n== a typed car is judged identically, and never joins the pool ==")
    typed = car_payload(DB, *W, price="$31,000", odometer="100k", year="2013",
                        seller="Private Seller", state="New South Wales")
    s = typed["subject"]
    check("typed values are normalised by the importer's own functions",
          (s["price"], s["odometer_km"], s["year"], s["seller_type"],
           s["state"]) == (31000.0, 100000.0, 2013, "private", "NSW"),
          f"'$31,000' '100k' 'Private Seller' 'New South Wales' -> "
          f"{s['price']}, {s['odometer_km']}, {s['seller_type']}, {s['state']}")
    check("a typed car adds exactly one to the pool a stored car sees",
          typed["pool"]["n"] == p["pool"]["n"] + 1 and s["stored"] is False,
          f"stored subject: {p['pool']['n']} peers, typed: "
          f"{typed['pool']['n']}")
    check("a typed car has no key, so it cannot appear among its own peers",
          s["key"] is None
          and all(c["key"] for c in typed["comparables"]),
          "subject key is None; every comparable has a real key")

    print("\n== comparables: log-ratio order, duplicates flagged not merged ==")
    import math
    SUBJ_ODO = 70000
    low = car_payload(DB, *W, price=30000, odometer=SUBJ_ODO)
    odos = [c["odometer_km"] for c in low["comparables"]]
    # the whole candidate set, ranked independently both ways
    cand = sorted(float(o) for o in pool["odometer_km"].dropna() if o > 0)
    want_log = sorted(cand, key=lambda o: (abs(math.log(o / SUBJ_ODO)), o))[
        :CAR_COMPARABLES]
    want_abs = sorted(cand, key=lambda o: (abs(o - SUBJ_ODO), o))[:CAR_COMPARABLES]
    # VACUOUS-GUARD: `want_log != want_abs` is what proves the fixture can
    # tell the two rules apart; the length check stops an empty list passing
    check("ordered by log ratio, and the fixture can tell the two rules apart",
          odos == want_log and len(odos) == CAR_COMPARABLES
          and want_log != want_abs,
          f"got {[f'{o/1000:.0f}k' for o in odos]}; "
          f"by absolute distance it would be "
          f"{[f'{o/1000:.0f}k' for o in want_abs]}")
    dup = car_payload(DB, *W, price=31000, odometer=100000)
    flagged = [c for c in dup["comparables"] if c["dup"]]
    check("two identities holding one price and odometer are BOTH flagged",
          len(flagged) == 2 and dup["band"]["dup_in_pool"] >= 1,
          f"{len(flagged)} flagged, {dup['band']['dup_in_pool']} surplus "
          f"identity(ies) in the pool")
    check("...and neither was merged away - the pool still holds both",
          dup["pool"]["n"] == p["pool"]["n"] + 1,
          f"pool {dup['pool']['n']} (typed subject) vs {p['pool']['n']} "
          f"(stored subject) - nothing removed")

    print("\n== like-for-like refuses below its gate ==")
    vic = car_payload(DB, *W, price=25000, odometer=140000, state="VIC",
                      seller="private", year=2013)
    st = vic["splits"]["state"]
    # VACUOUS-GUARD: assert the suppressed side is PRESENT with a reason,
    # not merely missing from the dict
    check("a thin state split is present, refused, and says why",
          st["median"] is None and st["n"] < CAR_MIN_PEERS
          and len(st["reason"]) > 10 and st["value"] == "VIC",
          f"VIC n={st['n']} -> {st['reason']}")
    sl = vic["splits"]["seller"]
    check("a well-populated split carries a median equal to pandas'",
          sl["median"] == float(
              pool[pool["seller_type"] == "private"]["price_aud"].median()),
          f"private n={sl['n']} median {sl['median']}")

    print("\n== a merged identity reports no price change ==")
    merged = analyze.collided_keys(sub_rows)
    mk = next(iter(merged))
    mp = car_payload(DB, *W, key=mk)
    check("the collision is flagged and its history carries no percentage",
          mp["history"]["clash"] is True and mp["history"]["change"] is None
          and mp["history"]["moved"] is False,
          f"key {mk[:10]} holds {mp['history']['sightings']} prices on one day")

    print("\n== a car with no odometer says what it cannot answer ==")
    no_odo = pool[pool["odometer_km"].isna()]["key"].iloc[0]
    npay = car_payload(DB, *W, key=no_odo)
    check("no odometer: no adjustment, no km percentile, and both are named",
          npay["adjusted"] is None and npay["odo_percentile"] is None
          and len(npay["unavailable"]) >= 1
          and all(len(u) > 10 for u in npay["unavailable"]),
          f"unavailable: {npay['unavailable'][0][:70]}...")
    check("...and it still gets comparables, matched on year instead",
          npay["band"]["basis"] == "year" and len(npay["comparables"]) > 0,
          f"{len(npay['comparables'])} comparables matched on {npay['band']['basis']}")

    print("\n== the bell curve and the standouts it identifies ==")
    import numpy as np
    from vpa.explore import detail_payload
    prices = pool["price_aud"].dropna()
    c = analyze.price_curve(prices)
    ln = np.log(prices.to_numpy(dtype=float))
    check("fitted on LOG price: mu and sigma equal numpy on the same values",
          c["usable"] and abs(c["mu"] - float(ln.mean())) < 1e-12
          and abs(c["sigma"] - float(ln.std(ddof=1))) < 1e-12,
          f"mu={c['mu']:.6f} sigma={c['sigma']:.6f} over n={c['n']}")
    check("exp(mu) is the fitted median, and the mean sits above it (skew)",
          abs(c["median"] - float(np.exp(c["mu"]))) < 1e-9
          and c["mean"] > c["median"],
          f"median ${c['median']:,.0f} < mean ${c['mean']:,.0f}")
    check("the sigma marks are exp(mu + k*sigma), in dollars",
          all(abs(m["price"] - float(np.exp(c["mu"] + m["sd"] * c["sigma"]))) < 1e-6
              for m in c["marks"]) and len(c["marks"]) == 5,
          " ".join(f"{m['sd']:+d}s=${m['price']:,.0f}" for m in c["marks"]))
    # VACUOUS-GUARD: an all-zero tail table would satisfy a "counts exist"
    # check, so the actual counts are recomputed here independently
    vals = prices.to_numpy(dtype=float)
    ok_tails = all(
        m["beyond"] == int((vals < m["price"]).sum() if m["sd"] < 0
                           else (vals > m["price"]).sum())
        for m in c["marks"])
    check("each tail reports the REAL count beside the curve's prediction",
          ok_tails and sum(m["beyond"] for m in c["marks"]) > 0,
          " ".join(f"{m['sd']:+d}s {m['beyond']}v{m['expected']}"
                   for m in c["marks"] if m["sd"]))
    check("a curve is refused below its minimum, and when every price is equal",
          analyze.price_curve([1000] * (analyze.CURVE_MIN_N - 1))["usable"] is False
          and analyze.price_curve([1000] * 40)["usable"] is False
          and "same" in analyze.price_curve([1000] * 40)["reason"],
          f"{analyze.CURVE_MIN_N - 1} prices refused; 40 identical prices -> "
          f"{analyze.price_curve([1000] * 40)['reason']}")
    check("z is symmetric in log space: half and double are equal distances",
          abs(analyze.z_of(c["median"] / 2, c)
              + analyze.z_of(c["median"] * 2, c)) < 1e-9
          and analyze.z_of(c["median"], c) == 0,
          f"half -> {analyze.z_of(c['median'] / 2, c)}, "
          f"double -> {analyze.z_of(c['median'] * 2, c)}")

    dp = detail_payload(DB, *W)
    so = dp["standouts"]
    check("standouts cover the whole pool, sorted cheapest-first by z",
          len(so) == int(len(pool)) and so[0]["z"] <= so[-1]["z"]
          and all(so[i]["z"] <= so[i + 1]["z"] for i in range(len(so) - 1)),
          f"{len(so)} rows, z from {so[0]['z']} to {so[-1]['z']}")
    check("each z matches analyze.z_of on that car's own price",
          all(r["z"] == analyze.z_of(r["price"], dp["curve"]) for r in so),
          "every row recomputed and identical")
    # VACUOUS-GUARD: `any(live)` alone would pass if EVERY car were live, so
    # both sides are asserted against an independent live-key computation
    live, live_day = analyze.live_keys(sub_rows)
    flagged = {r["key"] for r in so if r["live"]}
    check("the live flag equals analyze.live_keys, and is not all-or-nothing",
          flagged == (live & {r["key"] for r in so})
          and 0 < len(flagged) < len(so),
          f"{len(flagged)} of {len(so)} cars seen in the {live_day} capture")
    check("live uses this selection's OWN latest capture day",
          live_day == dp["distribution"]["live_day"] == DAYS[3],
          f"{live_day} (the fixture's last capture)")
    check("the mileage-adjusted z separates 'cheap' from 'cheap for its km'",
          dp["adj_curve"] and dp["adj_curve"]["usable"]
          and any(r["z_adj"] is not None and abs(r["z_adj"] - r["z"]) > 0.3
                  for r in so),
          f"{sum(1 for r in so if r['z_adj'] is not None)} cars carry both "
          f"scores; at least one moves by more than 0.3 sigma")

    print("\n== finding one car from its saved ad page ==")
    from vpa.adapters.detailpage import is_detail_page, parse_html
    from vpa.explore import identify_payload

    def detail_page(ad, make="Ford", model="Falcon", name="2013 Ford Falcon Wide",
                    price=31000, odo=100000, exterior="Kinetic",
                    interior="Black", state="NSW", seller="Private",
                    year="2013", specs=True):
        """A page shaped like a real carsales details page: one JSON-LD block,
        one analytics metaData object, page-level tracking, and the ad's own
        specifications table.

        The colour arrangement is copied from the real file and is a TRAP on
        purpose: `metaData.colour` holds the INTERIOR colour, while the paint
        is stated only in the labelled spec row and in page-level tracking.
        Reading the obvious-looking analytics field reports a Kinetic Blue car
        as black, which is what the first version of the parser did.
        """
        spec = ('<div><span class="l">Exterior colour</span></div>'
                '<div class="v"><span class="t">%s</span></div>'
                '<div><span class="l">Interior colour</span></div>'
                '<div class="v"><span class="t">%s</span></div>'
                % (exterior, interior)) if specs else ""
        return ('<html><head><script type="application/ld+json">'
                '{"@type":["Product","Vehicle"],"name":"%s",'
                '"brand":{"@type":"Brand","name":"%s"},"model":"%s",'
                '"offers":{"@type":"Offer","price":%s,"priceCurrency":"AUD"},'
                '"@id":"https://www.carsales.com.au/cars/details/x/%s/"}'
                '</script></head><body>%s<script>var t={"metaData":{'
                '"pageCategory":"details","make":"%s","model":"%s",'
                '"badge":"Wide","state":"%s","networkId":"%s","colour":"%s",'
                '"sellertype":"%s","year":"%s","odometermin":"%s","price":"%s"'
                '}}</script>'
                '<script>var m=[{"type":"KeyValueMetaData",'
                '"key":"tracking/page/details/colour","value":"%s"}]</script>'
                # a real details page repeats the ad id in BOTH cases; a
                # case-sensitive count reads that as two vehicles and rejects
                # the page, which is exactly what happened
                '<script>var u={"networkId":"%s"}</script>'
                '</body></html>'
                % (name, make, model, price, ad, spec, make, model, state, ad,
                   interior, seller, year, odo, price, exterior.lower(),
                   ad.lower()))

    with Store(DB) as store:
        row = store.conn.execute(
            "SELECT key, listing_id FROM vehicles WHERE listing_id LIKE 'W-%' "
            "AND listing_id NOT IN ('W-DUP','W-NOODO','W-LOW') LIMIT 1").fetchone()
        ad, want_key = row["listing_id"], row["key"]
        obs = store.conn.execute(
            "SELECT price_aud, odometer_km FROM observations WHERE key=? LIMIT 1",
            (want_key,)).fetchone()

    raw = parse_html(detail_page(ad))
    check("a details page yields every field, colour included",
          raw and raw["listing_id"] == ad and raw["colour"] == "Kinetic"
          and raw["state"] == "NSW" and raw["odometer"] == "100000"
          and raw["variant"],
          f"{ {k: raw[k] for k in ('listing_id','colour','state','variant')} }")
    check("colour comes ONLY from here - it is in no other adapter or column",
          "colour" not in {c[1] for c in __import__("sqlite3").connect(DB)
                           .execute("PRAGMA table_info(vehicles)")}
          and raw.get("colour"),
          "vehicles has no colour column; the page has one")
    # REGRESSION (04/08/2026): the paint was read from metaData.colour, which
    # on the real page holds the INTERIOR colour. Both must be reported, and
    # the labelled EXTERIOR row must be the one called `colour`.
    check("the paint is the ad's exterior row, not the analytics field",
          raw["colour"] == "Kinetic" and raw["interior_colour"] == "Black",
          f"exterior {raw['colour']}, interior {raw['interior_colour']}, "
          f"while metaData said Black")
    two_tone = parse_html(detail_page(ad, exterior="Winter White",
                                      interior="Charcoal"))
    check("...and a multi-word paint name survives intact",
          two_tone["colour"] == "Winter White"
          and two_tone["interior_colour"] == "Charcoal",
          f"{two_tone['colour']} / {two_tone['interior_colour']}")
    # the fallback path: no spec table at all. Page-level tracking agreed with
    # the exterior row on the real file, so it may stand in - metaData may not.
    bare = parse_html(detail_page(ad, specs=False))
    check("with no spec table, page tracking supplies the paint, not metaData",
          bare["colour"] == "Kinetic" and "interior_colour" not in bare,
          f"colour={bare['colour']} (metaData said Black)")

    page = detail_page(ad, price=int(obs["price_aud"]),
                       odo=int(obs["odometer_km"]))
    got = identify_payload(DB, page)
    check("the site's own ad id finds the exact stored car",
          got["ok"] and got["in_data"] and got["key"] == want_key
          and "ad id" in got["basis"],
          f"{ad} -> {got['key'][:12]} via {got['basis']}")
    check("...and it reports what the page adds that the captures never had",
          isinstance(got.get("adds"), list) and got["stored"]["sightings"] >= 1,
          f"adds={got['adds']}, {got['stored']['sightings']} sighting(s)")

    # no ad id: the evidence rule from merge.py, on a precise odometer
    with Store(DB) as store:
        prec = store.conn.execute(
            "SELECT o.key, o.price_aud, o.odometer_km, v.variant "
            "FROM observations o JOIN vehicles v ON v.key=o.key "
            "WHERE v.variant='Wide' AND CAST(o.odometer_km AS INT) % 1000 <> 0 "
            "LIMIT 1").fetchone()
    if prec:
        anon = detail_page("SSE-AD-NOTSTORED", price=int(prec["price_aud"]),
                           odo=int(prec["odometer_km"]))
        got2 = identify_payload(DB, anon)
        check("an unknown ad id still matches on exact price + exact odometer",
              got2["ok"] and got2["in_data"] and got2["key"] == prec["key"],
              f"matched via {got2.get('basis')}")
    # a ROUND odometer must NOT be enough on its own
    roundish = detail_page("SSE-AD-ALSONOTSTORED", price=31000, odo=100000)
    got3 = identify_payload(DB, roundish)
    check("a round odometer alone is NOT accepted as an identity match",
          got3["ok"] and got3["in_data"] is False
          and got3["trim_known"] is True,
          f"in_data={got3['in_data']}, trim_known={got3['trim_known']}")

    # VACUOUS-GUARD: an error dict would satisfy "not in_data", so ok is
    # asserted True and the trim/line flags are required to be present
    unknown = detail_page("SSE-AD-9999", make="Mazda", model="MX-5",
                          name="2016 Mazda MX-5 Roadster", price=29000,
                          odo=54321)
    got4 = identify_payload(DB, unknown)
    check("a car from a make you have never captured is reported, not an error",
          got4["ok"] and got4["in_data"] is False
          and got4["trim_known"] is False and got4["line_known"] is False,
          "Mazda MX-5: not in data, trim and line both unknown")

    multi = ('<html><body><script>var a={"networkId":"SSE-AD-1"},'
             'b={"networkId":"SSE-AD-2"}</script></body></html>')
    check("a search-results page is refused with guidance, not mis-parsed",
          is_detail_page(multi) is False
          and identify_payload(DB, multi)["ok"] is False
          and "several" in identify_payload(DB, multi)["error"],
          identify_payload(DB, multi)["error"][:70])
    check("junk and empty input are refused cleanly",
          identify_payload(DB, "<html>hi</html>")["ok"] is False
          and identify_payload(DB, "")["ok"] is False,
          "both return ok:False with a message")

    print("\n== finding one car from its advert LINK (nothing fetched) ==")
    from vpa.explore import link_payload
    full = f"https://www.carsales.com.au/cars/details/2013-ford-falcon-wide/{ad}/?Cr=0&gclid=abc"
    lk = link_payload(DB, full)
    check("a full advert link finds the stored car by the ad id inside it",
          lk["ok"] and lk["in_data"] and lk["key"] == want_key
          and "ad id" in lk["basis"] and lk["stored"]["sightings"] >= 1,
          f"{ad} -> {lk.get('key', '')[:12]} via {lk.get('basis')}")
    check("...and it reads the car's own details from your data, not the web",
          lk["found"]["make"] == "Ford" and lk["found"]["model"] == "Falcon"
          and lk["found"]["price"] is not None,
          f"{lk['found']['make']} {lk['found']['model']} {lk['found']['variant']} "
          f"${lk['found']['price']:,.0f}")
    bare = link_payload(DB, ad.lower())
    nohost = link_payload(DB, f"carsales.com.au/cars/details/x/{ad}")
    check("a bare ad id, any case, or a link without https:// works the same",
          bare["key"] == nohost["key"] == want_key, f"{ad.lower()} and a host-less link")
    gone = link_payload(DB, "https://www.carsales.com.au/cars/details/x/SSE-AD-123456789/")
    check("an advert you never captured is reported as such, not guessed at",
          gone["ok"] and gone["in_data"] is False
          and gone["found"]["listing_id"] == "SSE-AD-123456789",
          "a link has no price or km, so there is no weaker rule to fall back on")
    fb = link_payload(DB, "https://www.facebook.com/marketplace/item/12345/")
    junk = [link_payload(DB, t) for t in ("", "two words", "https://example.com/x")]
    check("Facebook, junk and empty links are refused with a reason",
          fb["ok"] is False and "Facebook" in fb["error"]
          and all(j["ok"] is False and j["error"] for j in junk),
          fb["error"][:60])
    import vpa.explore as _ex
    src = Path(_ex.__file__).read_text(encoding="utf-8")
    body = src[src.index("def link_payload"):src.index("# ----", src.index("def link_payload"))]
    check("the link lookup has no way to reach the network",
          not any(w in body for w in ("urlopen", "requests", "http.client", "socket")),
          "link_payload only reads the text and the database")

    before_id = hashlib.sha256(DB.read_bytes()).hexdigest()
    for pg in (page, roundish, unknown, multi, ""):
        identify_payload(DB, pg)
    for t in (full, ad, "https://www.carsales.com.au/cars/details/x/SSE-AD-1/", ""):
        link_payload(DB, t)
    check("identifying a car imports nothing - the database is untouched",
          hashlib.sha256(DB.read_bytes()).hexdigest() == before_id,
          "5 saved-page and 4 link lookups, file byte-identical")

    print("\n== refusals and read-only ==")
    broad = car_payload(DB, "Ford", "Falcon", None, price=20000)
    check("the card refuses above trim level rather than pooling trims",
          not broad["usable"] and broad["level"] == "too broad",
          broad["reason"][:70])
    bad = car_payload(DB, *W, key="does-not-exist")
    check("an unknown key is an error, not a crash or a silent empty card",
          bad["ok"] is False and len(bad["error"]) > 5,
          f"{bad}")
    noprice = car_payload(DB, *W, odometer=100000)
    # no price is a question too - "what should it ask?" - so no rank, but a
    # range centred exactly on the card's own "expected for its kilometres"
    withp = car_payload(DB, *W, odometer=100000, price=30000)
    e = noprice.get("estimate") or {}
    check("a typed car with no price gets an expected range, and no rank",
          noprice["ok"] is True and noprice["raw"]["usable"] is False
          and noprice["expected"] is None and e.get("usable")
          and e["wide_lo"] <= e["lo"] <= e["mid"] <= e["hi"] <= e["wide_hi"]
          and withp["expected"] and e["mid"] == withp["expected"]["price"]
          and withp["estimate"] == e,
          f"{e.get('basis')}: {e.get('lo')}-{e.get('hi')}, typical {e.get('mid')}")
    est_far = car_payload(DB, *W, odometer=600000)["estimate"]
    est_none = car_payload(DB, *W)["estimate"]
    stored = car_payload(DB, *W, key=car_payload(DB, *W, odometer=100000)["comparables"][0]["key"])
    check("off the curve it falls back to the nearest cars, and with no km to the trim",
          est_far["basis"] == "nearest" and "outside" in (est_far["why_not_curve"] or "")
          and est_none["basis"] == "trim" and stored["estimate"] is None,
          f"{est_far['basis']}, {est_none['basis']}; a stored car gets none")

    before = hashlib.sha256(DB.read_bytes()).hexdigest()
    assert DB.stat().st_size > 0
    for args in [{"key": third}, {"price": 20000, "odometer": 90000},
                 {"price": 1}, {"key": no_odo}, {"key": mk}]:
        car_payload(DB, *W, **args)
    car_payload(DB, "Ford", "Falcon", "Thin", price=9000)
    after = hashlib.sha256(DB.read_bytes()).hexdigest()
    check("judging cars wrote nothing - the database is byte-identical",
          before == after and DB.stat().st_size > 0,
          f"sha256 {before[:16]}... unchanged over 6 calls "
          f"({DB.stat().st_size:,} bytes)")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


def main() -> int:
    print("== check_car: seeding throwaway DB ==")
    seed()
    with Store(DB) as store:
        s = store.status()
    print(f"  {s['vehicles']} vehicles, {s['observations']} observations\n")
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
