"""Segment statistics: time-bucketed medians/means/IQR/volume, and
odometer (mileage) adjustment where the data can support it.

Every number travels with its sample size. Median is the headline stat;
used-vehicle prices are right-skewed and thin, so means are reported but
never trusted alone.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Gates for fitting a price~odometer model. Below these, adjustment is
# refused (better no adjustment than a fit driven by three points).
MILEAGE_MIN_N = 20
MILEAGE_MIN_ODO_SPREAD_KM = 20_000


def prep(df: pd.DataFrame, freq: str = "MS") -> pd.DataFrame:
    """Priced observations only, one row per vehicle per time bucket
    (last sighting in the bucket wins, so a vehicle seen 3x in a month
    counts once at its latest price)."""
    out = df.dropna(subset=["price_aud"]).copy()
    if out.empty:
        out["bucket"] = pd.Series(dtype="datetime64[ns]")
        return out
    out["bucket"] = out["capture_date"].dt.to_period(
        {"MS": "M", "ME": "M", "M": "M", "W": "W"}.get(freq, "M")).dt.start_time
    # kind="stable": two sightings of one car on one day (a merged identity,
    # see collided_keys) resolve to the one stored later, every time - the
    # default quicksort left that to numpy's internals, and the phone copy
    # (vpa/phone_app/stats.js) reproduces this rule exactly.
    # tail(1) keeps the whole latest row per vehicle per bucket — .last()
    # would take the last non-null value per column and could stitch fields
    # from different captures together.
    out = (out.sort_values("capture_date", kind="stable")
              .groupby(["key", "bucket"], observed=True)
              .tail(1)
              .reset_index(drop=True))
    return out


def latest_per_vehicle(prepped: pd.DataFrame) -> pd.DataFrame:
    """One row per physical car — its most recent priced sighting.

    `prep()` already collapses to one row per vehicle per BUCKET, which is
    right for a time series but wrong for "how does this car rank against
    the others": a car that has sat unsold across seven captures would vote
    seven times in the distribution. Collapsing to the last bucket gives one
    price per car, which is the population a ranking has to be over.

    Lives here, not inline in a page, so a rank is never derived two ways in
    two files."""
    if prepped.empty:
        return prepped
    return (prepped.sort_values("capture_date", kind="stable")
                   .groupby("key", observed=True).tail(1)
                   .reset_index(drop=True))


def bucket_stats(prepped: pd.DataFrame, price_col: str = "price_aud") -> pd.DataFrame:
    """Per-bucket n, median, mean, IQR, min/max, distinct-vehicle volume."""
    if prepped.empty:
        return pd.DataFrame(columns=["bucket", "n", "median", "mean",
                                     "q25", "q75", "min", "max"])
    g = prepped.groupby("bucket", observed=True)[price_col]
    stats = pd.DataFrame({
        "n": g.size(), "median": g.median(), "mean": g.mean(),
        "q25": g.quantile(0.25), "q75": g.quantile(0.75),
        "min": g.min(), "max": g.max(),
    }).reset_index().sort_values("bucket")
    for c in ("median", "mean", "q25", "q75", "min", "max"):
        stats[c] = stats[c].round(0)
    return stats


def fit_mileage(prepped: pd.DataFrame) -> dict:
    """Robust fit of log(price) ~ odometer within a segment.

    Returns {'usable': bool, 'reason': str, 'slope_per_km': float,
    'pct_per_10k_km': float, 'n': int, 'median_odo': float}.
    Refuses when data is thin, odometer spread is small, or the slope is
    positive (a segment where more km = more money means the fit is picking
    up something else — variant mix, not wear)."""
    d = prepped.dropna(subset=["price_aud", "odometer_km"])
    d = d[d["price_aud"] > 0]
    n = len(d)
    if n < MILEAGE_MIN_N:
        return {"usable": False, "reason": f"only {n} priced listings with odometer (need {MILEAGE_MIN_N})", "n": n}
    spread = d["odometer_km"].quantile(0.9) - d["odometer_km"].quantile(0.1)
    if spread < MILEAGE_MIN_ODO_SPREAD_KM:
        return {"usable": False, "reason": f"odometer spread {spread:,.0f} km too narrow", "n": n}
    try:
        import statsmodels.api as sm
        X = sm.add_constant(d["odometer_km"].to_numpy(dtype=float))
        y = np.log(d["price_aud"].to_numpy(dtype=float))
        fit = sm.RLM(y, X, M=sm.robust.norms.HuberT()).fit()
        slope = float(fit.params[1])
    except Exception as e:  # numerical failure -> no adjustment, say why
        return {"usable": False, "reason": f"fit failed: {e}", "n": n}
    if slope >= 0:
        return {"usable": False, "reason": "price does not fall with km here (mixed variants?)", "n": n}
    return {"usable": True, "reason": "", "slope_per_km": slope,
            "pct_per_10k_km": float((np.exp(slope * 10_000) - 1) * 100),
            "n": n, "median_odo": float(d["odometer_km"].median())}


def apply_mileage_adjustment(prepped: pd.DataFrame, fit: dict) -> pd.DataFrame:
    """Add price_adj: each price restated as if the vehicle had the segment's
    median odometer. Listings without an odometer keep their raw price."""
    out = prepped.copy()
    if not fit.get("usable"):
        out["price_adj"] = out["price_aud"]
        return out
    slope, med = fit["slope_per_km"], fit["median_odo"]
    delta = (med - out["odometer_km"]).fillna(0.0)
    out["price_adj"] = out["price_aud"] * np.exp(slope * delta)
    return out


def segment_summary(df: pd.DataFrame, level_col: str = "segment") -> pd.DataFrame:
    """One row per segment: totals for the 'everything' view."""
    priced = df.dropna(subset=["price_aud"])
    if priced.empty:
        return pd.DataFrame()
    g = priced.groupby(level_col, observed=True)
    out = pd.DataFrame({
        "listings": g["key"].nunique(),
        "observations": g.size(),
        "median_price": g["price_aud"].median().round(0),
        "mean_price": g["price_aud"].mean().round(0),
        "q25": g["price_aud"].quantile(0.25).round(0),
        "q75": g["price_aud"].quantile(0.75).round(0),
        "median_odo_km": g["odometer_km"].median().round(0),
        "pct_odo_missing": (g["odometer_km"].agg(lambda s: s.isna().mean()) * 100).round(0),
        "first_seen": g["capture_date"].min().dt.date,
        "last_seen": g["capture_date"].max().dt.date,
    }).reset_index().sort_values("listings", ascending=False)
    return out


#: below this a fitted curve is a drawing, not a distribution
CURVE_MIN_N = 12


def price_curve(prices, grid: int = 72) -> dict:
    """Fit a bell curve to asking prices and return it ready to draw.

    Fitted on **log price**, not price. Used-vehicle prices are right-skewed
    - a long tail of rare and near-new cars, a floor at zero - so a plain
    normal curve would put its left tail below $0 and understate how many
    cars sit above the mean. A normal on log price (a log-normal) has the
    right shape, is the standard model for prices, and is the same transform
    `fit_mileage` already works in.

    Returns mu/sigma in LOG space, the sd marks translated back to dollars,
    the density sampled across `grid` points for drawing, and `log_skew` -
    how well the log-normal actually fits, so a caller can say so rather
    than implying the curve is gospel.

    `usable` is False (with a reason) below CURVE_MIN_N prices or when every
    price is identical; callers must not draw a curve in that case.
    """
    s = pd.Series([p for p in prices if p is not None], dtype="float64").dropna()
    s = s[s > 0]
    n = int(len(s))
    if n < CURVE_MIN_N:
        return {"usable": False, "n": n,
                "reason": f"only {n} price(s) - {CURVE_MIN_N} are needed "
                          f"before a distribution means anything"}
    ln = np.log(s.to_numpy(dtype=float))
    mu, sigma = float(ln.mean()), float(ln.std(ddof=1))
    if not np.isfinite(sigma) or sigma <= 0:
        return {"usable": False, "n": n,
                "reason": "every price here is the same"}

    lo, hi = float(s.min()), float(s.max())
    # draw across the data's own range, widened to the 3-sigma tails so the
    # curve does not appear clipped
    x0 = max(min(lo, float(np.exp(mu - 3 * sigma))), 1.0)
    x1 = max(hi, float(np.exp(mu + 3 * sigma)))
    xs = np.linspace(x0, x1, grid)
    # log-normal density in PRICE space (the 1/x is the Jacobian; dropping it
    # is the classic mistake and tilts the curve)
    dens = (1.0 / (xs * sigma * np.sqrt(2 * np.pi))
            * np.exp(-((np.log(xs) - mu) ** 2) / (2 * sigma ** 2)))

    from scipy.stats import norm, skew
    vals = s.to_numpy(dtype=float)
    marks = []
    for k in (-2, -1, 0, 1, 2):
        price = float(np.exp(mu + k * sigma))
        # How many cars ACTUALLY sit beyond this mark against how many the
        # curve predicts. Used-car tails are heavier than any tidy curve, so
        # a caller can show the gap instead of implying the curve is gospel -
        # and the excess IS the set of standout listings.
        if k < 0:
            actual, expected = int((vals < price).sum()), float(norm.cdf(k)) * n
        elif k > 0:
            actual, expected = int((vals > price).sum()), float(norm.sf(k)) * n
        else:
            actual, expected = int((vals > price).sum()), 0.5 * n
        marks.append({"sd": k, "price": price, "beyond": actual,
                      "expected": round(expected, 1)})
    return {
        "usable": True, "n": n, "reason": "",
        "mu": mu, "sigma": sigma,
        "median": float(np.exp(mu)),           # exp(mu) IS the median here
        "mean": float(np.exp(mu + sigma ** 2 / 2)),
        "lo": lo, "hi": hi,
        "x": [float(v) for v in xs], "y": [float(v) for v in dens],
        "marks": marks,
        "raw_skew": float(skew(s.to_numpy(dtype=float))),
        "log_skew": float(skew(ln)),
    }


def z_of(price, curve: dict):
    """How many standard deviations from the middle, in log space - so a car
    at half the typical price and one at double are the same distance out,
    which is how price actually behaves."""
    if not curve.get("usable") or price is None or price <= 0:
        return None
    return round((float(np.log(price)) - curve["mu"]) / curve["sigma"], 2)


def live_keys(df: pd.DataFrame) -> tuple[set, str | None]:
    """The vehicles seen in THIS selection's most recent capture, and the day.

    "Live" here means exactly that and nothing more. A car absent from the
    latest capture may have sold, or may simply not have been in the search
    that was captured - listing identity also breaks across capture eras, so
    absence is often a format artefact. Callers must present this as "seen in
    the capture of <date>", never as "still for sale".

    Uses the selection's OWN latest day, not the database's: different trims
    were captured on different days.
    """
    priced = df.dropna(subset=["price_aud"])
    if priced.empty:
        return set(), None
    day = priced["capture_date"]
    if pd.api.types.is_datetime64_any_dtype(day):
        day = day.dt.strftime("%Y-%m-%d")
    latest = str(day.max())
    return set(priced.loc[day == latest, "key"]), latest


def collided_keys(df: pd.DataFrame) -> set:
    """Keys holding two different prices on the SAME capture day.

    One car cannot be advertised at two prices on one day, so such a key is
    two different cars merged into one identity. Two known causes:

    * the id-less fingerprint buckets odometer at 5,000 km, so two cars of
      the same year/variant/state with similar mileage collapse together
      (e.g. 318,200 km and 315,000 km both land in bucket 63);
    * v2-era `IMG-` ids are derived from image URLs and are not unique per
      listing, so two ads can share one.

    Their price difference is NOT a price movement. Callers must exclude
    these keys before reporting movement, or the collision is published as a
    market signal.
    """
    priced = df.dropna(subset=["price_aud"])
    if priced.empty:
        return set()
    day = priced["capture_date"]
    if pd.api.types.is_datetime64_any_dtype(day):
        day = day.dt.strftime("%Y-%m-%d")
    n = priced.groupby([priced["key"], day], observed=True)["price_aud"].nunique()
    return set(n[n > 1].index.get_level_values(0))


def data_caveats(prepped: pd.DataFrame, stats: pd.DataFrame) -> list[str]:
    """Honest health warnings that ride along with every report."""
    cav: list[str] = []
    if prepped.empty:
        return ["no priced listings"]
    pct_no_odo = prepped["odometer_km"].isna().mean() * 100
    if pct_no_odo > 30:
        cav.append(f"odometer missing for {pct_no_odo:.0f}% of listings")
    if prepped["source"].nunique() == 1:
        cav.append(f"single source only ({prepped['source'].iloc[0]})")
    states = prepped["state"].dropna().unique()
    if len(states) == 1:
        cav.append(f"one state only ({states[0]})")
    elif prepped["state"].isna().mean() > 0.5:
        cav.append("state unknown for most listings")
    if not stats.empty and stats.iloc[-1]["n"] < 3:
        cav.append(f"latest bucket has only {int(stats.iloc[-1]['n'])} listings")
    mix = prepped["seller_type"].value_counts(normalize=True, dropna=True)
    if len(mix) == 2 and 0.2 < mix.min() < 0.8:
        pass  # healthy mix, nothing to flag
    elif "dealer" in mix.index and mix.get("dealer", 0) > 0.8:
        cav.append("mostly dealer listings (asking prices skew high)")
    return cav
