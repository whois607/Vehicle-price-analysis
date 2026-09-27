"""Performance work, held to the behaviour it replaced.

    python -m tests.check_perf

Every speed-up here is a rewrite of something that already worked, so each
check compares the fast version against the slow one it replaced - kept
verbatim below as a reference - rather than against expected numbers:

  1. forecast._theil_sen is numpy, not scipy.stats.theilslopes: bit-for-bit
     equal, on random, tied and degenerate inputs;
  2. turnover.cohort and turnover._day_sets are single passes, not pandas
     string methods and a groupby loop: equal on awkward values (None, NaN,
     pd.NA, ints, derived ids, a missing capture day);
  3. turnover.by_variant is cached - keyed on a hash of every input cell, so
     a manual price correction that changes no count still invalidates it,
     and a caller can never alter what the next caller receives;
  4. the Explore / Compare pages fetch "latest wins", so a slow answer to an
     earlier click can no longer overwrite a later one, and show that they
     are working.

The whole-payload check - every selection in the real database, old code
against new, byte-identical JSON - is too slow and too data-dependent to run
here; NOTES.md says how it was done.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from vpa import forecast, turnover
from vpa.merge import SITE_ID_PREFIXES

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


# ------------------------------------------------ the replaced implementations
def old_cohort(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "listing_id" not in df.columns:
        return df.iloc[0:0]
    lid = df["listing_id"].astype("string").fillna("")
    mask = lid.str.startswith(SITE_ID_PREFIXES[0], na=False)
    for p in SITE_ID_PREFIXES[1:]:
        mask = mask | lid.str.startswith(p, na=False)
    return df[mask.fillna(False)]


def old_day_sets(df: pd.DataFrame) -> dict:
    out = {}
    if df.empty:
        return out
    for day, g in df.groupby("day"):
        out[str(day)] = set(str(k) for k in g["key"])
    return out


def old_robust_trend(prepped, stats, x, y, fx, future, interval, price_col):
    """forecast._robust_trend as it was: rng.choice and a full Theil-Sen per draw."""
    slope, intercept = forecast._theil_sen(x, y)
    point = intercept + slope * fx
    resid = y - (intercept + slope * x)
    rng = np.random.default_rng(forecast._RNG_SEED)
    buckets = list(stats["bucket"])
    groups = {b: g[price_col].to_numpy(dtype=float)
              for b, g in prepped.groupby("bucket", observed=True)}
    sims = np.empty((forecast.BOOTSTRAP_B, len(fx)))
    for i in range(forecast.BOOTSTRAP_B):
        med = np.array([np.median(rng.choice(groups[b], size=len(groups[b]),
                                             replace=True)) for b in buckets])
        try:
            s_, c = forecast._theil_sen(x, med)
        except Exception:
            s_, c = slope, intercept
        noise = rng.choice(resid, size=len(fx), replace=True) if len(resid) else 0.0
        sims[i] = c + s_ * fx + noise
    lo_q, hi_q = (1 - interval) / 2, 1 - (1 - interval) / 2
    lo = np.maximum(np.quantile(sims, lo_q, axis=0), 0)
    hi = np.quantile(sims, hi_q, axis=0)
    return pd.DataFrame({"bucket": future, "forecast": point.round(0),
                         "lo": lo.round(0), "hi": hi.round(0)})


def line_frame(n_trims=6, cars=10, days=("2026-03-01", "2026-03-08",
                                         "2026-03-15", "2026-03-22")):
    """A model line whose trims each lose a car per week."""
    rows = []
    for t in range(n_trims):
        for c in range(cars):
            for di, d in enumerate(days):
                if c < di:                 # car c has left by capture c+1
                    continue
                rows.append({"key": f"k{t}-{c}", "listing_id": f"SSE-AD-{t}{c:03d}",
                             "day": d, "variant": f"T{t}",
                             "price_aud": 20000.0 + 500 * c + 1000 * t,
                             "odometer_km": 100000.0 + c, "year": 2014,
                             "state": "NSW", "seller_type": "dealer",
                             "raw_title": f"car {t}-{c}", "url": None})
    return pd.DataFrame(rows)


def main() -> int:
    print("\n== Theil-Sen: numpy against scipy.stats.theilslopes ==")
    from scipy.stats import theilslopes
    import warnings
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
    check("equal bit for bit, including tied x values", same == total,
          f"{same}/{total} identical (every third input has repeated months)")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s1, _, _, _ = theilslopes(np.array([1.0, 2.0, 3.0]), np.array([4.0, 4.0, 4.0]))
        s2, _ = forecast._theil_sen(np.array([4.0, 4.0, 4.0]), np.array([1.0, 2.0, 3.0]))
    check("all x equal gives no slope in both, rather than a number",
          np.isnan(s1) and np.isnan(s2), f"scipy {s1}, ours {s2}")

    print("\n== the forecast bootstrap: same draws, same intervals ==")
    rng = np.random.default_rng(3)
    identical = n_cases = 0
    for _ in range(12):
        months = pd.date_range("2025-01-01", periods=int(rng.integers(3, 9)), freq="MS")
        rows = []
        for mi, m in enumerate(months):
            for c in range(int(rng.integers(1, 30))):     # one-car months included
                rows.append({"bucket": m, "key": f"c{c}",
                             "price_aud": float(round(rng.normal(30000 - 400 * mi, 6000)))})
        prepped = pd.DataFrame(rows)
        stats = (prepped.groupby("bucket")["price_aud"].median()
                 .rename("median").reset_index())
        x = forecast._month_index(stats["bucket"])
        y = stats["median"].to_numpy(dtype=float)
        fx = x[-1] + np.arange(1, 4)
        fut = forecast._future_buckets(pd.Timestamp(stats.iloc[-1]["bucket"]), 3)
        new = forecast._robust_trend(prepped, stats, x, y, fx, fut, 0.8, "price_aud")
        old = old_robust_trend(prepped, stats, x, y, fx, fut, 0.8, "price_aud")
        n_cases += 1
        identical += new.equals(old)
    check("500-draw intervals identical to the old loop",
          identical == n_cases, f"{identical}/{n_cases} forecasts identical")

    print("\n== cohort and day sets: single passes against the old pandas code ==")
    awkward = pd.DataFrame({
        "key": ["a", "b", "c", "d", "e", "f", "g", "h", 5],
        "listing_id": ["SSE-AD-1", "OAG-AD-9", None, np.nan, "IMG-abc", "BASE-1",
                       12345, "sse-ad-lower", "SSE-AD-2"],
        "day": ["2026-03-01", "2026-03-01", "2026-03-08", None, "2026-03-08",
                "2026-03-01", "2026-03-08", "2026-03-01", "2026-03-08"],
    })
    awkward_na = awkward.astype({"listing_id": "object"})
    awkward_na.loc[3, "listing_id"] = pd.NA
    for label, frame in (("None/NaN/int/derived ids", awkward),
                         ("pd.NA in the id column", awkward_na)):
        new, old = turnover.cohort(frame), old_cohort(frame)
        check(f"cohort keeps exactly the same rows ({label})",
              list(new.index) == list(old.index),
              f"kept rows {list(new.index)}")
    new_d, old_d = turnover._day_sets(awkward), old_day_sets(awkward)
    check("day sets equal, a missing day skipped, days in order",
          new_d == old_d and list(new_d) == list(old_d),
          f"{ {d: sorted(v) for d, v in new_d.items()} }")
    line = line_frame()
    check("day sets equal on a whole model line",
          turnover._day_sets(line) == old_day_sets(line),
          f"{len(turnover._day_sets(line))} days")

    print("\n== by_variant cache: keyed on content, never shared by reference ==")
    turnover._BV_CACHE.clear()
    calls = {"n": 0}
    real = turnover.by_variant_uncached

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    turnover.by_variant_uncached = counting
    try:
        line = line_frame()
        first = turnover.by_variant(line, selected=["T1"], full_line=line)
        second = turnover.by_variant(line, selected=["T2"], full_line=line)
        check("the same rows asked twice are computed once",
              calls["n"] == 1, f"{calls['n']} computation(s) for two requests")
        sel1 = [r["variant"] for r in first["rows"] if r["selected"]]
        sel2 = [r["variant"] for r in second["rows"] if r["selected"]]
        check("the highlighted trim is per request, not cached",
              sel1 == ["T1"] and sel2 == ["T2"], f"first {sel1}, second {sel2}")
        ref = real(line, ["T2"], line)
        check("a cached answer equals a fresh computation",
              second == ref, f"{len(ref['rows'])} rows, pooled {ref['pooled']}")
        second["rows"][0]["rate30"] = -1
        second["pooled"]["exits"] = -1
        third = turnover.by_variant(line, selected=["T2"], full_line=line)
        check("altering a returned answer does not reach the next caller",
              third == ref, "the cache hands out copies")
        # a manual correction: same rows, same counts, one price different
        edited = line.copy()
        edited.loc[edited.index[0], "price_aud"] += 1
        before = calls["n"]
        turnover.by_variant(edited, selected=["T2"], full_line=edited)
        check("one corrected price, with every count unchanged, is a new key",
              calls["n"] == before + 1,
              "recomputed after a one-dollar edit that no count would notice")
        renamed = line.copy()
        renamed.loc[renamed.index[0], "state"] = "VIC"
        before = calls["n"]
        turnover.by_variant(renamed, selected=None, full_line=renamed)
        check("a change to a column the maths ignores still misses, never hits",
              calls["n"] == before + 1, "any cell counts; a stale hit is not possible")
        for i in range(turnover._BV_MAX + 5):
            other = line.copy()
            other["price_aud"] += i + 10
            turnover.by_variant(other, None, other)
        check("the cache is bounded", len(turnover._BV_CACHE) <= turnover._BV_MAX,
              f"{len(turnover._BV_CACHE)} entries, limit {turnover._BV_MAX}")
    finally:
        turnover.by_variant_uncached = real
        turnover._BV_CACHE.clear()

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


if __name__ == "__main__":
    sys.exit(main())
