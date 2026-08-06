"""Report orchestration: segment -> stats table + charts + forecast + CSVs.

Forecasts run on mileage-adjusted prices when the segment earns the
adjustment (see analyze.fit_mileage), otherwise raw prices — and the report
always says which one you're looking at.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import analyze, charts, forecast
from .segments import add_segment_cols, filter_segment, segment_groups, slugify
from .store import Store

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "output"
MIN_SEGMENT_N = 5   # segments smaller than this are listed but not reported


def _segment_report(name: str, sub: pd.DataFrame, outdir: Path, freq: str,
                    horizon: int, make_charts: bool = True) -> dict:
    """Compute everything for one segment; write files; return summary dict."""
    prepped = analyze.prep(sub, freq=freq)
    mfit = analyze.fit_mileage(prepped)
    prepped = analyze.apply_mileage_adjustment(prepped, mfit)
    price_col = "price_adj" if mfit.get("usable") else "price_aud"

    stats_raw = analyze.bucket_stats(prepped, "price_aud")
    stats = analyze.bucket_stats(prepped, price_col) if price_col != "price_aud" else stats_raw

    caveats = analyze.data_caveats(prepped, stats)
    if mfit.get("usable"):
        caveats.append(
            f"prices mileage-adjusted to {mfit['median_odo']:,.0f} km "
            f"({mfit['pct_per_10k_km']:+.1f}% per 10,000 km, n={mfit['n']})")
    else:
        caveats.append(f"no mileage adjustment: {mfit.get('reason', 'n/a')}")

    fc = forecast.forecast_segment(prepped, stats, horizon=horizon,
                                   price_col=price_col, extra_caveats=caveats)

    seg_dir = outdir / "reports" / slugify(name)
    seg_dir.mkdir(parents=True, exist_ok=True)
    stats_out = stats_raw.merge(
        stats[["bucket", "median"]].rename(columns={"median": "median_adjusted"}),
        on="bucket", how="left") if price_col != "price_aud" else stats_raw
    stats_out.to_csv(seg_dir / "stats.csv", index=False)
    if fc["points"] is not None:
        fc["points"].to_csv(seg_dir / "forecast.csv", index=False)
    cols = ["key", "capture_date", "source", "make", "model", "variant", "year",
            "price_aud", "price_adj", "odometer_km", "state", "seller_type",
            "raw_title", "url"]
    prepped[[c for c in cols if c in prepped.columns]].to_csv(
        seg_dir / "listings.csv", index=False)

    if make_charts and not prepped.empty:
        # One shared plotly.min.js for the whole report tree - see charts.py.
        charts.write_plotly_bundle(seg_dir.parent)
        js = charts.SHARED_PLOTLYJS
        charts.trend_png(prepped, stats, name, seg_dir / "trend.png", price_col)
        charts.trend_html(prepped, stats, name, seg_dir / "trend.html",
                          price_col, plotlyjs=js)
        charts.forecast_png(stats, fc, name, seg_dir / "forecast.png")
        charts.forecast_html(stats, fc, name, seg_dir / "forecast.html",
                             plotlyjs=js)

    return {"segment": name, "dir": seg_dir, "stats": stats_out, "forecast": fc,
            "mileage": mfit, "n_vehicles": int(prepped["key"].nunique()) if not prepped.empty else 0}


def _print_segment(res: dict) -> None:
    fc = res["forecast"]
    print("=" * 72)
    print(f"SEGMENT: {res['segment']}   (distinct vehicles: {res['n_vehicles']})")
    print("=" * 72)
    stats = res["stats"]
    if stats.empty:
        print("  no priced listings.")
        return
    show = stats.copy()
    show["bucket"] = pd.DatetimeIndex(show["bucket"]).strftime("%Y-%m")
    print(show.to_string(index=False))
    print(f"\nFORECAST [{fc['method']}] n={fc['n_total']}, buckets={fc['n_buckets']}:")
    print(f"  {fc.get('verdict', '')}")
    if fc["points"] is not None:
        pts = fc["points"].copy()
        pts["bucket"] = pd.DatetimeIndex(pts["bucket"]).strftime("%Y-%m")
        for _, r in pts.iterrows():
            print(f"  {r['bucket']}: A${r['forecast']:,.0f}  "
                  f"[{fc['interval_level']}% PI: A${r['lo']:,.0f} - A${r['hi']:,.0f}]")
    if fc["caveats"]:
        print("  caveats:")
        for c in fc["caveats"]:
            print(f"    - {c}")
    print(f"  files: {res['dir']}")
    print()


def run_report(store: Store, query: str | None = None, band: str | None = None,
               level: str = "band", freq: str = "MS", horizon: int = 3,
               outdir: Path = DEFAULT_OUT, min_n: int = MIN_SEGMENT_N,
               band_width: int = 3, make_charts: bool = True,
               everything: bool = False) -> list[dict]:
    df = add_segment_cols(store.frame(), band_width)
    if df.empty:
        print("Dataset is empty - ingest something first.")
        return []
    results = []
    if query:
        sub = filter_segment(df, query, band)
        if sub.empty:
            print(f"No listings match '{query}'" + (f" in {band}" if band else "") + ".")
            return []
        # Label honestly: a query slice can span several segments.
        segs = sub["segment"].unique()
        lines = sub["model_line"].unique()
        if band is not None:
            name = f"{lines[0] if len(lines) == 1 else query} {band}"
        elif len(segs) == 1:
            name = str(segs[0])
        elif len(lines) == 1:
            name = f"{lines[0]} (all years)"
        else:
            name = f"query '{query}' ({len(lines)} model lines)"
        results.append(_segment_report(name, sub, outdir, freq, horizon, make_charts))
    elif everything:
        skipped = []
        for name, sub in segment_groups(df, level):
            if sub["key"].nunique() < min_n:
                skipped.append((name, sub["key"].nunique()))
                continue
            results.append(_segment_report(name, sub, outdir, freq, horizon, make_charts))
        if skipped:
            print(f"Skipped {len(skipped)} segment(s) with fewer than {min_n} vehicles:")
            for name, n in sorted(skipped, key=lambda t: -t[1]):
                print(f"  - {name} (n={n})")
            print()
    for res in results:
        _print_segment(res)
    return results


def export_all(store: Store, outdir: Path = DEFAULT_OUT) -> dict[str, Path]:
    """Full normalised dataset + segment summary + price changes -> CSV."""
    exp = outdir / "exports"
    exp.mkdir(parents=True, exist_ok=True)
    df = add_segment_cols(store.frame())
    paths = {}
    p = exp / "dataset.csv"
    df.to_csv(p, index=False)
    paths["dataset"] = p
    if not df.empty:
        p = exp / "segment_summary.csv"
        analyze.segment_summary(df).to_csv(p, index=False)
        paths["segment_summary"] = p
        p = exp / "model_line_summary.csv"
        analyze.segment_summary(df, "model_line").to_csv(p, index=False)
        paths["model_line_summary"] = p
        changes = store.price_changes()
        if not changes.empty:
            p = exp / "price_changes.csv"
            changes.to_csv(p, index=False)
            paths["price_changes"] = p
    return paths
