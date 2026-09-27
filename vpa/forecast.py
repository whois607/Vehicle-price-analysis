"""Short-horizon forecasting for sparse, irregular listing data.

A three-rung ladder — the data earns its way up, never the other way:

  insufficient   < 3 time buckets or < 8 priced listings.
                 NO forecast. Reports current median + trend direction only.
  robust_trend   Theil-Sen slope on bucket medians. Prediction interval from
                 a bootstrap that resamples listings within buckets AND adds
                 empirical residual noise — so the interval reflects both
                 "which cars happened to list" and month-to-month wobble.
  ets            >= 8 buckets and >= 40 priced listings: damped additive-trend
                 exponential smoothing (statsmodels ETS) with model-based
                 intervals. Falls back to robust_trend if the fit fails.

Every result carries: method, n, buckets, interval level, caveats.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_BUCKETS = 3
MIN_N = 8
ETS_MIN_BUCKETS = 8
ETS_MIN_N = 40
FLAT_THRESHOLD = 0.02          # |total move over horizon| < 2% of level -> flat
BOOTSTRAP_B = 500
_RNG_SEED = 42


def _month_index(buckets) -> np.ndarray:
    """Bucket timestamps -> integer months since the first bucket."""
    b = pd.DatetimeIndex(buckets)
    return np.array((b.year * 12 + b.month) - (b.year[0] * 12 + b.month[0]),
                    dtype=float)


def _theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Theil-Sen slope and intercept - exactly scipy.stats.theilslopes with
    its default method='separate', without scipy's per-call overhead.

    The bootstrap below calls this 500 times on five or six monthly medians.
    scipy spends ~2 ms per call on input validation and array-API dispatch
    for arithmetic that takes microseconds, so it was over a second of every
    Explore page load. The algorithm is scipy's own: the median of the slope
    of every pair of points with distinct x, then median(y) - slope *
    median(x). tests/check_perf.py holds it to theilslopes on random and
    degenerate inputs.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    i, j = np.triu_indices(len(x), k=1)
    dx, dy = x[i] - x[j], y[i] - y[j]
    ok = dx != 0
    slope = float(np.median(dy[ok] / dx[ok])) if ok.any() else float("nan")
    return slope, float(np.median(y) - slope * np.median(x))


def _direction(slope: float, level: float, horizon: int) -> str:
    if level <= 0 or np.isnan(slope):
        return "unknown"
    move = slope * horizon / level
    if move > FLAT_THRESHOLD:
        return "up"
    if move < -FLAT_THRESHOLD:
        return "down"
    return "flat"


def _future_buckets(last_bucket: pd.Timestamp, horizon: int) -> pd.DatetimeIndex:
    return pd.date_range(last_bucket, periods=horizon + 1, freq="MS")[1:]


def forecast_segment(prepped: pd.DataFrame, stats: pd.DataFrame,
                     horizon: int = 3, interval: float = 0.80,
                     price_col: str = "price_aud",
                     extra_caveats: list[str] | None = None) -> dict:
    """prepped: one row per vehicle per bucket (analyze.prep output).
    stats: analyze.bucket_stats output for the same data/price_col."""
    caveats = list(extra_caveats or [])
    n_total = int(len(prepped))
    n_buckets = int(len(stats))
    base = {"n_total": n_total, "n_buckets": n_buckets,
            "horizon_months": horizon, "interval_level": int(interval * 100),
            "caveats": caveats, "points": None}

    if n_buckets == 0:
        return {**base, "method": "insufficient", "level": None,
                "direction": "unknown",
                "verdict": "no priced listings at all - nothing to say"}

    level = float(stats.iloc[-1]["median"])
    last_bucket = pd.Timestamp(stats.iloc[-1]["bucket"])

    x = _month_index(stats["bucket"])
    y = stats["median"].to_numpy(dtype=float)

    slope = np.nan
    if n_buckets >= 2:
        slope, intercept = _theil_sen(x, y) if n_buckets >= 3 else \
            ((y[-1] - y[0]) / max(x[-1] - x[0], 1.0), float(y[0]))
    direction = _direction(slope, level, horizon) if n_buckets >= 2 else "unknown"

    if n_buckets < MIN_BUCKETS or n_total < MIN_N:
        why = (f"{n_buckets} time bucket(s)" if n_buckets < MIN_BUCKETS
               else f"{n_total} priced listings")
        return {**base, "method": "insufficient", "level": level,
                "direction": direction,
                "verdict": (f"not enough data to forecast ({why}; need "
                            f">={MIN_BUCKETS} buckets and >={MIN_N} listings). "
                            f"Current median A${level:,.0f} (n={n_total}), "
                            f"direction: {direction}.")}

    fx = x[-1] + np.arange(1, horizon + 1)
    future = _future_buckets(last_bucket, horizon)

    if n_buckets >= ETS_MIN_BUCKETS and n_total >= ETS_MIN_N:
        try:
            res = _ets(stats, fx, future, horizon, interval)
            caveats.extend(res.pop("caveats_extra", []))
            return {**base, **res, "level": level, "direction": direction}
        except Exception as e:
            caveats.append(f"ETS fit failed ({type(e).__name__}), fell back to robust trend")

    pts = _robust_trend(prepped, stats, x, y, fx, future, interval, price_col)
    return {**base, "method": "robust_trend", "level": level,
            "direction": direction, "points": pts,
            "verdict": (f"median A${level:,.0f}, {direction} at "
                        f"A${slope:,.0f}/month (Theil-Sen, n={n_total}, "
                        f"{n_buckets} buckets)")}


def _robust_trend(prepped, stats, x, y, fx, future, interval, price_col) -> pd.DataFrame:
    slope, intercept = _theil_sen(x, y)
    point = intercept + slope * fx
    resid = y - (intercept + slope * x)

    rng = np.random.default_rng(_RNG_SEED)
    buckets = list(stats["bucket"])
    groups = {b: g[price_col].to_numpy(dtype=float)
              for b, g in prepped.groupby("bucket", observed=True)}
    pools = [groups[b] for b in buckets]
    sizes = [len(p) for p in pools]
    # x is the same on every draw, so the Theil-Sen pairs are set up once
    # (the arithmetic is _theil_sen's, operation for operation). And
    # rng.choice(a, size=n, replace=True) is a[rng.integers(0, n, size=n)]
    # draw for draw - same generator calls in the same order - minus the
    # per-call overhead, which across 500 draws x every bucket was most of
    # the forecast's cost. The intervals come out bit-identical.
    pi, pj = np.triu_indices(len(x), k=1)
    dx = x[pi] - x[pj]
    ok = dx != 0
    dx_ok, mx = dx[ok], np.median(x)
    nr = len(resid)
    sims = np.empty((BOOTSTRAP_B, len(fx)))
    for i in range(BOOTSTRAP_B):
        med = np.array([np.median(p[rng.integers(0, n, size=n)])
                        for p, n in zip(pools, sizes)])
        s = (float(np.median((med[pi] - med[pj])[ok] / dx_ok))
             if ok.any() else float("nan"))
        c = float(np.median(med) - s * mx)
        noise = resid[rng.integers(0, nr, size=len(fx))] if nr else 0.0
        sims[i] = c + s * fx + noise
    lo_q, hi_q = (1 - interval) / 2, 1 - (1 - interval) / 2
    lo = np.maximum(np.quantile(sims, lo_q, axis=0), 0)
    hi = np.quantile(sims, hi_q, axis=0)
    return pd.DataFrame({"bucket": future, "forecast": point.round(0),
                         "lo": lo.round(0), "hi": hi.round(0)})


def _ets(stats, fx, future, horizon, interval) -> dict:
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    s = (stats.set_index("bucket")["median"]
              .asfreq("MS"))
    gaps = int(s.isna().sum())
    caveat = []
    if gaps:
        s = s.interpolate()
        caveat.append(f"{gaps} empty month(s) interpolated for ETS")
    model = ETSModel(s.astype(float), error="add", trend="add", damped_trend=True)
    fit = model.fit(disp=False)
    pred = fit.get_prediction(start=len(s), end=len(s) + horizon - 1)
    sf = pred.summary_frame(alpha=1 - interval)
    pts = pd.DataFrame({"bucket": future,
                        "forecast": sf["mean"].to_numpy().round(0),
                        "lo": np.maximum(sf["pi_lower"].to_numpy(), 0).round(0),
                        "hi": sf["pi_upper"].to_numpy().round(0)})
    n = int(stats["n"].sum())
    return {"method": "ets", "points": pts,
            "verdict": (f"ETS (damped trend) on {len(s)} monthly medians, "
                        f"n={n} listings"),
            "caveats_extra": caveat}
