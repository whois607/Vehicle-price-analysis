"""End-to-end selfcheck against synthetic data with known ground truth.

    python -m tests.selfcheck      (from the vehicle-price-analysis directory)

Regenerates tests/synthetic/selfcheck/, ingests every format through the real
pipeline into a throwaway database, then asserts parsing, normalisation,
dedupe, price history, aggregation, mileage adjustment, forecasting and the
too-little-data refusal — each check printed with the actual numbers.
Exit code 0 only if every check passes. ASCII output only.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd

from vpa import analyze, forecast
from vpa.adapters import load_file, v2db  # noqa: F401 (v2db import proves module loads)
from vpa.cli import main as cli_main
from vpa.segments import add_segment_cols, filter_segment
from vpa.store import Store

from . import make_synthetic

ROOT = Path(__file__).resolve().parent
SYN = ROOT / "synthetic" / "selfcheck"   # only its own folder - other suites run beside it
DB = SYN / "test.db"
OUTDIR = SYN / "out"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main() -> int:
    print("== selfcheck: regenerating synthetic data ==")
    if SYN.exists():
        shutil.rmtree(SYN)
    truth = make_synthetic.main()
    flags = truth["capture_date_flags"]

    # ---- 1. adapter parsing --------------------------------------------------
    print("\n== adapters ==")
    csv_recs, _ = load_file(SYN / "falcons_1.csv", source="examplecars")
    check("CSV parse count", len(csv_recs) == truth["csv_rows"],
          f"parsed {len(csv_recs)}, expected {truth['csv_rows']}")
    json_recs, _ = load_file(SYN / "falcons_2.json")
    want = truth["json_falcon_rows"] + truth["n_bikes"]
    check("JSON parse count", len(json_recs) == want,
          f"parsed {len(json_recs)}, expected {want}")
    check("JSON per-row source honoured",
          {r.source for r in json_recs} == {"examplecars", "examplebikes"},
          f"sources: {sorted({r.source for r in json_recs})}")
    text_recs, _ = load_file(SYN / "falcons_3.txt", source="exampleads",
                             capture_date=flags["falcons_3.txt"])
    check("pasted-text parse count", len(text_recs) == truth["text_rows"],
          f"parsed {len(text_recs)}, expected {truth['text_rows']}")
    html_recs, _ = load_file(SYN / "falcons_4.html",
                             capture_date=flags["falcons_4.html"])
    check("HTML JSON-LD parse count", len(html_recs) == truth["html_ld_rows"],
          f"parsed {len(html_recs)}, expected {truth['html_ld_rows']}")
    check("HTML source auto-detected", all(r.source == "examplecars" for r in html_recs),
          f"sources: {sorted({r.source for r in html_recs})}")
    n_seller = sum(1 for r in html_recs if r.seller_type)
    n_state = sum(1 for r in html_recs if r.state)
    n_variant = sum(1 for r in html_recs if r.variant)
    check("HTML tracking supplement fills seller/state; variant from title",
          n_seller >= 8 and n_state >= 6 and n_variant == len(html_recs),
          f"seller_type on {n_seller}, state on {n_state}, variant on "
          f"{n_variant} of {len(html_recs)} records")
    relist_recs, _ = load_file(SYN / "falcons_5_relist.txt", source="exampleads",
                               capture_date=flags["falcons_5_relist.txt"])
    check("re-list paste parse count", len(relist_recs) == 1,
          f"parsed {len(relist_recs)}, expected 1")
    neg = [r for r in csv_recs + json_recs + text_recs
           if r.price_aud is None and (r.price_note or "")]
    check("'Negotiable'/'POA' become no-price + note", len(neg) >= 9,
          f"{len(neg)} unpriced-with-note records (expected >= 9)")

    # ---- 2. store: append, dedupe, history ----------------------------------
    print("\n== store ==")
    if DB.exists():
        DB.unlink()
    store = Store(DB)
    for recs, label in [(csv_recs, "csv"), (json_recs, "json"), (text_recs, "text"),
                        (html_recs, "html"), (relist_recs, "relist")]:
        store.ingest(recs, source_file=label, adapter=label)
    st = store.status()
    check("vehicle dedupe across sources",
          st["vehicles"] == truth["expected_vehicles"],
          f"{st['vehicles']} vehicles, expected {truth['expected_vehicles']} "
          f"(re-list merged by fingerprint)")
    check("observation count (history kept)",
          st["observations"] == truth["expected_observations"],
          f"{st['observations']} observations, expected {truth['expected_observations']}")
    dup = store.ingest(html_recs, source_file="html-again", adapter="html")
    check("re-ingest is idempotent",
          dup["vehicles_new"] == 0 and dup["obs_new"] == 0
          and dup["obs_duplicate"] == len(html_recs),
          f"re-ingesting HTML: new={dup['obs_new']}, dupes={dup['obs_duplicate']}")
    changes = store.price_changes()
    keys_changed = changes["key"].nunique() if not changes.empty else 0
    got_prices = (sorted(changes["price_aud"].unique().tolist())
                  if not changes.empty else [])
    want_prices = sorted([truth["relist"]["price_old"], truth["relist"]["price_new"]])
    check("price change tracked for re-list",
          keys_changed == 1 and got_prices == want_prices,
          f"{keys_changed} vehicle(s) changed price, prices {got_prices} "
          f"(expected {want_prices})")

    # ---- 3. normalisation ----------------------------------------------------
    print("\n== normalisation ==")
    df = add_segment_cols(store.frame())
    falc = df[df["model"] == "Falcon"]
    check("make casing unified", set(falc["make"].unique()) == {"Ford"},
          f"makes: {sorted(falc['make'].unique())}")
    check("bike model casing unified",
          set(df[df["vehicle_type"] == "bike"]["model"].unique()) == {"YZ250F"},
          f"bike models: {sorted(df[df['vehicle_type'] == 'bike']['model'].unique())}")
    states = set(df["state"].dropna().unique())
    check("states normalised", states <= {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"},
          f"states: {sorted(states)}")
    check("AU DD/MM/YYYY dates parsed",
          df["capture_date"].min() == pd.Timestamp("2025-09-02")
          or df["capture_date"].min().month == 9,
          f"earliest capture: {df['capture_date'].min().date()}")

    # ---- 4. aggregation vs ground truth --------------------------------------
    print("\n== aggregation ==")
    seg = falc[falc["segment"] == "Ford Falcon 2011-2013"]
    prepped = analyze.prep(seg)
    stats = analyze.bucket_stats(prepped)
    med_ok, n_ok, details = True, True, []
    for _, row in stats.iterrows():
        m = pd.Timestamp(row["bucket"]).strftime("%Y-%m")
        want_med = truth["median_per_month_stored"][m]
        want_n = truth["priced_per_month_stored"][m]
        if abs(row["median"] - want_med) > 1:
            med_ok = False
            details.append(f"{m}: median {row['median']:.0f} != {want_med}")
        if int(row["n"]) != want_n:
            n_ok = False
            details.append(f"{m}: n {int(row['n'])} != {want_n}")
    check("monthly medians match ground truth (10 buckets)", med_ok,
          "; ".join(details) if details else
          f"all {len(stats)} bucket medians within $1 of truth")
    check("monthly counts match ground truth", n_ok,
          "; ".join(d for d in details if " n " in d) or
          f"counts {[int(x) for x in stats['n']]}")

    # ---- 5. mileage adjustment -----------------------------------------------
    print("\n== mileage adjustment ==")
    mfit = analyze.fit_mileage(prepped)
    true_pct = (pd.Series([truth["beta_per_km"]]).mul(10_000).apply(
        lambda b: (2.718281828 ** b - 1) * 100)).iloc[0]
    check("mileage fit accepted on dense segment", bool(mfit.get("usable")),
          f"usable={mfit.get('usable')}, reason='{mfit.get('reason', '')}'")
    if mfit.get("usable"):
        got = mfit["pct_per_10k_km"]
        check("mileage slope near truth", -5.0 < got < -1.5,
              f"fit {got:+.2f}%/10k km vs true {true_pct:+.2f}%/10k km (n={mfit['n']})")
    sparse_fit = analyze.fit_mileage(analyze.prep(df[df["vehicle_type"] == "bike"]))
    check("mileage fit refused on sparse segment", not sparse_fit.get("usable"),
          f"reason: '{sparse_fit.get('reason', '')}'")

    # ---- 6. forecasting ladder ------------------------------------------------
    print("\n== Theil-Sen: numpy against scipy.stats.theilslopes ==")
    import warnings
    import numpy as np
    from scipy.stats import theilslopes
    rng = np.random.default_rng(7)
    same = total = 0
    for t in range(5000):
        n = int(rng.integers(2, 13))
        x = (np.sort(rng.integers(0, 6, n)).astype(float) if t % 3 == 0
             else np.arange(n, dtype=float))
        y = np.round(rng.normal(30000, 9000, n)) if t % 2 else rng.normal(0, 1, n)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s1, c1, _, _ = theilslopes(y, x)
        s2, c2 = forecast._theil_sen(x, y)
        total += 1
        same += (np.isnan(s1) and np.isnan(s2)) or (s1 == s2 and c1 == c2)
    check("our Theil-Sen equals scipy's bit for bit, including tied x values", same == total,
          f"{same}/{total} identical (every third input has repeated months)")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s1, _, _, _ = theilslopes(np.array([1.0, 2.0, 3.0]), np.array([4.0, 4.0, 4.0]))
        s2, _ = forecast._theil_sen(np.array([4.0, 4.0, 4.0]), np.array([1.0, 2.0, 3.0]))
    check("all x equal gives no slope in both, rather than a number",
          np.isnan(s1) and np.isnan(s2), f"scipy {s1}, ours {s2}")

    print("\n== forecasting ==")
    prepped_adj = analyze.apply_mileage_adjustment(prepped, mfit)
    pcol = "price_adj" if mfit.get("usable") else "price_aud"
    stats_adj = analyze.bucket_stats(prepped_adj, pcol)
    fc = forecast.forecast_segment(prepped_adj, stats_adj, horizon=3, price_col=pcol)
    check("dense segment gets a real forecast",
          fc["method"] in ("ets", "robust_trend"),
          f"method={fc['method']}, n={fc['n_total']}, buckets={fc['n_buckets']}")
    pts = fc["points"]
    ok_pts = pts is not None and len(pts) == 3 and \
        bool(((pts["lo"] <= pts["forecast"]) & (pts["forecast"] <= pts["hi"])
              & (pts["lo"] > 0) & (pts["hi"] > pts["lo"])).all())
    check("forecast has 3 points with sane 80% intervals", ok_pts,
          pts.to_string(index=False) if pts is not None else "no points")
    if ok_pts:
        # The forecast is on mileage-adjusted prices (restated to the
        # segment's median odometer), so restate the generator's truth —
        # defined at ODO_CENTER — onto the same basis before comparing.
        import math
        basis = math.exp(truth["beta_per_km"]
                         * (mfit["median_odo"] - make_synthetic.ODO_CENTER)) \
            if mfit.get("usable") else 1.0
        brack, msgs = True, []
        for step, (_, r) in enumerate(pts.iterrows(), start=1):
            truth_level = truth["true_level_by_idx"][str(9 + step)] * basis
            inside = r["lo"] <= truth_level <= r["hi"]
            brack &= inside
            msgs.append(f"m+{step}: true {truth_level:.0f} in "
                        f"[{r['lo']:.0f}, {r['hi']:.0f}] {'ok' if inside else 'MISS'}")
        check("interval brackets the true future level (same odo basis)",
              brack, "; ".join(msgs))
    check("direction is 'down' (true slope -$150/mo)", fc["direction"] == "down",
          f"direction={fc['direction']}")

    bikes_prep = analyze.prep(df[df["vehicle_type"] == "bike"])
    bikes_stats = analyze.bucket_stats(bikes_prep)
    bfc = forecast.forecast_segment(bikes_prep, bikes_stats, horizon=3)
    check("sparse segment REFUSES to forecast",
          bfc["method"] == "insufficient" and bfc["points"] is None
          and "not enough data" in bfc.get("verdict", ""),
          f"method={bfc['method']}, verdict: {bfc.get('verdict', '')[:90]}")

    # ---- 7. CLI, reports, exports ---------------------------------------------
    print("\n== CLI / reports / exports ==")
    rc_add = cli_main(["--db", str(DB), "add", "--make", "Honda", "--model",
                       "CB500X", "--year", "2019", "--price", "$8,990 neg",
                       "--odo", "23,000 km", "--state", "VIC", "--seller",
                       "private", "--vehicle-type", "bike", "--date", "2026-07-01"])
    st2 = Store(DB).status()
    check("manual add via CLI", rc_add == 0 and st2["vehicles"] == st["vehicles"] + 1,
          f"rc={rc_add}, vehicles {st['vehicles']} -> {st2['vehicles']}")
    rc = cli_main(["--db", str(DB), "report", "--all", "--out", str(OUTDIR)])
    fdir = OUTDIR / "reports" / "ford-falcon-2011-2013"
    files = ["stats.csv", "forecast.csv", "listings.csv", "trend.png",
             "forecast.png", "trend.html", "forecast.html"]
    missing = [f for f in files if not (fdir / f).exists()]
    check("report --all writes full artefact set", rc == 0 and not missing,
          f"rc={rc}, missing: {missing or 'none'} in {fdir.name}")
    ydir = OUTDIR / "reports" / "yamaha-yz250f-2020-2022"
    check("insufficient segment: charts yes, forecast.csv NO",
          (ydir / "forecast.png").exists() and not (ydir / "forecast.csv").exists(),
          f"{ydir.name}: forecast.png={ (ydir / 'forecast.png').exists() }, "
          f"forecast.csv={(ydir / 'forecast.csv').exists()}")
    rc = cli_main(["--db", str(DB), "export", "--out", str(OUTDIR)])
    ds = pd.read_csv(OUTDIR / "exports" / "dataset.csv")
    want_rows = truth["expected_observations"] + 1   # + manual add
    check("dataset export row count", rc == 0 and len(ds) == want_rows,
          f"rc={rc}, {len(ds)} rows, expected {want_rows}")
    store.close()

    # ---- summary ---------------------------------------------------------------
    fails = [name for ok, name in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
