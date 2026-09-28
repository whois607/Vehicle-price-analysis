"""Explore — the individual-vehicle side of the app.

    served at /explore by importui's server

Deliberately separate from the import page. That page answers dataset-level
questions ("how much do I have, is it healthy, what came in last") and owns
ingestion. This one answers vehicle-level questions ("what is a G6E Turbo
FG X worth, which cars make up that number, which ones moved") and writes
nothing at all.

It is glue, like importui: every number comes from the SAME engines the CLI
reports and the Excel workbook use —

    analyze.prep / bucket_stats / fit_mileage / apply_mileage_adjustment /
    data_caveats     and     forecast.forecast_segment

so a median here, in `vpa report`, and in the workbook's Trend sheet are the
same computation on the same rows. Selection matches viewer_xlsx's `Match`:
exact equality at three levels (make / make+model / make+model+variant), so
"Ford Falcon" pools every trim and "Ford Falcon G6E FG X" pools only that
trim - identical to picking in the workbook.

Nothing is cached. Every request recomputes from the database, so a commit
on the import page is visible here on the next load; see `data_version()`
in importui for how open pages notice.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from . import dupes, links
from .store import Store, DEFAULT_DB

MAX_VEHICLES = 400          # table rows sent to the page; total is reported
MAX_SCATTER = 1200          # points drawn on the price-vs-odometer chart
MAX_STANDOUTS = 800         # rows behind the bell curve, sorted by z
DEFAULT_HORIZON = 3

# ---- gates for judging ONE car against its trim (car_payload below).
# All counted in DISTINCT VEHICLES, never rows: the median trim here holds 4
# cars and only a third reach 8, so these fire constantly and have to be
# right. Ranking one car against three others is noise with a number on it.
CAR_MIN_PEERS = 8           # below this, no rank at all - only comparables
CAR_PERCENTILE_MIN = 20     # below this, a literal count, never a percentile
CAR_COMPARABLES = 6         # nearest ads shown beside the subject
CAR_SPLIT_MIN_N = 8         # seller / state like-for-like median
CAR_YEAR_MIN_N = 5          # model-year like-for-like median
CAR_FIT_RANGE = (0.05, 0.95)   # odometer span the mileage fit may speak for
CAR_LINE_DISAGREE = 25      # percentile points before the line rank is worth a mention
CAR_STALE_DAYS = 21         # capture older than this is flagged on the card


def _frame(db_path: str | Path) -> pd.DataFrame:
    from .segments import add_segment_cols
    with Store(db_path) as store:
        df = add_segment_cols(store.frame())
    if not df.empty:
        df["day"] = df["capture_date"].dt.strftime("%Y-%m-%d")
    return df


def _select(df: pd.DataFrame, make, model, variant) -> pd.DataFrame:
    """Exact-equality selection, mirroring viewer_xlsx.Match's three levels.
    Substring matching was tried and removed: 'G6E' also caught 'G6E Turbo'
    and pooled 66 rows where 12 were meant."""
    sub = df
    if make:
        sub = sub[sub["make"] == make]
    if model:
        sub = sub[sub["model"] == model]
    if variant:
        sub = sub[sub["variant"] == variant]
    return sub


def _select_multi(df: pd.DataFrame, make, model, variants) -> pd.DataFrame:
    """`_select` widened to SEVERAL trims of one model, pooled into one set.

    An empty or missing list means every trim, which is exactly what
    `_select(make, model, None)` already meant — so one trim, three trims and
    all trims are the same code path and cannot drift apart.

    Pooling is only offered WITHIN one make and model. Trims of one model are
    the same car in different specifications and a median over them is a real
    number; pooling a Corvette with a Falcon would produce a confident number
    describing nothing.
    """
    variants = [v for v in (variants or []) if str(v).strip()]
    if not variants:
        return _select(df, make, model, None)
    sub = _select(df, make, model, None)
    return sub[sub["variant"].isin(variants)]


#: Columns a keyword is matched against. `variant` is the trim as stored;
#: `raw_title` is the seller's own words, which is where a name like
#: "Stingray" survives even when the trim column spells it differently.
KEYWORD_COLS = ("variant", "raw_title", "model")


def _apply_filters(df: pd.DataFrame, year_min=None, year_max=None,
                   price_min=None, price_max=None, keyword=None) -> tuple:
    """Narrow a selection by year, asking price and free text.

    Returns (filtered, applied) where `applied` describes each filter that
    actually ran, so the page can state what it is showing instead of
    quietly presenting a subset as if it were the whole selection — the same
    reason every other panel here prints its basis.

    Rows with NO year are kept by a year filter and rows with NO price are
    kept by a price filter. A blank is not a small number: dropping the
    unpriced rows would silently change the vehicle count as a side effect
    of asking for cars under $50,000.
    """
    applied = []
    out = df

    def num(v):
        try:
            return float(str(v).replace(",", "").replace("$", "").strip())
        except (TypeError, ValueError):
            return None

    y0, y1 = num(year_min), num(year_max)
    if y0 is not None or y1 is not None:
        keep = out["year"].isna()
        if y0 is not None:
            keep = keep | (out["year"] >= y0)
            if y1 is not None:
                keep = out["year"].isna() | ((out["year"] >= y0) & (out["year"] <= y1))
        elif y1 is not None:
            keep = keep | (out["year"] <= y1)
        out = out[keep]
        applied.append({"kind": "year", "text": (
            f"{int(y0) if y0 else ''}-{int(y1) if y1 else ''}".strip("-")
            if not (y0 and y1) else f"{int(y0)}-{int(y1)}")})

    p0, p1 = num(price_min), num(price_max)
    if p0 is not None or p1 is not None:
        keep = out["price_aud"].isna()
        cond = pd.Series(True, index=out.index)
        if p0 is not None:
            cond &= out["price_aud"] >= p0
        if p1 is not None:
            cond &= out["price_aud"] <= p1
        out = out[keep | cond]
        lo = f"${p0:,.0f}" if p0 is not None else ""
        hi = f"${p1:,.0f}" if p1 is not None else ""
        applied.append({"kind": "price",
                        "text": f"{lo} to {hi}".strip() if lo and hi
                                else (f"from {lo}" if lo else f"up to {hi}")})

    kw = (keyword or "").strip()
    if kw:
        hit = pd.Series(False, index=out.index)
        for col in KEYWORD_COLS:
            if col in out.columns:
                hit = hit | out[col].astype("string").str.contains(
                    kw, case=False, na=False, regex=False)
        out = out[hit]
        applied.append({"kind": "keyword", "text": f'"{kw}"'})
    return out, applied


def _num(v):
    """pandas/numpy scalar -> plain JSON-safe python (None for NaN)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if pd.isna(v):
        return None
    f = float(v)
    return round(f, 2)


def picker_payload(db_path: str | Path = DEFAULT_DB) -> dict:
    """The cascading make -> model -> trim tree, with counts, so the page can
    cascade without a round trip per level (same shape as the workbook's
    Picker sheet)."""
    df = _frame(db_path)
    if df.empty:
        return {"ok": True, "makes": []}
    priced = df.dropna(subset=["price_aud"])
    makes = []
    for mk, msub in priced.groupby("make"):
        models = []
        for mo, vsub in msub.groupby("model"):
            # Every field the page can sort trims by travels with the trim,
            # so re-sorting is instant and needs no extra request.
            variants = [
                {"name": str(va), "n": int(len(g)),
                 "vehicles": int(g["key"].nunique()),
                 "days": int(g["day"].nunique()),
                 "median": _num(g["price_aud"].median()),
                 "lo": _num(g["price_aud"].min()),
                 "hi": _num(g["price_aud"].max()),
                 "odo": _num(g["odometer_km"].median()),
                 "last": str(g["day"].max()), "first": str(g["day"].min())}
                for va, g in vsub.dropna(subset=["variant"]).groupby("variant")
                if str(va).strip()
            ]
            variants.sort(key=lambda v: -v["n"])
            models.append({"name": str(mo), "n": int(len(vsub)),
                           "vehicles": int(vsub["key"].nunique()),
                           "days": int(vsub["day"].nunique()),
                           "median": _num(vsub["price_aud"].median()),
                           "last": str(vsub["day"].max()),
                           "variants": variants})
        models.sort(key=lambda m: -m["n"])
        makes.append({"name": str(mk), "n": int(len(msub)),
                      "vehicles": int(msub["key"].nunique()),
                      "models": models})
    makes.sort(key=lambda m: -m["n"])
    return {"ok": True, "makes": makes}


def detail_payload(db_path: str | Path = DEFAULT_DB, make: str | None = None,
                   model: str | None = None, variant: str | None = None,
                   horizon: int = DEFAULT_HORIZON, variants=None,
                   year_min=None, year_max=None, price_min=None,
                   price_max=None, keyword=None) -> dict:
    """Everything about one selection: headline stats, per-capture-day trend
    and supply, mileage fit, forecast, the individual vehicles behind the
    numbers, and which of them changed price.

    `variants` pools SEVERAL trims of one model into one set — "how do all
    the fast Corvettes look together" is a different question from any one
    trim, and answering it by eye across three separate page loads is how
    people mis-remember a median. `variant` (singular) is kept so existing
    links, the car card and the tests all still work; it is folded into the
    list.

    The four filters narrow whatever that selection is. They run AFTER
    selection and BEFORE any statistic, so every number on the page — median,
    trend, fit, forecast, bell curve — describes the filtered set and not a
    filtered view of unfiltered stats.
    """
    from . import analyze, forecast

    df = _frame(db_path)
    picked = [v for v in ([variant] if variant else []) + list(variants or [])
              if v and str(v).strip()]
    picked = list(dict.fromkeys(picked))          # de-dupe, keep order
    if len(picked) == 1:
        label = " ".join(x for x in (make, model, picked[0]) if x)
    elif picked:
        label = f"{make or ''} {model or ''}".strip() + f" — {len(picked)} trims"
    else:
        label = " ".join(x for x in (make, model) if x) or "everything"
    if df.empty:
        return {"ok": True, "empty": True, "label": label,
                "reason": "the database is empty - import something first"}

    from .starred import keys as _star_keys
    STARS = _star_keys(db_path)
    NOT_DUP = dupes.dismissed_sets(db_path)
    sub = _select_multi(df, make, model, picked)
    unfiltered_n = int(len(sub))
    # kept whole: turnover needs the pre-filter selection to tell a car that
    # left the market from one that merely left the filter (turnover.py)
    unfiltered_sub = sub
    sub, applied = _apply_filters(sub, year_min, year_max, price_min,
                                  price_max, keyword)
    if sub.empty:
        return {"ok": True, "empty": True, "label": label,
                "filters": applied, "unfiltered": unfiltered_n,
                "reason": ("no listings match that selection"
                           if not applied else
                           f"the selection holds {unfiltered_n} sighting(s), "
                           f"but none of them match the filters")}

    priced = sub.dropna(subset=["price_aud"])
    level = ("trims" if len(picked) > 1 else
             "trim" if picked else ("line" if model else "make"))

    # ---- headline (whole selection, every sighting)
    head = {
        "label": label, "level": level,
        "observations": int(len(sub)),
        "vehicles": int(sub["key"].nunique()),
        "days": int(sub["day"].nunique()),
        "first_day": str(sub["day"].min()), "last_day": str(sub["day"].max()),
        "median": _num(priced["price_aud"].median()),
        "mean": _num(priced["price_aud"].mean()),
        "q25": _num(priced["price_aud"].quantile(0.25)),
        "q75": _num(priced["price_aud"].quantile(0.75)),
        "lo": _num(priced["price_aud"].min()),
        "hi": _num(priced["price_aud"].max()),
        "median_odo": _num(priced["odometer_km"].median()),
        "pct_no_odo": int(round(sub["odometer_km"].isna().mean() * 100)),
        "full": int((sub["detail_level"] == "full").sum()),
        "brief": int((sub["detail_level"] == "brief").sum()),
    }

    # ---- who is selling / where / from which source, each WITH its median.
    # The workbook's RichStats sheet splits dealer vs private this way; a
    # count alone cannot tell you dealers ask more, which is the actual
    # question. Sorted by n so the page never has to.
    def split(col: str, cast=str) -> list:
        out = []
        for name, g in priced.groupby(priced[col].fillna("(unknown)")):
            out.append({"name": cast(name), "n": int(len(g)),
                        "vehicles": int(g["key"].nunique()),
                        "median": _num(g["price_aud"].median()),
                        "lo": _num(g["price_aud"].min()),
                        "hi": _num(g["price_aud"].max()),
                        "odo": _num(g["odometer_km"].median())})
        out.sort(key=lambda r: -r["n"])
        return out

    splits = {"seller": split("seller_type"), "state": split("state"),
              "source": split("source"),
              "year": sorted(split("year", lambda v: str(int(float(v)))
                                   if str(v) != "(unknown)" else "(unknown)"),
                             key=lambda r: r["name"])}

    # ---- by capture day: price trend AND supply (the workbook's Trend and
    # Supply sheets are these same two series)
    byday = []
    for day, g in priced.groupby("day"):
        byday.append({
            "d": str(day), "n": int(len(g)),
            "vehicles": int(g["key"].nunique()),
            "median": _num(g["price_aud"].median()),
            "q25": _num(g["price_aud"].quantile(0.25)),
            "q75": _num(g["price_aud"].quantile(0.75)),
            "lo": _num(g["price_aud"].min()), "hi": _num(g["price_aud"].max()),
        })
    byday.sort(key=lambda r: r["d"])

    # ---- what happened BETWEEN the captures: which of those cars left view,
    # which are new, how fast this selection clears, and how that compares to
    # its sibling trims. The unfiltered selection rides along so a car that
    # only left the FILTER is not reported as having left the market.
    from . import turnover as _turn
    churn = _turn.turnover(sub, unfiltered_sub, label=label)
    # the per-day composition is drawn on the supply chart, so each day needs
    # the cars the chart is already counting beside the trackable ones
    per_day = {r["d"]: r for r in churn.get("daily", [])}
    for b in byday:
        c = per_day.get(b["d"])
        if c:
            b.update({"cohort": c["n"], "carry": c["carry"], "new": c["new"],
                      "exits": c["exits"], "rated": c["rated"],
                      "break_before": c["break_before"], "inspan": c["inspan"]})
        else:
            b.update({"cohort": 0, "carry": None, "new": None, "exits": 0,
                      "rated": False, "break_before": False, "inspan": False})

    # ---- supply summary: the Supply sheet's top block. "now" is the newest
    # capture, not a live count - a smaller number can mean fewer cars OR a
    # smaller capture, which is why the capture count sits beside it.
    counts = [b["vehicles"] for b in byday]
    supply = {
        "captures": len(byday),
        "now": counts[-1] if counts else None,
        "avg": (None if not counts else round(sum(counts) / len(counts), 1)),
        "most": max(counts) if counts else None,
        "fewest": min(counts) if counts else None,
        "change": (counts[-1] - counts[-2]) if len(counts) > 1 else None,
    }

    # ---- price vs odometer, the RichStats scatter. Sent as points so the
    # page can draw it and the fitted line together; capped because 2,000
    # circles in an SVG is slower to draw than it is useful to read.
    with_odo = priced.dropna(subset=["odometer_km"])
    with_odo = with_odo[with_odo["odometer_km"] > 0]
    # `key` rides along so clicking a point can name the car it is - the
    # natural "what is that outlier bottom-left?" gesture
    scatter = [{"odo": _num(r["odometer_km"]), "price": _num(r["price_aud"]),
                "year": None if pd.isna(r["year"]) else int(r["year"]),
                "variant": None if pd.isna(r["variant"]) else str(r["variant"]),
                "key": str(r["key"]), "d": str(r["day"])}
               for _, r in with_odo.tail(MAX_SCATTER).iterrows()]

    # ---- monthly buckets, mileage fit, forecast: identical call sequence to
    # report.py::_segment_report, so these match `vpa report` exactly
    prepped = analyze.prep(sub, freq="MS")
    stats = analyze.bucket_stats(prepped)
    mfit = analyze.fit_mileage(prepped)
    caveats = list(analyze.data_caveats(prepped, stats))
    if mfit.get("usable"):
        adj = analyze.apply_mileage_adjustment(prepped, mfit)
        adj_stats = analyze.bucket_stats(adj, price_col="price_adj")
        caveats.append(
            f"prices mileage-adjusted to {mfit['median_odo']:,.0f} km "
            f"({mfit['pct_per_10k_km']:+.1f}% per 10,000 km, n={mfit['n']})")
    else:
        adj_stats = None
    fc = forecast.forecast_segment(prepped, stats, horizon=horizon,
                                   extra_caveats=caveats)

    bymonth = [{"d": pd.Timestamp(r["bucket"]).strftime("%Y-%m"),
                "n": int(r["n"]), "median": _num(r["median"]),
                "q25": _num(r["q25"]), "q75": _num(r["q75"])}
               for _, r in stats.iterrows()]

    pts = fc.get("points")
    fcast = {
        "method": fc["method"], "verdict": fc.get("verdict", ""),
        "direction": fc.get("direction", "unknown"),
        "level": _num(fc.get("level")),
        "horizon_months": fc["horizon_months"],
        "interval_level": fc["interval_level"],
        "n_total": fc["n_total"], "n_buckets": fc["n_buckets"],
        "points": ([] if pts is None else
                   [{"d": pd.Timestamp(r["bucket"]).strftime("%Y-%m"),
                     "v": _num(r["forecast"]), "lo": _num(r["lo"]),
                     "hi": _num(r["hi"])} for _, r in pts.iterrows()]),
    }
    # `median_price` is the median of the rows the FIT used, not the headline
    # median: the fit runs on prepped rows (one per vehicle per month) while
    # the headline counts every sighting. The drawn curve has to pass through
    # the fit's own centre or it would not be the model being described.
    fit_rows = prepped.dropna(subset=["price_aud", "odometer_km"])
    mileage = {"usable": bool(mfit.get("usable")),
               "reason": mfit.get("reason", ""),
               "pct_per_10k_km": _num(mfit.get("pct_per_10k_km")),
               "median_odo": _num(mfit.get("median_odo")),
               "median_price": _num(fit_rows["price_aud"].median()),
               "n": int(mfit.get("n", 0))}

    # ---- the price distribution, the bell curve fitted to it, and every
    # car's distance from the middle.
    #
    # ONE price per car (its latest sighting), the same pool the verdict card
    # ranks against - so a car sitting unsold across seven captures appears
    # once in the tail instead of seven times, and the tab cannot disagree
    # with the card beside it. This is NOT the every-sighting basis the
    # capture-day chart uses; the tab says so on screen.
    pool = analyze.latest_per_vehicle(prepped)
    live, live_day = analyze.live_keys(sub)
    pool_prices = pool["price_aud"].dropna()

    hist = None
    if len(pool_prices) >= 4 and pool_prices.max() > pool_prices.min():
        import numpy as np
        bins = int(min(24, max(6, round(len(pool_prices) ** 0.5))))
        counts_, edges = np.histogram(pool_prices.to_numpy(dtype=float), bins=bins)
        hist = {"bins": [{"lo": _num(edges[i]), "hi": _num(edges[i + 1]),
                          "n": int(counts_[i])} for i in range(len(counts_))],
                "median": _num(pool_prices.median()),
                "q25": _num(pool_prices.quantile(0.25)),
                "q75": _num(pool_prices.quantile(0.75)),
                "n": int(len(pool_prices))}

    curve = analyze.price_curve(pool_prices)
    # a second curve over mileage-adjusted prices, where the fit allows it:
    # a car can look like a bargain purely because it has done 300,000 km,
    # and the two z-scores side by side are what separate those cases
    adj_curve, adj_lookup = None, {}
    if mfit.get("usable"):
        adj_pool = analyze.apply_mileage_adjustment(pool, mfit)
        adj_curve = analyze.price_curve(adj_pool["price_adj"].dropna())
        if adj_curve.get("usable"):
            adj_lookup = {str(r["key"]): analyze.z_of(r["price_adj"], adj_curve)
                          for _, r in adj_pool.iterrows()}

    standouts = []
    if curve.get("usable"):
        # groups you marked "different cars" on the Duplicates page are not
        # tagged - same rule and same code as the page itself
        dup_pairs, _ = dupes.dup_sigs(pool, NOT_DUP)
        for _, r in pool.iterrows():
            k = str(r["key"])
            standouts.append({
                "key": k, "z": analyze.z_of(r["price_aud"], curve),
                "z_adj": adj_lookup.get(k),
                "price": _num(r["price_aud"]),
                "odometer_km": _num(r["odometer_km"]),
                "year": None if pd.isna(r["year"]) else int(r["year"]),
                "variant": None if pd.isna(r["variant"]) else str(r["variant"]),
                "state": None if pd.isna(r["state"]) else str(r["state"]),
                "seller_type": (None if pd.isna(r["seller_type"])
                                else str(r["seller_type"])),
                "day": str(r["day"]), "live": k in live,
                "url": links.ad_url(r["url"], r.get("source")),
                "title": None if pd.isna(r["raw_title"]) else str(r["raw_title"]),
                "dup": (r["price_aud"], r["odometer_km"]) in dup_pairs,
            })
        standouts.sort(key=lambda v: (v["z"] is None, v["z"]))
    # How much of this pool is the same car twice. On the real data a trim
    # can be a third duplicates (XR8 FG X: 29 surplus identities in 74 cars),
    # because a paste capture and a saved-HTML capture of the same cars
    # produce id-less and id-bearing identities that never collide. Stated
    # once, up front - tagging 58 of 74 rows and saying nothing else would be
    # a wall of amber that explains nothing.
    dup_extra = 0
    if len(pool):
        _, dup_extra = dupes.dup_sigs(pool, NOT_DUP)
    distribution = {
        "basis": "one price per car (its latest sighting)",
        "live_day": live_day, "live_n": int(len(live)),
        "pool_n": int(len(pool_prices)),
        "total": len(standouts), "dup_extra": dup_extra,
    }

    # ---- the individual vehicles behind those numbers
    # Keys carrying two prices on one day are two cars merged into one
    # identity; their "change" is an artefact, so they are shown in the table
    # (flagged) but never counted as a price movement.
    collided = analyze.collided_keys(sub)
    head["collisions"] = len(collided)

    # One stable sort and positional lookups. This used to re-sort and slice
    # a DataFrame per car (a sort, an iloc and a Series per vehicle), which
    # on a model line of 1,200 cars was a large share of every page load.
    vehicles, movers = [], []
    srt = sub.sort_values("capture_date", kind="stable")
    recs = srt.to_dict("records")
    groups = srt.groupby("key").indices          # key -> row positions, in date order
    for key in sorted(groups):                   # groupby's own key order
        rows = [recs[i] for i in groups[key]]
        prices = [r["price_aud"] for r in rows if pd.notna(r["price_aud"])]
        last = rows[-1]
        first_price = _num(prices[0]) if prices else None
        last_price = _num(prices[-1]) if prices else None
        clash = key in collided
        moved = (first_price is not None and last_price is not None
                 and len(set(prices)) > 1 and not clash)
        v = {
            "key": str(key),
            "year": None if pd.isna(last["year"]) else int(last["year"]),
            "make": str(last["make"]), "model": str(last["model"]),
            "variant": None if pd.isna(last["variant"]) else str(last["variant"]),
            "price": last_price, "first_price": first_price,
            "odometer_km": _num(last["odometer_km"]),
            "state": None if pd.isna(last["state"]) else str(last["state"]),
            "seller_type": None if pd.isna(last["seller_type"]) else str(last["seller_type"]),
            "source": str(last["source"]),
            "url": links.ad_url(last["url"], last.get("source")),
            "title": None if pd.isna(last["raw_title"]) else str(last["raw_title"]),
            "listing_id": None if pd.isna(last["listing_id"]) else str(last["listing_id"]),
            "detail": str(last["detail_level"]),
            "sightings": len(rows),
            "first_day": str(rows[0]["day"]), "last_day": str(last["day"]),
            "moved": moved, "clash": clash,
            "starred": str(last["key"]) in STARS,
            "change": (round((last_price - first_price) / first_price * 100, 1)
                       if moved and first_price else None),
        }
        vehicles.append(v)
        if moved:
            movers.append({**{k: v[k] for k in
                              ("key", "year", "variant", "first_price", "price",
                               "change", "first_day", "last_day", "url", "title")},
                           "steps": [{"d": str(r["day"]), "v": _num(r["price_aud"])}
                                     for r in rows if pd.notna(r["price_aud"])]})

    # most recently seen first; that is the useful order when shopping
    vehicles.sort(key=lambda v: (v["last_day"], v["price"] or 0), reverse=True)
    movers.sort(key=lambda m: -abs(m["change"] or 0))

    # ---- sibling trims (the workbook's Models sheet: spot near-duplicates
    # and compare trims without re-picking)
    siblings = []
    if make and model:
        fam = _select(df, make, model, None).dropna(subset=["price_aud"])
        if not fam.empty:
            # One grouped aggregation for every trim at once. This was a loop
            # of ten small filters per trim - on a 154-trim model line, about
            # a second of every page load, including when only one trim was
            # being looked at.
            f2 = fam.assign(_v=fam["variant"].fillna("(no trim)"),
                            _full=(fam["detail_level"] == "full"))
            agg = f2.groupby("_v").agg(
                n=("key", "size"), vehicles=("key", "nunique"),
                days=("day", "nunique"), median=("price_aud", "median"),
                lo=("price_aud", "min"), hi=("price_aud", "max"),
                odo=("odometer_km", "median"), full=("_full", "sum"),
                last=("day", "max"))
            # the Supply sheet's per-variant block: how many were up at THAT
            # trim's own latest capture, which is not always the selection's
            # latest capture
            at_last = f2[f2["day"].astype(str) == f2["_v"].map(agg["last"]).astype(str)]
            now = at_last.groupby("_v").agg(now=("key", "nunique"),
                                            now_median=("price_aud", "median"))
            chosen = set(picked)
            for va, r in agg.iterrows():
                # same fields the picker carries, so the page can order chips
                # and the dropdown with one comparator
                has_now = va in now.index
                siblings.append({"variant": str(va), "n": int(r["n"]),
                                 "vehicles": int(r["vehicles"]),
                                 "days": int(r["days"]),
                                 "median": _num(r["median"]),
                                 "lo": _num(r["lo"]), "hi": _num(r["hi"]),
                                 "odo": _num(r["odo"]),
                                 "full": int(r["full"]),
                                 "last": str(r["last"]),
                                 "now": int(now.at[va, "now"]) if has_now else 0,
                                 "now_median": (_num(now.at[va, "now_median"])
                                                if has_now else None),
                                 "selected": str(va) in chosen})
        siblings.sort(key=lambda s: -s["n"])

    # ---- the same churn measurement for every trim of this model line, so
    # "does this one clear faster than its siblings" is one look rather than
    # six page loads. Each trim is measured on its own span; see by_variant.
    trim_churn = {"rows": [], "pooled": None, "usable": False}
    if make and model:
        fam_all = _select(df, make, model, None)
        fam_filtered, _ = _apply_filters(fam_all, year_min, year_max,
                                         price_min, price_max, keyword)
        trim_churn = _turn.by_variant(fam_filtered, selected=picked,
                                      full_line=fam_all)

    return {"ok": True, "empty": False, "head": head, "byday": byday,
            "picked": picked, "filters": applied, "starred_total": len(STARS),
            "unfiltered": unfiltered_n, "kept": int(len(sub)),
            "bymonth": bymonth, "forecast": fcast, "mileage": mileage,
            "caveats": caveats, "supply": supply, "splits": splits,
            "scatter": scatter, "scatter_total": int(len(with_odo)),
            "hist": hist, "curve": curve, "adj_curve": adj_curve,
            "standouts": standouts[:MAX_STANDOUTS],
            "distribution": distribution,
            "adjusted": (None if adj_stats is None else
                         [{"d": pd.Timestamp(r["bucket"]).strftime("%Y-%m"),
                           "median": _num(r["median"])}
                          for _, r in adj_stats.iterrows()]),
            "vehicles": vehicles[:MAX_VEHICLES],
            "vehicle_total": len(vehicles),
            "movers": movers[:40], "mover_total": len(movers),
            "siblings": siblings,
            "churn": churn, "trim_churn": trim_churn}


# ------------------------------------------------------- one car vs its trim
def _subject_from_row(row) -> dict:
    """The car being judged, taken from a stored row."""
    return {"key": str(row["key"]), "stored": True,
            "price": _num(row["price_aud"]),
            "odometer_km": _num(row["odometer_km"]),
            "year": None if pd.isna(row["year"]) else int(row["year"]),
            "state": None if pd.isna(row["state"]) else str(row["state"]),
            "seller_type": (None if pd.isna(row["seller_type"])
                            else str(row["seller_type"])),
            "variant": None if pd.isna(row["variant"]) else str(row["variant"]),
            "title": None if pd.isna(row["raw_title"]) else str(row["raw_title"]),
            "url": links.ad_url(row["url"], row.get("source")),
            "source": str(row["source"]),
            "day": str(row["day"])}


def _subject_typed(price, odometer, year, seller, state) -> dict:
    """A car the user has SEEN but not imported. Normalised by the same
    functions the importer uses, so '$18,990', '98k' and 'New South Wales'
    mean here exactly what they would mean in the database - and the page
    echoes these back, so a misreading is visible instead of silent."""
    from .schema import (norm_seller, norm_state, norm_year, parse_odometer,
                         parse_price)
    p, note = parse_price(price)
    return {"key": None, "stored": False, "price": _num(p),
            "price_note": note,
            "odometer_km": _num(parse_odometer(odometer)),
            "year": norm_year(year), "state": norm_state(state),
            "seller_type": norm_seller(seller),
            "variant": None, "title": None, "url": None,
            "source": None, "day": None}


def _rank_in(peer_prices: list, value: float) -> dict:
    """Where `value` sits among the peers, and how confidently we may say so.

    Below CAR_MIN_PEERS there is no answer worth printing. Between that and
    CAR_PERCENTILE_MIN there is a count ("3rd cheapest of 11") but no
    percentile, because a percentile over eleven cars implies a precision
    eleven cars cannot carry."""
    n = len(peer_prices)
    if value is None or n < CAR_MIN_PEERS:
        return {"n": n, "usable": False,
                "reason": (f"only {n} other car(s) of this trim have a price - "
                           f"{CAR_MIN_PEERS} are needed before a ranking means "
                           f"anything")}
    cheaper = sum(1 for p in peer_prices if p < value)
    out = {"n": n, "usable": True, "rank": cheaper + 1,
           "dearer_than": cheaper, "as_count": n < CAR_PERCENTILE_MIN}
    if n >= CAR_PERCENTILE_MIN:
        out["percentile"] = round(cheaper / n * 100, 1)
    return out


def _pool_stats(prices) -> dict:
    s = pd.Series(prices, dtype="float64")
    return {"median": _num(s.median()), "q25": _num(s.quantile(0.25)),
            "q75": _num(s.quantile(0.75)),
            "lo": _num(s.min()), "hi": _num(s.max())}


def _split_median(peers: pd.DataFrame, col: str, value, min_n: int) -> dict:
    """Median of the peers that match the subject on one attribute, or a
    stated refusal. A private Victorian ad judged against a national pooled
    median looks like a bargain purely because of mix, so this is the panel
    that stops the card being confidently wrong."""
    if value is None:
        return {"value": None, "n": 0, "median": None,
                "reason": "not recorded for this car"}
    matched = peers[peers[col] == value]
    n = int(len(matched))
    if n < min_n:
        return {"value": str(value), "n": n, "median": None,
                "reason": f"only {n} other {value} listing(s) - too few to compare"}
    return {"value": str(value), "n": n,
            "median": _num(matched["price_aud"].median()), "reason": ""}


def _timeline(sub: pd.DataFrame, key: str, db_path) -> dict:
    """Where this one car sat, capture day by capture day, and where every
    sighting of it came from.

    Two questions the rest of the card cannot answer:

      * *"was this car really in my earlier captures?"* Each sighting names
        the import that recorded it — timestamp, source, and how many rows
        that import carried — so "seen on three capture days" can be CHECKED
        against the ingest log instead of believed. A car pasted in among
        eleven others is easy to have never consciously looked at, and that
        reads identically to a wrongly merged identity until you can see the
        two imports were genuinely separate.
      * *"has it moved against its trim?"* The trim's own spread is drawn on
        the same days, so an unchanged asking price in a falling market shows
        up as the relative rise it actually is.

    The daily peer stats EXCLUDE the subject, matching every other panel: a
    car never counts in the pool it is being ranked against. Days are the
    trim's capture days, not the car's, so a gap in its line is visible as a
    day when the trim was captured and this car was not in it.
    """
    own_days = sub[sub["key"] == key]
    if own_days.empty:
        return {}

    # one row per car per day (a re-paste on the same day is not a new price)
    one = (sub.dropna(subset=["price_aud"])
              .sort_values("capture_date")
              .groupby(["day", "key"], observed=True).tail(1))
    mine = {str(r["day"]): float(r["price_aud"]) for _, r in
            own_days.dropna(subset=["price_aud"]).iterrows()}

    byday = []
    for d, g in one.groupby("day", observed=True):
        peers = g[g["key"] != key]["price_aud"].astype(float)
        row = {"d": str(d), "n": int(len(peers)), "mine": mine.get(str(d))}
        if len(peers):
            row.update(median=_num(peers.median()), q25=_num(peers.quantile(0.25)),
                       q75=_num(peers.quantile(0.75)),
                       lo=_num(peers.min()), hi=_num(peers.max()))
        if row["mine"] is not None and len(peers) >= CAR_MIN_PEERS:
            row["rank"] = int((peers < row["mine"]).sum()) + 1
            row["of"] = int(len(peers)) + 1
        byday.append(row)
    byday.sort(key=lambda r: r["d"])

    # ---- provenance, straight from the ingest log
    with Store(db_path) as store:
        raw_rows = store.sightings(key)
    rows, prev = [], None
    for r in raw_rows:
        price, odo = r["price_aud"], r["odometer_km"]
        item = {"d": str(r["capture_date"]), "price": _num(price),
                "odometer_km": _num(odo), "note": r["price_note"],
                "imported_at": r["imported_at"], "adapter": r["adapter"],
                "source_file": r["source_file"], "batch": r["batch_id"],
                "batch_rows": r["rows_parsed"]}
        if prev is not None:
            if price is not None and prev["price_aud"] is not None:
                item["d_price"] = _num(price - prev["price_aud"])
            if odo is not None and prev["odometer_km"] is not None:
                item["d_odo"] = _num(odo - prev["odometer_km"])
        rows.append(item)
        prev = r

    batches = {r["batch"] for r in rows if r["batch"] is not None}
    # An identical price AND odometer on every sighting means the ad was
    # simply re-captured unchanged - worth saying, because it is the pattern
    # that makes a real repeat sighting look like a duplicated row.
    unchanged = (len(rows) > 1
                 and len({r["price"] for r in rows}) == 1
                 and len({r["odometer_km"] for r in rows}) == 1)
    # `byday`, NOT `days`: the caller merges this into `history`, which
    # already uses `days` for the count of capture days this car appears on.
    return {"byday": byday, "rows": rows, "imports": len(batches),
            "unchanged": unchanged,
            "orphans": sum(1 for r in rows if r["batch"] is None)}


def car_payload(db_path: str | Path = DEFAULT_DB, make: str | None = None,
                model: str | None = None, variant: str | None = None,
                key: str | None = None, price=None, odometer=None,
                year=None, seller=None, state=None) -> dict:
    """Judge ONE car against the others of its trim. Read-only.

    Two entry paths, ONE implementation, deliberately: `key` names a car
    already in the database, while price/odometer/year/seller/state describe
    one the user has only seen. Both are judged by this same function, so a
    typed car and a stored car can never be graded by two different rules.
    Nothing is written and nothing typed is stored - the caller keeps it in
    the URL.

    The peer set is `latest_per_vehicle(prep(...))`: one price per physical
    car, with the subject's own rows removed. A car cannot grade itself, and
    a car that has sat unsold across seven captures cannot vote seven times.
    """
    from . import analyze

    df = _frame(db_path)
    label = " ".join(x for x in (make, model, variant) if x) or "everything"
    base = {"ok": True, "label": label, "gates": {
        "min_peers": CAR_MIN_PEERS, "percentile_min": CAR_PERCENTILE_MIN}}
    if df.empty:
        return {**base, "usable": False,
                "reason": "the database is empty - import something first"}
    # Refuse above trim level rather than pooling different cars: a rank
    # against every Falcon trim at once would be confident and meaningless.
    if not (make and model and variant):
        return {**base, "usable": False, "level": "too broad",
                "reason": "pick a trim first - at make or model level the "
                          "peer set would pool cars that are not comparable"}

    sub = _select(df, make, model, variant)
    if sub.empty:
        return {**base, "usable": False,
                "reason": "no listings match that selection"}

    prepped = analyze.prep(sub, freq="MS")
    pool = analyze.latest_per_vehicle(prepped)
    if pool.empty:
        return {**base, "usable": False,
                "reason": "no priced listings in this trim"}

    # ---- the subject, and the peers that exclude it
    if key:
        own = pool[pool["key"] == key]
        if own.empty:
            return {"ok": False, "error": "that car is not in this selection"}
        subject = _subject_from_row(own.iloc[0])
        peers = pool[pool["key"] != key]
        fit_rows = prepped[prepped["key"] != key]
    else:
        subject = _subject_typed(price, odometer, year, seller, state)
        if subject["price"] is None:
            return {"ok": False, "error": "a price is needed to place a car"}
        peers = pool
        fit_rows = prepped
    subject["label"] = label

    peer_prices = [float(p) for p in peers["price_aud"].dropna()]
    raw = _rank_in(peer_prices, subject["price"])
    pool_stats = _pool_stats(peer_prices)

    unavailable, notes = [], []

    # ---- odometer percentile: 20th on price at the 90th on odometer is not
    # a bargain, and this is the misreading the card most invites
    odo_pct = None
    peer_odo = peers["odometer_km"].dropna()
    if subject["odometer_km"] is not None and len(peer_odo) >= CAR_MIN_PEERS:
        odo_pct = round(float((peer_odo < subject["odometer_km"]).mean() * 100), 1)
    elif subject["odometer_km"] is None:
        unavailable.append("no odometer on this car, so it cannot be placed "
                           "on kilometres or mileage-adjusted")

    # ---- the mileage fit. The subject is NEVER in fit_mileage's input, so
    # it cannot bend the curve that judges it. The model line's fit is NEVER
    # substituted when this trim's refuses: pooling trims of different value
    # makes the slope pick up mix rather than wear.
    fit = analyze.fit_mileage(fit_rows)
    adjusted, expected = None, None
    fit_info = {"usable": bool(fit.get("usable")), "reason": fit.get("reason", ""),
                "n": int(fit.get("n", 0)),
                "pct_per_10k_km": _num(fit.get("pct_per_10k_km")),
                "median_odo": _num(fit.get("median_odo"))}

    if fit.get("usable") and subject["odometer_km"] is not None:
        used = fit_rows.dropna(subset=["price_aud", "odometer_km"])
        lo_q = float(used["odometer_km"].quantile(CAR_FIT_RANGE[0]))
        hi_q = float(used["odometer_km"].quantile(CAR_FIT_RANGE[1]))
        fit_info["covers"] = [_num(lo_q), _num(hi_q)]
        if not (lo_q <= subject["odometer_km"] <= hi_q):
            fit_info["outside"] = True
            unavailable.append(
                f"at {subject['odometer_km']:,.0f} km this car sits outside the "
                f"{lo_q:,.0f}-{hi_q:,.0f} km the fit actually covers, so no "
                f"mileage-adjusted figure is shown")
        else:
            # Adjust the subject through the SAME function the peers go
            # through, by joining it as one sentinel row. apply_mileage_
            # adjustment takes the fit as a parameter, so adding the row
            # provably cannot move the slope.
            sentinel = peers.iloc[:1].copy()
            sentinel["key"] = "__subject__"
            sentinel["price_aud"] = subject["price"]
            sentinel["odometer_km"] = subject["odometer_km"]
            joined = pd.concat([peers, sentinel], ignore_index=True)
            adj = analyze.apply_mileage_adjustment(joined, fit)
            subj_adj = float(adj[adj["key"] == "__subject__"]["price_adj"].iloc[0])
            peer_adj = [float(v) for v in
                        adj[adj["key"] != "__subject__"]["price_adj"].dropna()]
            adjusted = {"price": _num(subj_adj),
                        "rank": _rank_in(peer_adj, subj_adj),
                        **{f"pool_{k}": v for k, v in _pool_stats(peer_adj).items()},
                        "at_odo": _num(fit["median_odo"])}

            # expected-at-this-odometer, anchored on the median of the rows
            # the fit itself used - the same anchor mileage.median_price uses
            import numpy as np
            base_price = float(used["price_aud"].median())
            slope = float(fit["slope_per_km"])
            exp_price = base_price * np.exp(
                slope * (subject["odometer_km"] - float(fit["median_odo"])))
            resid = np.log(subject["price"]) - np.log(exp_price)
            peer_resid = (np.log(used["price_aud"].to_numpy(dtype=float))
                          - np.log(base_price)
                          - slope * (used["odometer_km"].to_numpy(dtype=float)
                                     - float(fit["median_odo"])))
            expected = {
                "price": _num(round(exp_price / 100) * 100),
                "gap": _num(round((subject["price"] - exp_price) / 100) * 100),
                "gap_pct": round(float(subject["price"] / exp_price - 1) * 100, 1),
                "n": int(len(used)),
                "above": int((peer_resid > resid).sum()),
                "of": int(len(peer_resid)),
            }
    elif not fit.get("usable"):
        unavailable.append(f"no mileage adjustment: {fit.get('reason', 'refused')}")

    # ---- the nearest real ads. Ranked on |ln(odo / subject odo)| because
    # 20,000 km means something different at 60k than at 260k - the same
    # log-linear view the adjustment takes. No model needed, which is why
    # this is the fallback whenever the fit refuses.
    cmp_rows = peers.copy()
    if subject["odometer_km"]:
        import numpy as np
        cand = cmp_rows.dropna(subset=["odometer_km"])
        cand = cand[cand["odometer_km"] > 0]
        cand = cand.assign(_d=(np.log(cand["odometer_km"].astype(float)
                                      / subject["odometer_km"])).abs())
        basis = "odometer"
    elif subject["year"]:
        cand = cmp_rows.dropna(subset=["year"])
        cand = cand.assign(_d=(cand["year"].astype(float) - subject["year"]).abs())
        basis = "year"
    else:
        cand = cmp_rows.assign(_d=0.0)
        basis = "none"
    cand = cand.sort_values(["_d", "price_aud"]).head(CAR_COMPARABLES)
    # Two identities of one car otherwise read as corroboration at the exact
    # moment confidence is being formed. Flagged, never merged.
    #
    # Matched on price AND odometer only, NOT on capture day: this pool holds
    # each identity's own latest sighting, so the two copies of one car
    # usually carry DIFFERENT last-seen days (that is exactly how the id-less
    # copy arises - see NOTES bug 11). Requiring the same day missed them.
    dup_set, dup_total = dupes.dup_sigs(peers, dupes.dismissed_sets(db_path))
    comparables = []
    for _, r in cand.iterrows():
        sig = (r["price_aud"], r["odometer_km"])
        comparables.append({
            "key": str(r["key"]),
            "year": None if pd.isna(r["year"]) else int(r["year"]),
            "price": _num(r["price_aud"]),
            "odometer_km": _num(r["odometer_km"]),
            "state": None if pd.isna(r["state"]) else str(r["state"]),
            "seller_type": (None if pd.isna(r["seller_type"])
                            else str(r["seller_type"])),
            "day": str(r["day"]),
            "url": links.ad_url(r["url"], r.get("source")),
            "title": None if pd.isna(r["raw_title"]) else str(r["raw_title"]),
            "dup": sig in dup_set,
        })
    band = {"basis": basis, "n": len(comparables), "dup_in_pool": dup_total,
            "median": _num(pd.Series([c["price"] for c in comparables],
                                     dtype="float64").median())
            if comparables else None}
    if band["median"] and subject["price"]:
        band["vs"] = round(subject["price"] / band["median"] * 100 - 100, 1)

    # ---- like-for-like: the two largest confounds here are who is selling
    # and where, and the trim pools several model years with no year term in
    # the fit
    splits = {
        "seller": _split_median(peers, "seller_type", subject["seller_type"],
                                CAR_SPLIT_MIN_N),
        "state": _split_median(peers, "state", subject["state"],
                               CAR_SPLIT_MIN_N),
        "year": _split_median(peers, "year", subject["year"], CAR_YEAR_MIN_N),
    }

    # ---- the same car ranked against its whole model line.
    #
    # NOT a mis-picked-trim alarm, which is what this started as: a trim that
    # is systematically cheaper or dearer than its line disagrees with the
    # line on EVERY car in it, so an alarm would fire constantly and mean
    # nothing. Measured here: a mid-pack XR6 FG MkII (49.6th percentile in
    # its trim) sits at the 23.5th across all Falcons, purely because the
    # line pools XR8s. Both numbers are true and neither is a warning.
    #
    # It is reported as plain context - "mid-pack for this trim, cheapest
    # quarter of all Falcons" - which is useful when deciding between trims
    # and is never wrong.
    line_pool = analyze.latest_per_vehicle(
        analyze.prep(_select(df, make, model, None), freq="MS"))
    if key:
        line_pool = line_pool[line_pool["key"] != key]
    line_prices = [float(p) for p in line_pool["price_aud"].dropna()]
    line_rank = _rank_in(line_prices, subject["price"])
    line_check = {"label": f"{make} {model}", "rank": line_rank,
                  "median": _num(pd.Series(line_prices, dtype="float64").median())}
    if (raw.get("percentile") is not None
            and line_rank.get("percentile") is not None):
        line_check["gap"] = round(line_rank["percentile"] - raw["percentile"], 1)
        line_check["notable"] = abs(line_check["gap"]) >= CAR_LINE_DISAGREE

    # ---- this listing's own history. Only capture days are observed, so
    # time on market is only ever a LOWER bound, and the words sold / gone /
    # still listed are never used anywhere on this card.
    history = None
    if key:
        own_rows = sub[sub["key"] == key].sort_values("capture_date")
        prices = [p for p in own_rows["price_aud"].tolist() if pd.notna(p)]
        clash = key in analyze.collided_keys(sub)
        first_day, last_day = str(own_rows["day"].iloc[0]), str(own_rows["day"].iloc[-1])
        span = (pd.Timestamp(last_day) - pd.Timestamp(first_day)).days
        history = {
            "sightings": int(len(own_rows)), "days": int(own_rows["day"].nunique()),
            "first_day": first_day, "last_day": last_day, "at_least_days": int(span),
            "first_price": _num(prices[0]) if prices else None,
            "price": _num(prices[-1]) if prices else None,
            "clash": clash,
            "steps": [{"d": str(d), "v": _num(p)}
                      for d, p in zip(own_rows["day"], own_rows["price_aud"])
                      if pd.notna(p)],
        }
        moved = (not clash and len(set(prices)) > 1 and prices[0])
        history["moved"] = bool(moved)
        history["change"] = (round((prices[-1] - prices[0]) / prices[0] * 100, 1)
                             if moved else None)
        history.update(_timeline(sub, key, db_path))

    # ---- provenance. NOTES 5.12: every panel states its basis, and this
    # card's basis (one price per car) is one the workbook cannot express.
    from datetime import datetime
    last_day = str(pool["day"].max())
    days_since = (datetime.now() - pd.Timestamp(last_day).to_pydatetime()).days
    caveats = list(analyze.data_caveats(prepped, analyze.bucket_stats(prepped)))

    return {**base, "usable": raw["usable"], "level": "trim",
            "subject": subject,
            "pool": {"n": len(peer_prices), "first": str(pool["day"].min()),
                     "last": last_day, "days_since": days_since,
                     "stale": days_since > CAR_STALE_DAYS, **pool_stats},
            "raw": raw, "adjusted": adjusted, "expected": expected,
            "fit": fit_info, "odo_percentile": odo_pct,
            "comparables": comparables, "band": band, "splits": splits,
            "line_check": line_check, "history": history,
            "caveats": caveats, "unavailable": unavailable, "notes": notes,
            "basis": ("one price per car - each car's latest sighting - so a "
                      "car seen on seven capture days counts once"),
            "footer": ("These are ASKING prices, not sale prices, and this "
                       "compares numbers only. Condition, service history, "
                       "rego and colour are not in the data.")}


# ------------------------------------------------- identify from a saved page
def identify_payload(db_path: str | Path = DEFAULT_DB, html: str = "") -> dict:
    """Work out WHICH stored car a saved single-vehicle page describes.

    Read-only, and deliberately so: this never ingests. Uploading an ad to
    ask "how does this one compare" must not quietly add a listing - `/` is
    the only page that writes, and a details page is a different capture from
    the search sweeps the dataset is built on.

    Matching runs strongest-first and reports which rule fired, because the
    three are not equally trustworthy:

      1. the site's own ad id - conclusive, it IS the identity;
      2. `Record.dedupe_key()` - the same fingerprint ingestion would use;
      3. same trim + EXACT price + EXACT odometer - the evidence rule from
         merge.py, and only when the odometer is a reading rather than a
         whole thousand.

    Nothing matched is not a failure: the car is simply not in your captures,
    and the caller can still score it as a hypothetical.
    """
    from .adapters.detailpage import is_detail_page, parse_html
    from .merge import ROUND_KM
    from .schema import make_record

    if not (html or "").strip():
        return {"ok": False, "error": "no page content"}
    if "networkId" not in html and "application/ld+json" not in html:
        return {"ok": False, "error":
                "that does not look like a saved carsales/bikesales page"}

    raw = parse_html(html)
    if not raw:
        return {"ok": False, "error":
                ("no vehicle found in that page. Save the ad itself "
                 "(Ctrl+S on the listing), not the search results - a "
                 "results page has no single car to identify.")}
    if not is_detail_page(html):
        return {"ok": False, "error":
                ("that page lists several vehicles. Use Add data for a "
                 "search-results page; this needs the ad for one car.")}

    # Record has no colour fields. Both are kept for display only, and kept
    # SEPARATE: the ad states an exterior and an interior colour and they are
    # routinely different, so collapsing them to one "colour" is how a Kinetic
    # Blue car gets reported as black.
    colour = raw.pop("colour", None)
    interior = raw.pop("interior_colour", None)
    fuel = raw.pop("fuel_type", None)
    rec = make_record(dict(raw), source="carsales")
    if rec is None:
        return {"ok": False, "error": "the page gave no usable make and model"}

    found = {
        "listing_id": rec.listing_id, "make": rec.make, "model": rec.model,
        "variant": rec.variant, "year": rec.year,
        "price": _num(rec.price_aud), "odometer_km": _num(rec.odometer_km),
        "state": rec.state, "seller_type": rec.seller_type,
        "colour": colour, "interior_colour": interior, "fuel_type": fuel,
        "url": rec.url, "title": rec.raw_title,
    }

    df = _frame(db_path)
    key = basis = None
    if not df.empty:
        if rec.listing_id:
            hit = df[df["listing_id"] == rec.listing_id]
            if not hit.empty:
                key, basis = str(hit.iloc[0]["key"]), "the site's own ad id"
        if key is None:
            hit = df[df["key"] == rec.dedupe_key()]
            if not hit.empty:
                key, basis = str(hit.iloc[0]["key"]), "an exact fingerprint match"
        if key is None and rec.price_aud and rec.odometer_km \
                and rec.odometer_km % ROUND_KM != 0:
            hit = df[(df["make"] == rec.make) & (df["model"] == rec.model)
                     & (df["variant"] == rec.variant)
                     & (df["price_aud"] == rec.price_aud)
                     & (df["odometer_km"] == rec.odometer_km)]
            if not hit.empty:
                key = str(hit.iloc[0]["key"])
                basis = (f"the same trim at exactly ${rec.price_aud:,.0f} and "
                         f"{rec.odometer_km:,.0f} km")

    out = {"ok": True, "found": found, "key": key, "basis": basis,
           "in_data": key is not None}
    if key is not None:
        own = df[df["key"] == key]
        out["stored"] = _stored_summary(own)
        # what the FILE states and the stored record does not - a details page
        # carries state, seller and colour that a search sweep never did
        gained = [f for f in ("state", "seller_type")
                  if found.get(f) and pd.isna(own.iloc[0][f])]
        out["adds"] = gained
    else:
        # is the trim itself known? decides whether it can even be ranked
        sel = _select(df, rec.make, rec.model, rec.variant)
        out["trim_known"] = bool(len(sel))
        out["line_known"] = bool(len(_select(df, rec.make, rec.model, None)))
    return out


def _stored_summary(own: pd.DataFrame) -> dict:
    return {
        "sightings": int(len(own)),
        "days": int(own["day"].nunique()),
        "first_day": str(own["day"].min()), "last_day": str(own["day"].max()),
        "price": _num(own.sort_values("capture_date").iloc[-1]["price_aud"]),
    }


# ------------------------------------------------------- identify from a link
#: what a site ad id looks like (SSE-AD-20578075, OAG-AD-..., SHRM-AD-...)
_SITE_ID = re.compile(r"\b([A-Z]{2,5}-AD-\d+)\b", re.I)


def _norm_path(u: str) -> str:
    """'https://www.carsales.com.au/cars/details/x/SSE-AD-1/?q=2' and the
    stored bare path '/cars/details/x/SSE-AD-1/' -> the same string."""
    from urllib.parse import urlsplit
    u = (u or "").strip()
    if "://" not in u and not u.startswith("/"):
        u = "https://" + u
    return urlsplit(u).path.rstrip("/").lower()


def link_payload(db_path: str | Path = DEFAULT_DB, link: str = "") -> dict:
    """Work out which stored car an advert LINK points at - without opening it.

    Nothing is fetched: the link is only read as text and matched against what
    your own captures recorded, strongest first:

      1. an ad id in the link that is a stored listing id - the site's own
         identity, so conclusive;
      2. the link's path equal to a stored advert path.

    A link carries no price, odometer or year, so there is no weaker rule to
    fall back on: an advert that is not in your captures is reported as such,
    and the saved page (which does carry them) is the way to score it.
    Read-only, like identify_payload.
    """
    link = (link or "").strip()
    if not link:
        return {"ok": False, "error": "paste an advert link first"}
    if len(link) > 2000 or any(c.isspace() for c in link):
        return {"ok": False, "error": "that does not look like a single link"}
    low = link.lower()
    if "facebook.com" in low or "fb.com" in low:
        return {"ok": False, "error":
                ("Facebook listings were captured without their links, so a "
                 "Facebook link cannot be matched. Pick the car's trim below "
                 "instead, or use Type one I've seen.")}

    df = _frame(db_path)
    ids = {str(x).upper(): x for x in df["listing_id"].dropna().unique()} \
        if not df.empty else {}
    want_path = _norm_path(link)
    segments = [s for s in re.split(r"[/?#&=]", link) if s]
    key = basis = listing = None
    # 1. any part of the link that IS a stored listing id
    for seg in reversed(segments):
        hit_id = ids.get(seg.upper())
        if hit_id is not None:
            key = str(df[df["listing_id"] == hit_id].iloc[0]["key"])
            basis, listing = "the ad id in the link", hit_id
            break
    # 2. the advert path itself
    if key is None and want_path and not df.empty:
        paths = df["url"].dropna()
        hit = paths[paths.map(_norm_path) == want_path]
        if not hit.empty:
            key = str(df.loc[hit.index[0], "key"])
            basis = "the advert's address"
    m = _SITE_ID.search(link)
    if key is None:
        if not m and not want_path.startswith(("/cars/", "/bikes/")) \
                and "carsales" not in low and "bikesales" not in low:
            return {"ok": False, "error":
                    ("no ad id in that link - paste the address of the advert "
                     "itself (it contains something like SSE-AD-20578075)")}
        return {"ok": True, "in_data": False, "key": None,
                "found": {"listing_id": m.group(1).upper() if m else None,
                          "url": links.ad_url(link, "carsales")}}

    own = df[df["key"] == key].sort_values("capture_date")
    last = own.iloc[-1]
    found = {k: (None if pd.isna(last[k]) else last[k]) for k in
             ("listing_id", "make", "model", "variant", "state", "seller_type")}
    found.update({"year": None if pd.isna(last["year"]) else int(last["year"]),
                  "price": _num(last["price_aud"]),
                  "odometer_km": _num(last["odometer_km"]),
                  "url": links.ad_url(last["url"], last["source"])
                  if isinstance(last["url"], str) else None,
                  "title": None if pd.isna(last["raw_title"]) else last["raw_title"]})
    return {"ok": True, "in_data": True, "key": key, "basis": basis,
            "found": found, "stored": _stored_summary(own), "adds": []}


# -------------------------------------------------------------------- page
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>vpa explore</title>
<style>
__CHROME_CSS__
 select{min-width:150px;max-width:280px}
 .filters{margin-top:12px;padding-top:12px;border-top:1px solid var(--line-2)}
 .w6{width:104px}.w8{width:132px}
 .fbadge{display:inline-block;background:var(--accent-soft);border:1px solid var(--accent-line);
   color:var(--accent-2);border-radius:11px;padding:0 9px;font-size:11.5px;line-height:19px;
   margin-right:5px}
 /* the trim picker: a filter box and a bounded set of chips, not a wall */
 .chipbar{display:flex;gap:10px;align-items:center;margin:14px 0 8px}
 .chipbar .ptitle{margin:0}
 #chipq{height:30px;width:220px;font-size:12.5px;margin-left:auto}
 #chips{max-height:196px;overflow:auto;padding-right:4px}
 /* supply summary: the figures under the capture chart, as a small grid */
 .sumline{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px 14px;
   margin-top:10px;padding-top:10px;border-top:1px solid var(--line-2);font-size:12px}
 .sumline>span{display:flex;flex-direction:column;line-height:1.3}
 .sumline b{font-size:14px;font-weight:600}
 .sumline .flat{font-size:11.5px}
 .sumline>span.note{grid-column:1/-1;display:block;font-size:11.5px}
 tr.pick{cursor:pointer}tr.pick.on td{background:var(--accent-soft)}
 .cmp tr.dupe td{background:var(--warn-soft)}
 /* one-car verdict card */
 #carcard{border-color:var(--accent-line);box-shadow:0 1px 3px rgba(20,50,110,.10)}
 .verdict{font-size:15px;line-height:1.5;margin:2px 0 14px}
 .verdict b{font-weight:600}
 .subjtag{display:inline-block;background:var(--accent);color:#fff;border-radius:4px;
          padding:0 7px;font-size:11px;line-height:18px;margin-left:6px;vertical-align:2px}
 .subjtag.typed{background:var(--warn)}
 .grid2>div{flex:1 1 300px;min-width:280px}
 .gated{color:var(--dim);font-style:italic}
 .mine{color:var(--warn);font-weight:600}
 .fld{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}
 /* the sighting-by-sighting audit table under the over-time chart */
 .prov td:last-child{color:var(--ink-2);font-size:12px}
 .prov td .flat{display:block;font-size:11px}
 .lg{display:inline-block;width:10px;height:10px;border-radius:2px;
     vertical-align:-1px;margin-right:3px}
 /* correcting stored data - visually separated from every read-only panel,
    because this is the one place on this page that WRITES */
 .fixbox{border:1px solid #d9c9a0;background:#fffdf6;border-radius:8px;
   padding:12px 14px;margin:10px 0}
 .fixbox h4{margin:0 0 8px;font-size:13px;color:#7a5a10;font-weight:600}
 .fixgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
   gap:8px}
 .fixgrid label{margin-bottom:2px}
 .fixgrid input{width:100%;height:30px;font-size:12.5px}
 .fixgrid input.dirty{border-color:#b8860b;background:#fffbea}
 .fixobs{width:100%;border-collapse:collapse;margin-top:6px}
 .fixobs th{font-size:11px;background:none;border:0;padding:2px 4px}
 .fixobs td{padding:2px 4px;border:0}
 .fixobs input{width:100%;height:28px;font-size:12px}
 .fixdiff{font-size:12.5px;margin:8px 0}
 .fixdiff div{padding:2px 0;border-top:1px solid #f0ecdf}
 .fixdiff b{font-weight:600}
 .fixdiff .was{color:var(--bad);text-decoration:line-through;margin-right:6px}
 .fixdiff .now{color:var(--good);font-weight:600}
 /* the watchlist. A star is YOUR annotation, so it is coloured apart from
    every computed thing on the page - nothing amber here came from a stat. */
 button.star{cursor:pointer;font-size:15px;line-height:1;color:#c9ccd4;
   background:none;border:0;padding:0 2px;height:auto;min-width:0}
 button.star.on,button.star:hover:not(:disabled){color:var(--mine);background:none}
 tr.starred td{background:#fffaf0}
 tr.starred td:first-child{box-shadow:inset 3px 0 0 var(--mine)}
 .wl{border:1px solid #ead9b0;background:#fffdf7;border-radius:8px;padding:4px 14px}
 .wlrow{display:flex;gap:12px;align-items:baseline;padding:9px 0;
   border-top:1px solid #f2ead8;font-size:12.5px;flex-wrap:wrap}
 .wlrow:first-child{border-top:0}
 .wlname{flex:1 1 210px;font-weight:600}
 .wltag{font-size:11px;border-radius:9px;padding:0 7px;line-height:18px;border:1px solid}
 .wltag.live{color:#245224;background:var(--good-soft);border-color:var(--good-line)}
 .wltag.stale{color:var(--warn);background:var(--warn-soft);border-color:var(--warn-line)}
 .wltag.gone{color:var(--bad);background:var(--bad-soft);border-color:var(--bad-line)}
 .okbox code{background:#fff}
 /* find-a-car bar: the primary way in, so it leads the page */
 .findbar{display:grid;grid-template-columns:1fr;
   grid-template-areas:"h" "p" "row" "msg";
   background:radial-gradient(120% 140% at 0% 0%,#f3f7ff 0%,#e9f0fc 60%,#e3ecfb 100%);
   border:1px solid var(--accent-line);border-radius:var(--r);padding:16px 20px;margin-bottom:16px;
   box-shadow:var(--shadow);animation:vpa-rise .3s ease-out backwards}
 .findbar h2{grid-area:h;font-family:var(--ui-d);font-size:16px;margin:0 0 2px;font-weight:650;
   color:var(--accent-2);letter-spacing:-.01em}
 .findbar p{grid-area:p;margin:0;font-size:12.5px;color:var(--ink-2)}
 .findrow{grid-area:row;display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
 .linkform{flex:1 1 420px;display:flex;align-items:center;gap:0;position:relative;min-width:0}
 .linkform .lk{position:absolute;left:12px;color:var(--faint);pointer-events:none}
 .linkform input{flex:1;min-width:0;height:38px;padding-left:36px;border-radius:var(--r-sm) 0 0 var(--r-sm);
   border-right:0;background:#fff}
 .linkform .bigbtn{border-radius:0 var(--r-sm) var(--r-sm) 0}
 #findmsg{grid-area:msg}
 button.bigbtn{height:38px;padding:0 16px;border:1px solid var(--accent);border-radius:var(--r-sm);
   background:var(--accent);color:#fff;font-size:13.5px;font-weight:600;
   box-shadow:0 1px 2px rgba(34,78,165,.3),inset 0 1px 0 rgba(255,255,255,.12)}
 button.bigbtn:hover:not(:disabled){background:var(--accent-2);border-color:var(--accent-2);color:#fff}
 button.bigbtn.alt{background:#fff;color:var(--accent-2);border-color:#9db8dd;font-weight:500}
 button.bigbtn.alt:hover:not(:disabled){background:#f2f7ff;color:var(--accent-2)}
 .findbar.drag{border-color:var(--accent);background:#e9f1fd}
 #findmsg{font-size:12.5px}
 #findmsg:not(:empty){margin-top:10px}
 .idcard{background:#fff;border:1px solid var(--accent-line);border-radius:8px;
   padding:10px 12px;margin-top:10px;font-size:12.5px}
 .idcard b{font-weight:600}
 .swatchdot{display:inline-block;width:11px;height:11px;border-radius:3px;
   border:1px solid rgba(0,0,0,.25);vertical-align:-1px;margin-right:5px}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Explore vehicles</h1>
 <div class="sub"><span id="subline">One trim, or one car against its trim. Read-only.</span></div>
</div>__ACTIONS__</div>
<div id="stale"></div>

<div class="card keep" id="help" style="display:none">
 <p><b>This page never writes anything.</b> It reads the same database the
 importer fills, using the same statistics engine as <code>vpa report</code>
 and the Excel workbook - a median here equals the median there.</p>
 <ul style="font-size:13px">
  <li><b>Selection works like the workbook's picker:</b> exact matching at
      three levels. Make alone pools every model; make+model pools every trim
      ("Ford Falcon"); adding a trim narrows to just that trim.</li>
  <li><b>Median by capture day</b> is the shaded IQR (25-75%) with the median
      line - the Trend sheet. <b>For sale</b> counts distinct vehicles seen
      that day - the Supply sheet.</li>
  <li><b>Bell curve and Standouts.</b> The curve is fitted on <i>log</i>
      price, so it cannot run below $0 and the long tail of rare cars does
      not drag it; the panel beside it shows how many cars really sit in each
      tail against how many the curve predicts, so you can see where it
      misfits. <b>Standouts</b> lists the cars in those tails with two
      scores: sigma on asking price, and sigma once every car is restated at
      the same odometer. A car that is only cheap because it has done 300,000
      km drops back towards zero on the second one.</li>
  <li><b>"In latest capture" is not "still for sale."</b> It means the car
      appeared in this selection's most recent capture. A car missing from it
      may have sold, or may simply not have been in the search you captured
      that day - and listing identity breaks across capture eras, so absence
      is often a format artefact. Treat it as a lower bound.</li>
  <li><b>Turnover</b> is the same caution turned into a measurement. The
      capture chart splits each bar into cars carried over, cars back in
      view, brand new cars, and cars with no site ad id (which cannot be
      recognised across days at all); cars last seen on a day hang below the
      line. The tab then rates how fast the trim clears - exits per car per
      day, so trims captured on different rhythms are still comparable - and
      whether the cars that went were the cheap ones for their day. It
      refuses rather than guesses in three cases: no trackable ids, two
      consecutive captures that share no cars (different searches, not an
      empty market), and a pair where one capture is far bigger than the
      other. <b>It never says "sold"</b> - a listing can vanish by selling,
      being withdrawn, expiring, or being re-listed under a new id.</li>
  <li><b>The tabs</b> are the workbook's deeper sheets: monthly trend
      (Trend), price against odometer with the fitted curve and dealer/private
      medians (RichStats), and every trim side by side (RichStats + Supply).
      Two bases are in play and each panel says which it uses - per capture
      day counts every sighting, monthly and the mileage fit count each
      vehicle once per month, which is what <code>vpa report</code> does.</li>
  <li>The <b>Compare</b> tab carries this selection and its nearest trims
      across to the side-by-side view - the workbook's Comparison sheet.</li>
  <li><b>Finding a car by its link.</b> Paste an advert's address (or just its
      ad id, e.g. SSE-AD-20578075) into the box at the top. It is matched
      against the links and ad ids your captures recorded - the advert is
      never opened. A car you never captured cannot be scored from a link
      alone (a link has no price or kilometres): save its page and use
      <b>From a saved page</b>. Facebook captures carry no links.</li>
  <li><b>Judging one car.</b> Click any row in the vehicles table, or any
      point on the price-vs-odometer scatter, to rank that car against the
      rest of its trim. <b>Score a car I've seen</b> does the same for a car
      that is not in your data - type its price, kilometres and year and it
      is judged by the identical code. Nothing is saved either way; the
      subject lives in the address bar, so a verdict can be bookmarked.</li>
  <li>The verdict pairs the <b>raw rank</b> with the <b>mileage-adjusted</b>
      one, because they often disagree - a dear-looking car with low
      kilometres is a different proposition from a dear one with high. The
      peer pool is <b>one price per car</b> (its latest sighting) with the
      subject removed, so nothing grades itself and a car sitting unsold
      across seven captures does not vote seven times.</li>
  <li>Below 8 other cars there is no ranking at all, and between 8 and 20 you
      get a count rather than a percentile. The mileage adjustment is
      withheld outside the kilometre range the fit actually covers, and the
      model line's fit is never substituted for a trim's.</li>
  <li><b>Forecast</b> only appears with 3+ monthly buckets and 8+ listings;
      otherwise it says so plainly rather than guessing.</li>
  <li><b>Mileage adjustment</b> needs 20+ priced listings with an odometer and
      a wide enough km spread, or it is refused with the reason.</li>
  <li><span class="tag warn">merged identity</span> on a vehicle means two
      different cars share one identity (they show two prices on the same
      day). Their difference is never counted as a price movement.</li>
 </ul></div>

<div class="findbar" id="findbar">
 <h2>Got a specific car in mind?</h2>
 <p>Paste its advert link to find it in your data and rank it against its trim &mdash;
  the link is only matched against what you captured; nothing is opened or fetched.</p>
 <div class="findrow">
  <form class="linkform" id="linkForm" autocomplete="off">
   <svg class="lk" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>
   <input type="text" id="findLink" spellcheck="false"
     placeholder="https://www.carsales.com.au/cars/details/.../SSE-AD-20578075/  or just the ad id">
   <button class="bigbtn" type="submit" id="linkBtn">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
     <path d="M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4.2-4.2"/></svg>
    Find</button>
  </form>
  <button class="bigbtn alt" id="findBtn" title="a saved page (Ctrl+S on the ad) also carries price, odometer and year, so a car you never captured can still be scored">
   From a saved page</button>
  <button class="bigbtn alt" id="seenBtn">
   <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M12 5v14M5 12h14"/></svg>
   Type one I've seen</button>
 </div>
 <input type="file" id="findFile" accept=".htm,.html" style="display:none">
 <div id="findmsg"></div>
</div>

<div class="card keep" id="seencard" style="display:none">
 <div class="ptitle">score a car you have seen
   <span>&mdash; nothing is saved; the numbers stay in the address bar</span></div>
 <div class="fld">
  <div><label>asking price</label><input type="text" id="sPrice" class="w8" placeholder="$18,990"></div>
  <div><label>odometer</label><input type="text" id="sOdo" class="w8" placeholder="132k"></div>
  <div><label>year</label><input type="text" id="sYear" class="w6" placeholder="2013"></div>
  <div><label>seller</label><select id="sSeller" class="w6">
    <option value="">unknown</option><option value="dealer">dealer</option>
    <option value="private">private</option></select></div>
  <div><label>state</label><input type="text" id="sState" class="w6" placeholder="NSW"></div>
  <div><label>your note (colour, where)</label><input type="text" id="sNote"
    class="w8" style="width:170px" placeholder="blue, Parramatta"></div>
  <div><label>your target price</label><input type="text" id="sTarget" class="w8"
    placeholder="optional"></div>
  <div><button class="ghost" id="sGo">Score it</button></div>
 </div>
 <div class="hint note">Price and odometer are read the same way the importer reads
  them, so <code>$18,990</code> and <code>98k</code> both work &mdash; the card
  echoes back what was understood. Your note and target price never leave this
  browser.</div>
</div>

<div class="card" id="wlcard" style="display:none">
 <div class="cardhead">
  <div class="ptitle">watchlist <span id="wlspan"></span></div>
  <button class="ghost mini" id="wlRefresh">Refresh</button>
 </div>
 <div id="wlbody"></div>
</div>

<div class="card keep">
 <div class="ptitle">what to look at</div>
 <div class="row">
  <div><label>make</label><select id="mk"></select></div>
  <div><label>model</label><select id="mo"></select></div>
  <div><label>trim / variant</label><select id="va"></select></div>
  <div><label>order trims by</label><select id="vsort">
    <option value="n">most prices collected</option>
    <option value="name">name (A-Z)</option>
    <option value="median_desc">median price (high to low)</option>
    <option value="median_asc">median price (low to high)</option>
    <option value="days">most capture days</option>
    <option value="last">most recently captured</option>
    <option value="vehicles">most distinct vehicles</option>
    <option value="odo">lowest median odometer</option>
  </select></div>
  <div><button class="ghost" id="clearBtn">All trims</button></div>
 </div>
 <div class="row filters">
  <div><label>year from</label><input type="number" id="fY0" class="w6" placeholder="any"></div>
  <div><label>to</label><input type="number" id="fY1" class="w6" placeholder="any"></div>
  <div><label>price from</label><input type="text" id="fP0" class="w8" placeholder="any"></div>
  <div><label>to</label><input type="text" id="fP1" class="w8" placeholder="any"></div>
  <div style="flex:1;min-width:170px"><label>keyword</label>
    <input type="text" id="fKw" placeholder="e.g. stingray"></div>
  <div><button class="ghost" id="fGo">Apply</button></div>
  <div><button class="ghost" id="fClear">Clear filters</button></div>
 </div>
 <div class="chipbar" id="chipbar" style="display:none">
  <div class="ptitle">trims in this model <span id="chipspan"></span></div>
  <input type="text" id="chipq" placeholder="filter trims..." autocomplete="off">
 </div>
 <div id="chips"></div>
 <div class="hint" id="pickhint"></div>
</div>

<div class="card"><div class="ptitle">selection <span id="selspan"></span></div>
 <div class="tiles" id="tiles"></div>
 <div id="caveats"></div>
</div>

<div class="card" id="carcard" style="display:none">
 <div class="cardhead">
  <div class="ptitle">this car vs the trim <span id="carspan"></span></div>
  <button class="ghost mini" id="fixBtn" title="correct this listing's stored details">Fix data</button>
  <button class="ghost mini" id="carClose">Clear</button>
 </div>
 <div id="fixpanel" style="display:none"></div>
 <div id="carbody"></div>
</div>

<div class="card">
 <div class="act">
  <div class="panel"><div class="ptitle">median by capture day
    <span>&mdash; shaded band is the 25-75% range</span></div><div id="trend"></div></div>
  <div class="panel"><div class="ptitle">for sale at each capture
    <span id="supplyspan"></span></div><div id="supply"></div>
    <div class="sumline" id="supplysum"></div></div>
 </div>
</div>

<div class="card">
 <div class="tabs" id="tabs">
  <button class="tab on" data-t="month">Monthly trend</button>
  <button class="tab" data-t="fc">Projection</button>
  <button class="tab" data-t="moves">Price changes</button>
  <button class="tab" data-t="spread">Bell curve</button>
  <button class="tab" data-t="stand">Standouts</button>
  <button class="tab" data-t="mile">Price vs odometer</button>
  <button class="tab" data-t="turn">Turnover</button>
  <button class="tab" data-t="who">Who is selling</button>
  <button class="tab" data-t="trims">Trim breakdown</button>
 </div>
 <div id="tabbody"></div>
</div>

<div class="card">
 <div class="row" style="margin-bottom:8px">
  <div style="flex:1"><div class="ptitle" style="margin:0">individual vehicles
    <span id="vspan"></span></div></div>
  <div><input type="text" id="filter" placeholder="filter: year, trim, state, seller, source..."
       style="width:280px"></div>
 </div>
 <div class="scroll"><table id="vtbl"><thead><tr>
  <th class="s" data-k="starred" title="cars you are following">&#9733;</th>
  <th class="s" data-k="year">year</th><th class="s" data-k="variant">trim</th>
  <th class="s num" data-k="price">price</th>
  <th class="s num" data-k="odometer_km">odometer</th>
  <th class="s num" data-k="per10k">$/10k km</th>
  <th class="s" data-k="state">state</th><th class="s" data-k="seller_type">seller</th>
  <th class="s" data-k="source">source</th>
  <th class="s num" data-k="sightings">seen</th>
  <th class="s" data-k="last_day">last seen</th>
  <th>listing</th>
 </tr></thead><tbody></tbody></table></div>
 <div class="hint" id="vhint"></div>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money=v=>v==null?"":"$"+Math.round(v).toLocaleString();
const km=v=>v==null?"":Math.round(v).toLocaleString()+" km";
function pct(v){if(v==null)return '<span class="flat">&mdash;</span>';
  const c=v>0.05?"up":(v<-0.05?"down":"flat");
  return `<span class="${c}">${v>0?"+":""}${v.toFixed(1)}%</span>`;}

let TREE=null, DATA=null, sortKey="last_day", sortDir=-1;

/* ---------- charts (inline SVG, no libraries) ---------- */
function trendChart(byday){
  const el=$("trend");
  if(!byday.length){el.innerHTML='<div class="flat">no priced listings</div>';return;}
  const W=520,H=210,L=58,R=12,T=12,B=30,n=byday.length;
  const vals=byday.flatMap(d=>[d.q25,d.q75,d.median]).filter(v=>v!=null);
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(lo===hi){lo=lo*0.95;hi=hi*1.05;}
  // widen to the round numbers either side, so the top and bottom lines are labelled
  const tk0=vpaTicks(lo,hi,5),st=tk0.length>1?tk0[1]-tk0[0]:0;
  if(st){lo=Math.floor(lo/st)*st;hi=Math.ceil(hi/st)*st;}
  const tk=vpaTicks(lo,hi,5);
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo||1));
  const line=byday.map((d,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(d.median).toFixed(1)}`).join("");
  const band=byday.map((d,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(d.q75).toFixed(1)}`).join("")
    +byday.slice().reverse().map((d,i)=>`L${x(n-1-i).toFixed(1)},${y(d.q25).toFixed(1)}`).join("")+"Z";
  const grid=tk.map(v=>`
    <line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${vpaAxMoney(v)}</text>`).join("");
  const dots=byday.map((d,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(d.median).toFixed(1)}" r="3.2"
    fill="#c04040"><title>${d.d}: median ${money(d.median)} (n=${d.n}, IQR ${money(d.q25)}-${money(d.q75)})</title></circle>`).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="median price by capture day">${grid}
    <path d="${band}" fill="#c04040" opacity=".13"/>
    <path d="${line}" fill="none" stroke="#c04040" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>${dots}
    <text x="${L}" y="${H-8}" class="ax">${byday[0].d}</text>
    <text x="${W-R}" y="${H-8}" text-anchor="end" class="ax">${byday[n-1].d}</text></svg>`;
}
/* Each capture, split into what it is actually made of.
   A plain count of "how many were up" hides the interesting half: forty cars
   twice running can be the same forty sitting unsold, or forty out and forty
   in. So each bar stacks, bottom up:
       carried over   - also in the capture before
       back in view   - seen earlier, missed by the capture before
       new            - never seen here before
       not trackable  - no site ad id, so it cannot be recognised across days
   and the cars last seen on that day hang BELOW the axis, at the day they
   were last up rather than the day they were noticed missing.
   The bar total still equals the count the old chart drew. */
function supplyChart(byday){
  const el=$("supply");
  if(!byday.length){el.innerHTML='<div class="flat">nothing captured</div>';return;}
  const W=520,H=232,L=44,R=12,T=12,B=42,n=byday.length;
  const up=Math.max(...byday.map(d=>d.vehicles))||1;
  const dn=Math.max(...byday.map(d=>d.exits||0))||0;
  const PH=H-T-B;
  const zero=T+PH*(dn?up/(up+dn):1);
  const sU=(zero-T)/up, sD=dn?(T+PH-zero)/dn:0;
  const bw=Math.max(6,Math.min(38,(W-L-R)/n-6));
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R-bw)/(n-1))+bw/2;
  const grid=[0,.5,1].map(f=>{const v=Math.round(up*f),yy=zero-v*sU;return `
    <line x1="${L}" y1="${yy.toFixed(1)}" x2="${W-R}" y2="${yy.toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(yy+3.5).toFixed(1)}" text-anchor="end" class="ax">${v}</text>`;}).join("");
  const seg=(xi,base,v,fill,tip)=>v<=0?"":`<rect x="${(xi-bw/2).toFixed(1)}"
     y="${(zero-(base+v)*sU).toFixed(1)}" width="${bw.toFixed(1)}"
     height="${(v*sU).toFixed(1)}" fill="${fill}"><title>${tip}</title></rect>`;
  const bars=byday.map((d,i)=>{
    const xi=x(i), coh=d.cohort||0, untracked=Math.max(0,d.vehicles-coh);
    // first capture of the run has nothing to be carried FROM, so its whole
    // trackable count is drawn as one neutral block rather than guessed at
    const known=d.carry!=null;
    const carry=known?d.carry:0, nw=known?d.new:0;
    const back=known?Math.max(0,coh-carry-nw):coh;
    let b=0,out="";
    out+=seg(xi,b,carry,"#7d9e7d",`${d.d}: ${carry} carried over from the capture before`);b+=carry;
    out+=seg(xi,b,back,"#b7cbb0",`${d.d}: ${back} seen before, but not in the previous capture`);b+=back;
    out+=seg(xi,b,nw,"#5b87c4",`${d.d}: ${nw} new here`);b+=nw;
    out+=seg(xi,b,untracked,"#d4d8e0",`${d.d}: ${untracked} with no site ad id - cannot be tracked across captures`);
    if(d.exits)out+=`<rect x="${(xi-bw/2).toFixed(1)}" y="${zero.toFixed(1)}"
       width="${bw.toFixed(1)}" height="${(d.exits*sD).toFixed(1)}" fill="#c56b6b" opacity="${d.rated?1:.45}">
       <title>${d.d}: ${d.exits} last seen on this day${d.rated?"":" (not rated - the next capture was much smaller)"}</title></rect>`;
    if(d.break_before)out+=`<line x1="${(xi-bw/2-3).toFixed(1)}" y1="${T}"
       x2="${(xi-bw/2-3).toFixed(1)}" y2="${(T+PH).toFixed(1)}" stroke="#c56b6b"
       stroke-width="1.5" stroke-dasharray="3 3"><title>coverage break: this capture
 shares no cars with the one before, so they cover different searches</title></line>`;
    return out;}).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="what each capture was made of">${grid}${bars}
    <line x1="${L}" y1="${zero.toFixed(1)}" x2="${W-R}" y2="${zero.toFixed(1)}" stroke="#c3c8d2"/>
    <text x="${L}" y="${H-8}" class="ax">${byday[0].d}</text>
    <text x="${W-R}" y="${H-8}" text-anchor="end" class="ax">${byday[n-1].d}</text></svg>
   <div class="lgd">${[["#7d9e7d","carried over"],["#b7cbb0","back in view"],
      ["#5b87c4","new"],["#d4d8e0","no ad id"],["#c56b6b","last seen (below the line)"]]
     .map(([c,t])=>`<span><i style="background:${c}"></i>${t}</span>`).join("")}</div>`;
}

/* ---------- one car judged against its trim -----------------------------
   Every number here is computed server-side by explore.car_payload(); this
   only lays it out. The one exception is the target-price line, which is
   arithmetic on a number the user typed and deliberately never leaves the
   browser. */
let CAR=null, TARGET=null;

/* where the subject sits on the peers' price axis: peers as faint ticks, the
   middle half shaded, the median marked, the subject bold. A percentile
   alone hides the scale - "at the median" is worth nothing without the band
   the median sits in. */
function positionStrip(o,subject,label,colour){
  const W=560,H=62,L=10,R=10,T=16;
  const s=Math.min(o.lo,subject),e=Math.max(o.hi,subject);
  const x=v=>L+(W-L-R)*((v-s)/((e-s)||1));
  const ticks=(o.ticks||[]).map(v=>
    `<line x1="${x(v).toFixed(1)}" y1="${T+4}" x2="${x(v).toFixed(1)}" y2="${T+22}"
      stroke="#b9c2d0" stroke-width="1" opacity=".55"/>`).join("");
  // The subject's own price label is the one that matters, so it always
  // shows, kept inside the frame; an end label it would overprint is
  // dropped (it read "$89$150,0" when the car was the dearest in the trim).
  const xs=x(subject);
  const anchor=xs>W-R-40?"end":(xs<L+40?"start":"middle");
  return `<div class="mini-h">${label}</div>
   <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
     aria-label="${esc(label)}">
    <rect x="${x(o.q25).toFixed(1)}" y="${T}" width="${Math.max(1,x(o.q75)-x(o.q25)).toFixed(1)}"
      height="30" fill="${colour}" opacity=".13" rx="3"/>
    <line x1="${L}" y1="${T+15}" x2="${W-R}" y2="${T+15}" stroke="#e3e7ee"/>
    ${ticks}
    <line x1="${x(o.median).toFixed(1)}" y1="${T-2}" x2="${x(o.median).toFixed(1)}"
      y2="${T+32}" stroke="#8792a5" stroke-width="1.5" stroke-dasharray="3 2"/>
    <circle cx="${x(subject).toFixed(1)}" cy="${T+15}" r="7" fill="${colour}"/>
    ${xs-L>78?`<text x="${L}" y="${H-6}" class="ax">${money(o.lo)}</text>`:""}
    <text x="${x(o.median).toFixed(1)}" y="${T-5}" text-anchor="middle" class="ax">median ${money(o.median)}</text>
    ${W-R-xs>78?`<text x="${W-R}" y="${H-6}" text-anchor="end" class="ax">${money(o.hi)}</text>`:""}
    <text x="${xs.toFixed(1)}" y="${H-6}" text-anchor="${anchor}"
      class="ax" style="fill:${colour};font-weight:700">${money(subject)}</text>
   </svg>`;
}

/* ---------- where this car sat over time, and where each sighting came from
   Two panels, because they answer two different doubts. The chart answers
   "has it moved against the trim" - the trim's full range is the faint band,
   its middle half the stronger one, its median the dashed line, and this car
   the solid blue line. Gaps in that line are real and are meant to show: a
   day the trim WAS captured and this car was not in it.

   The table answers "was it really in my earlier captures", which a chart
   cannot: it names the import behind every sighting, so a capture day is
   checkable against the ingest log rather than something the app asserts. */
function overTimeChart(b){
  if(b.length<2)return "";
  const W=560,H=215,L=62,R=14,T=14,B=32,n=b.length;
  const vals=b.flatMap(d=>[d.lo,d.hi,d.mine]).filter(v=>v!=null);
  if(!vals.length)return "";
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(lo===hi){lo*=0.95;hi*=1.05;}
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo||1));

  /* a day on which only this car was captured has no peer band at all, so
     the bands are built over the days that HAVE one rather than assuming
     every day does - y(undefined) would silently poison the path */
  const withBand=b.map((d,i)=>({d,i})).filter(o=>o.d.median!=null);
  const band=(a,c,fill,op)=>withBand.length<2?"":
    `<path d="${withBand.map((o,j)=>`${j?"L":"M"}${x(o.i).toFixed(1)},${y(o.d[a]).toFixed(1)}`).join("")
      +withBand.slice().reverse().map(o=>`L${x(o.i).toFixed(1)},${y(o.d[c]).toFixed(1)}`).join("")}Z"
      fill="${fill}" opacity="${op}"/>`;
  const med=withBand.length<2?"":`<path d="${withBand.map((o,j)=>
    `${j?"L":"M"}${x(o.i).toFixed(1)},${y(o.d.median).toFixed(1)}`).join("")}"
    fill="none" stroke="#8792a5" stroke-width="1.6" stroke-dasharray="4 3"/>`;

  /* the subject's own line, broken wherever a sighting is missing */
  let path="",pen=false;
  b.forEach((d,i)=>{
    if(d.mine==null){pen=false;return;}
    path+=`${pen?"L":"M"}${x(i).toFixed(1)},${y(d.mine).toFixed(1)}`;pen=true;});
  const dots=b.map((d,i)=>d.mine==null?"":
    `<circle cx="${x(i).toFixed(1)}" cy="${y(d.mine).toFixed(1)}" r="4.5" fill="#3060a0">
      <title>${d.d}: ${money(d.mine)}${d.rank!=null
        ?` - ${ordinal(d.rank)} cheapest of ${d.of} captured that day`
        :(d.n?` - ${d.n} other car(s) captured that day`:" - the only one captured that day")}</title>
     </circle>`).join("");
  const grid=vpaTicks(lo,hi,3).map(v=>{return `
    <line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${vpaAxMoney(v)}</text>`;}).join("");

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="this car's asking price against its trim on each capture day">
    ${grid}${band("lo","hi","#c04040",".07")}${band("q75","q25","#c04040",".15")}${med}
    <path d="${path}" fill="none" stroke="#3060a0" stroke-width="2.2"
      stroke-linejoin="round" stroke-linecap="round"/>${dots}
    <text x="${L}" y="${H-8}" class="ax">${b[0].d}</text>
    <text x="${W-R}" y="${H-8}" text-anchor="end" class="ax">${b[n-1].d}</text></svg>
    <div class="hint"><span class="lg" style="background:#3060a0"></span>this car
      &nbsp;<span class="lg" style="background:#8792a5"></span>trim median
      &nbsp;<span class="lg" style="background:#c04040;opacity:.35"></span>middle half,
      faint band the full range. A gap means the trim was captured that day and
      this car was not in it.</div>`;
}

/* '2026-07-27 10:27:53' -> '27 Jul 2026, 10:27'. The date is the user's own
   clock at import time, so it is shown as written rather than reformatted
   into anything that could imply a timezone conversion happened. */
const _MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function stamp(s){
  if(!s)return "";
  const m=/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(s);
  if(!m)return esc(s);
  return `${+m[3]} ${_MON[+m[2]-1]} ${m[1]}, ${m[4]}:${m[5]}`;
}
function overTime(h){
  if(!h.rows||!h.rows.length)return "";
  const chart=overTimeChart(h.byday||[]);
  /* the sign goes OUTSIDE the currency symbol - money(-5000) reads "$-5,000",
     which is not how anyone writes a price cut */
  const delta=(v,f)=>`${v>0?"+":"−"}${f(Math.abs(v))}`;
  const rows=h.rows.map(r=>`<tr>
    <td>${r.d}</td>
    <td class="num">${money(r.price)}${r.d_price?`<span class="${r.d_price>0?"up":"down"}"
      > ${delta(r.d_price,money)}</span>`:""}</td>
    <td class="num">${km(r.odometer_km)}${r.d_odo?`<span class="flat">
      ${delta(r.d_odo,km)}</span>`:""}</td>
    <td>${r.imported_at
      ?`${stamp(r.imported_at)}${r.batch_rows?`, in a batch of ${r.batch_rows}`:""}
        <span class="flat">${esc(r.source_file==="(import-ui)"
          ?"pasted into Import with preview":(r.source_file||r.adapter||""))}</span>`
      :'<span class="gated">no import recorded</span>'}</td></tr>`).join("");
  /* The point of this sentence: a car you do not remember seeing is not
     evidence of a bug until you can rule out that it simply arrived among
     other rows. Separate imports on separate days is exactly that check. */
  const lead=h.rows.length<2?"":
    `Recorded by <b>${h.imports} separate import${h.imports===1?"":"s"}</b>${
      h.imports===1?" &mdash; every sighting came from ONE import, so these are "
        +"not independent captures":""}.
     ${h.unchanged?`The asking price and odometer are identical on all
        ${h.rows.length}, so this is the same unchanged ad seen again, not a
        price history.`:""}
     ${h.orphans?`<b>${h.orphans}</b> sighting(s) cannot name the import that
        recorded them.`:""}`;
  return `<div class="mini-h" style="margin-top:14px">where this car sat over time</div>
    ${chart}
    <table class="cmp prov"><thead><tr><th>capture day</th><th class="num">asking</th>
      <th class="num">odometer</th><th>recorded by</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="hint">${lead}</div>`;
}

/* ---------- the watchlist ---------------------------------------------
   A star is the user's own annotation. It changes nothing the engines
   compute - no median moves, no pool changes - it only decides what gets
   drawn in amber and what the watchlist reports. Kept visually distinct
   from every computed figure on the page for exactly that reason. */
/* Every call to a NEW endpoint goes through this. The app is a Python process
   that holds whatever code it imported at launch - it does not hot-reload -
   so a page served after an upgrade can call an endpoint the running process
   has never heard of. That used to surface as alert("unknown endpoint"),
   which reads like a bug in the feature rather than "restart me". */
let STALE=false;
/* "duplicate?" - another identity holds the same price and odometer. Links
   to that pair on the Duplicates page, where it can be merged or marked as
   different cars. */
function dupTag(key){
  return `<a class="tag warn" href="/duplicates?key=${encodeURIComponent(key)}" style="text-decoration:none"
    title="another identity holds the same price and odometer - probably the same advert captured twice. Click to review it on the Duplicates page.">duplicate?</a>`;
}
async function apiCall(url,opts){
  let r;
  try{r=await fetch(url,opts);}catch(e){return {ok:false,error:String(e)};}
  let j;
  try{j=await r.json();}catch(e){j={ok:false,error:"HTTP "+r.status};}
  if(r.status===404||j.stale)showStale(j.error);
  return j;
}
function showStale(msg){
  if(STALE)return;                       // one banner, not one per click
  STALE=true;
  const bar=document.createElement("div");
  bar.className="warnbox";
  bar.dataset.sticky="1";                // vpaReveal keeps cards clear of it
  bar.style.cssText="position:sticky;top:var(--navh);z-index:50;margin:0 0 10px";
  bar.innerHTML=`<b>The app needs restarting.</b> ${esc(msg||"")}`
    +` <button class="ghost mini" id="staleQuit" style="margin-left:8px">Shut down now</button>`;
  // NOT querySelector(".wrap,body"): a selector list returns the first match
  // in DOCUMENT order, and <body> precedes .wrap - so the banner landed
  // outside the page's own container and missed its width and padding.
  (document.querySelector(".wrap") || document.body).prepend(bar);
  const b=document.getElementById("staleQuit");
  if(b)b.onclick=()=>{fetch("/api/shutdown",{method:"POST",body:"{}"})
    .then(()=>{bar.innerHTML="<b>Stopped.</b> Reopen Vehicle Import to "
      +"pick up the new version.";});};
  bar.scrollIntoView({behavior:"smooth",block:"nearest"});
}

async function toggleStar(key,on){
  const r=await apiCall("/api/star",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({key,on})});
  if(!r.ok){if(!r.stale)alert(r.error);return;}
  // the flag lives in the payload, so refresh it rather than guessing
  const v=(DATA&&DATA.vehicles||[]).find(x=>x.key===key);
  if(v)v.starred=on;
  if(DATA)DATA.starred_total=r.total;
  renderVehicles();loadWatchlist();
}
function starMoney(v){return v==null?"—":money(v);}
async function loadWatchlist(){
  const w=await apiCall("/api/watchlist");
  const card=$("wlcard");
  if(!w.ok||!w.total){card.style.display="none";return;}
  card.style.display="block";
  const bits=[];
  if(w.moved)bits.push(`${w.moved} re-priced at the latest capture`);
  if(w.not_in_latest)bits.push(`${w.not_in_latest} not in their trim's latest sweep`);
  if(w.gone)bits.push(`${w.gone} no longer in the database`);
  $("wlspan").innerHTML=`&mdash; ${w.total} car(s) followed`
    +(bits.length?` &middot; ${bits.join(" &middot; ")}`:"");
  $("wlbody").innerHTML=`<div class="wl">`+w.cars.map(c=>{
    if(!c.present)return `<div class="wlrow">
      <span class="wlname">${esc(c.label)}</span>
      <span class="wltag gone">gone</span>
      <span class="flat">${esc(c.why)}</span>
      <button class="ghost mini" data-unstar="${esc(c.key)}">unfollow</button></div>`;
    const s=c.since||{};
    // "how it stacks up" is the comparison against its OWN trim, not a bare
    // percentage - a car holding its price in a falling market has moved
    // The figure a bare percentage hides: holding your price while the trim
    // falls IS a rise, and this is the only column that says so.
    const vs=s.vs_trim==null?"":`<span title="its own move minus its trim's
      move over the same two capture days - positive means it got relatively
      dearer, whatever its own price did"><span class="flat">vs its trim</span>
      <b class="${s.vs_trim>0?"down":"up"}">${s.vs_trim>0?"+":""}${s.vs_trim}%</b></span>`;
    return `<div class="wlrow">
      <span class="wlname">${esc(c.label)}${c.year?` <span class="flat">${c.year}</span>`:""}</span>
      <span>${starMoney(c.price)}</span>
      <span>${c.odometer_km==null?"":km(c.odometer_km)}</span>
      ${c.change!=null?`<span title="asking price now against the first time you
        saw it, on ${c.first_day}"><span class="flat">since ${c.first_day}</span>
        ${pct(c.change)}</span>`:""}
      ${s.move!=null?`<span title="between the two most recent capture days of
        this car's own trim, ${s.previous_day} and ${s.capture_day}"
        ><span class="flat">latest sweep</span> ${pct(s.move)}</span>`:""}
      ${vs}
      ${c.rank?`<span class="flat">${ordinal(c.rank.rank)} cheapest of ${c.rank.n+1}</span>`
              :`<span class="gated" title="${esc(c.rank_reason||"")}">no rank</span>`}
      ${c.vs_median!=null?`<span class="flat">${c.vs_median>0?"+":""}${c.vs_median}% vs trim median</span>`:""}
      <span class="wltag ${c.live?"live":"stale"}">${c.live?"in latest sweep"
        :`not in the ${esc(String(c.trim_last_day||""))} sweep`}</span>
      ${vpaAdLinks(c.url,!c.live,c)}
      <button class="ghost mini" data-unstar="${esc(c.key)}">unfollow</button>
      </div>`;}).join("")
    +`</div><div class="hint note">${esc(w.basis)}. A star changes nothing that is
      computed &mdash; no median, rank or forecast moves because you followed
      a car.</div>`;
  document.querySelectorAll("[data-unstar]").forEach(b=>
    b.onclick=()=>toggleStar(b.dataset.unstar,false));
}
$("wlRefresh").onclick=loadWatchlist;

/* ---------- correcting stored data ------------------------------------
   The ONLY writing thing on this page, and it is deliberately three steps:
   type, preview the diff, then save. The preview is produced by the server
   from the same plan() that performs the write, so what you approve is what
   runs - and the fingerprint it returns is sent back with the save, so an
   import committed in another tab between the two cannot slip underneath.
   Nothing here is clever: it refuses anything it cannot validate and says
   why, rather than storing a value the importer would have rejected. */
let FIX=null;
async function openFix(){
  if(!CAR||!CAR.subject||!CAR.subject.key)return;
  const d=await apiCall("/api/edit/row?key="+encodeURIComponent(CAR.subject.key));
  if(!d.ok){$("fixpanel").innerHTML=`<div class="warnbox">${esc(d.error)}</div>`;
    $("fixpanel").style.display="block";return;}
  FIX=d;renderFix();
}
function renderFix(){
  const d=FIX,v=d.vehicle,el=$("fixpanel");
  const field=(f,val)=>`<div><label>${f.replace(/_/g," ")}</label>
    <input data-v="${f}" value="${esc(val==null?"":String(val))}"></div>`;
  const obs=d.observations.map(o=>`<tr data-o="${o.id}">
    <td><input data-o="${o.id}" data-f="capture_date" value="${esc(o.capture_date)}"></td>
    <td><input data-o="${o.id}" data-f="price_aud" value="${o.price_aud==null?"":o.price_aud}"></td>
    <td><input data-o="${o.id}" data-f="odometer_km" value="${o.odometer_km==null?"":o.odometer_km}"></td>
    <td><input data-o="${o.id}" data-f="price_note" value="${esc(o.price_note||"")}"></td>
    <td><button class="ghost mini" data-del="${o.id}">delete</button></td></tr>`).join("");
  el.innerHTML=`<div class="fixbox">
    <h4>correct this listing &mdash; nothing is saved until you review the change</h4>
    <div class="fixgrid">${d.vehicle_fields.map(f=>field(f,v[f])).join("")}</div>
    <div class="mini-h" style="margin-top:12px">its ${d.observations.length}
      recorded sighting(s) &mdash; this is the price history itself</div>
    <table class="fixobs"><thead><tr><th>capture day</th><th>price</th>
      <th>odometer</th><th>note</th><th></th></tr></thead>
      <tbody>${obs}</tbody></table>
    <div><label style="font-size:10.5px;color:#667">why (recorded with the change)</label>
      <input id="fixWhy" style="width:100%;height:30px;font-size:12.5px"
        placeholder="e.g. misread the ad, wrong trim on the tile"></div>
    <div id="fixdiff"></div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="ghost" id="fixPreview">Review change</button>
      <button class="ghost" id="fixCancel">Cancel</button>
      <span style="flex:1"></span>
      <button class="ghost mini danger" id="fixDelCar">Delete this vehicle</button>
    </div>
    <div class="hint">The key stays as it is: editing make, model or trim
      corrects what the row SAYS without re-deriving its identity, so its
      price history stays attached. A backup is taken before any save, and
      every change is recorded and can be undone
      (<code>python -m vpa edit --history</code>).</div>
  </div>`;
  el.style.display="block";
  el.querySelectorAll("input").forEach(i=>i.oninput=()=>{
    const f=i.dataset.v,o=i.dataset.o;
    const was=o?String(FIX.observations.find(x=>String(x.id)===o)[i.dataset.f]??"")
               :String(FIX.vehicle[f]??"");
    i.classList.toggle("dirty",i.value!==was);});
  el.querySelectorAll("[data-del]").forEach(b=>b.onclick=()=>
    previewFix([{op:"delete_observation",target:b.dataset.del,
                 reason:$("fixWhy").value}]));
  $("fixPreview").onclick=()=>previewFix(collectFix());
  $("fixCancel").onclick=()=>{el.style.display="none";FIX=null;};
  $("fixDelCar").onclick=()=>previewFix([{op:"delete_vehicle",
    target:FIX.key,reason:$("fixWhy").value}]);
  vpaReveal(el);
}
function collectFix(){
  const why=$("fixWhy").value,ops=[],vf={};
  document.querySelectorAll("#fixpanel input[data-v]").forEach(i=>{
    if(i.classList.contains("dirty"))vf[i.dataset.v]=i.value;});
  if(Object.keys(vf).length)ops.push({op:"vehicle",target:FIX.key,fields:vf,reason:why});
  const byObs={};
  document.querySelectorAll("#fixpanel input[data-o]").forEach(i=>{
    if(!i.classList.contains("dirty"))return;
    (byObs[i.dataset.o]=byObs[i.dataset.o]||{})[i.dataset.f]=i.value;});
  Object.entries(byObs).forEach(([id,fields])=>
    ops.push({op:"observation",target:id,fields,reason:why}));
  return ops;
}
async function previewFix(ops){
  if(!ops.length){$("fixdiff").innerHTML=
    '<div class="hint">nothing changed yet - edit a value first</div>';return;}
  const p=await apiCall("/api/edit/preview",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({ops})});
  const box=$("fixdiff");
  if(!p.ok){box.innerHTML=`<div class="warnbox">${p.errors.map(esc).join("<br>")}</div>`;return;}
  if(!p.changes.length){box.innerHTML=
    '<div class="hint">every value already matches - nothing to save</div>';return;}
  const shown=v=>v==null||v===""?'<span class="flat">(blank)</span>':esc(String(v));
  box.innerHTML=`<div class="fixdiff">
    ${p.changes.map(c=>`<div>${c.op==="delete_observation"
        ?`<b>delete</b> the sighting on ${esc(String(c.before.capture_date||c.before.observation&&c.before.observation.capture_date||""))}`
        :c.op==="delete_vehicle"
        ?`<b>delete</b> ${esc(c.label)} and its ${c.obs_count} sighting(s)`
        :`<b>${esc(c.field.replace(/_/g," "))}</b>
          <span class="was">${shown(c.before)}</span>
          <span class="now">${shown(c.after)}</span>`}</div>`).join("")}
    </div>
    ${p.warnings.length?`<div class="warnbox">${p.warnings.map(esc).join("<br>")}</div>`:""}
    ${p.touches_price_history?`<div class="warnbox">This changes recorded price
      history, not just a label. A backup is taken first and this is undoable,
      but check it.</div>`:""}
    <div class="hint">after saving: ${p.expected.vehicles.toLocaleString()} vehicles,
      ${p.expected.observations.toLocaleString()} sightings,
      ${money(p.expected.price_total)} of prices.</div>
    <button class="primary" id="fixSave" style="margin-top:8px">Save ${p.changes.length} change(s)</button>`;
  $("fixSave").onclick=async()=>{
    $("fixSave").disabled=true;$("fixSave").textContent="saving...";
    const r=await apiCall("/api/edit/apply",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ops,confirm:p.fingerprint})});
    if(!r.applied){box.innerHTML=
      `<div class="warnbox">${(r.errors||["failed"]).map(esc).join("<br>")}</div>`;return;}
    box.innerHTML=`<div class="okbox">Saved. ${r.changes.length} change(s) written;
      backup taken first. Undo with
      <code>python -m vpa edit --undo ${esc(r.batch)}</code></div>`;
    $("fixpanel").style.display="none";FIX=null;
    TREE=await fetch("/api/explore/picker").then(x=>x.json());
    const c=sel();fillSelects(c.make,c.model,PICK.length===1?PICK[0]:null);
    await load();clearCar();};
}

function ordinal(n){
  const t=n%100;
  if(t>=11&&t<=13)return n+"th";
  return n+({1:"st",2:"nd",3:"rd"}[n%10]||"th");
}
/* `r.n` is the number of PEERS; the subject makes r.n+1 cars in the ranking.
   Every count on this card is stated against one of those two, never both
   silently - an earlier version said "102 of 124" beside "all 123" and left
   the reader to work out they were the same pool. */
function rankPhrase(r,what){
  if(!r||!r.usable)return `<span class="gated">${esc(r&&r.reason||"not enough data")}</span>`;
  const of=`${r.n+1}${what?" "+what:""}`;
  if(r.percentile==null)
    return `<b>${ordinal(r.rank)} cheapest</b> of ${of}`;
  return `<b>${ordinal(r.rank)} cheapest</b> of ${of} &mdash; dearer than
    ${r.percentile.toFixed(0)}% of them`;
}

function renderCar(){
  const d=CAR,el=$("carbody");
  if(!d){$("carcard").style.display="none";return;}
  $("carcard").style.display="block";
  if(d.ok===false){el.innerHTML=`<div class="warnbox">${esc(d.error)}</div>`;
    $("carspan").textContent="";return;}
  const s=d.subject;
  // refused before any car was looked up (e.g. "pick a trim first")
  if(!s){el.innerHTML=`<div class="warnbox">${esc(d.reason||"")}</div>`;
    $("carspan").textContent=`— ${d.label||""}`;return;}
  $("carspan").innerHTML=`&mdash; ${esc(d.label)} `
    +(s.stored?'<span class="subjtag">from your data</span>'
              :'<span class="subjtag typed">not in your data &mdash; nothing saved</span>')
    +" "+vpaAdLinks(s.url,false,s.stored?s:null);
  if(!d.usable&&!d.comparables.length){
    el.innerHTML=`<div class="warnbox">${esc(d.reason||d.raw&&d.raw.reason||"")}</div>`;return;}

  /* --- headline: raw rank AND mileage-adjusted rank in one sentence. They
     genuinely disagree often enough that showing one alone misleads. */
  let head=`<div class="verdict"><b>${money(s.price)}</b>`
    +(s.odometer_km?` at ${km(s.odometer_km)}`:"")
    +(s.year?`, ${s.year}`:"")+" &mdash; ";
  head+=d.raw.usable?rankPhrase(d.raw,"cars of this trim")
       :`<span class="gated">${esc(d.raw.reason)}</span>`;
  if(d.adjusted&&d.adjusted.rank.usable){
    const rp=d.raw.percentile,ap=d.adjusted.rank.percentile;
    head+=`. Restate every one of them at the same ${km(d.adjusted.at_odo)} and
      this car is <b>${money(d.adjusted.price)}</b>, ${rankPhrase(d.adjusted.rank,"")}`;
    if(rp!=null&&ap!=null&&Math.abs(ap-rp)>=10)
      head+=` &mdash; <b>${ap>rp?"dearer":"cheaper"} than it looks</b> once
        kilometres are taken into account`;
    head+=".";
  }
  head+="</div>";

  /* --- the two strips. Gated on the SAME rule as the rank: a distribution
     drawn from three cars is a picture of nothing, and drawing it while
     refusing to rank against it would contradict the card's own gate. */
  const peerTicks=(d.comparables||[]).map(c=>c.price);
  let strips="";
  if(!d.raw.usable){
    // the reason is already in the headline - repeating it here just made
    // the same sentence appear twice on one card
    strips=`<div class="warnbox">No ranking, no position and no mileage
      adjustment for this car. The comparable ads below need no model, so
      they are still shown.</div>`;
  }else{
  strips=positionStrip({...d.pool,ticks:peerTicks},s.price,
    "where it sits on asking price","#c04040");
  if(d.adjusted)strips+=positionStrip(
    {lo:d.adjusted.pool_lo,hi:d.adjusted.pool_hi,q25:d.adjusted.pool_q25,
     q75:d.adjusted.pool_q75,median:d.adjusted.pool_median},
    d.adjusted.price,"and once every car is restated at the same odometer","#3060a0");
  if(d.odo_percentile!=null)
    strips+=`<div class="hint">Its odometer is higher than
      <b>${d.odo_percentile.toFixed(0)}%</b> of the same cars. A low price
      percentile at a high odometer percentile is not a bargain.</div>`;
  }

  /* --- expected at this odometer */
  let exp="";
  if(d.expected){
    const g=d.expected;
    exp=`<div class="kv">
      <div><span class="k">cars like this at ${km(s.odometer_km)} ask about</span>
        <b>${money(g.price)}</b></div>
      <div><span class="k">this one asks</span>
        <b class="${g.gap>0?"down":"up"}">${g.gap>0?"+":""}${money(g.gap)}
        (${g.gap_pct>0?"+":""}${g.gap_pct}%)</b></div>
      <div><span class="k">cars sitting further above the curve</span>
        <b>${g.above} of ${g.of}</b></div>
      <div><span class="k">fit</span><span class="flat">${d.fit.pct_per_10k_km}%
        per 10,000 km, n=${g.n}</span></div></div>`;
  }else{
    exp=`<div class="kv"><div><span class="k">expected price for its
      kilometres</span><span class="gated">not available</span></div></div>`;
  }

  /* --- like-for-like */
  const sp=d.splits;
  const splitRow=(lab,o)=>`<div><span class="k">${lab}${o.value?" ("+esc(o.value)+")":""}</span>`
    +(o.median!=null?`<b>${money(o.median)}</b><span class="flat">n=${o.n}</span>`
      :`<span class="gated">${esc(o.reason)}</span>`)+`</div>`;
  const like=`<div class="kv">
    <div><span class="k">the other ${d.pool.n} of this trim</span><b>${money(d.pool.median)}</b></div>
    ${splitRow("same seller type",sp.seller)}
    ${splitRow("same state",sp.state)}
    ${splitRow("same model year",sp.year)}
    <div><span class="k">every ${esc(d.line_check.label)}</span>
      <b>${money(d.line_check.median)}</b>
      ${d.line_check.rank.percentile!=null
        ?`<span class="flat">cheaper than ${(100-d.line_check.rank.percentile).toFixed(0)}%
          of them</span>`:""}</div>
    </div>`;

  /* --- comparables */
  const cmp=`<table class="cmp"><thead><tr><th>year</th><th class="num">price</th>
    <th class="num">odometer</th><th>state</th><th>seller</th><th>last seen</th>
    <th></th></tr></thead><tbody>
    ${d.comparables.map(c=>`<tr class="${c.dup?"dupe":""}">
      <td>${c.year||""}</td><td class="num">${money(c.price)}</td>
      <td class="num">${km(c.odometer_km)}</td><td>${esc(c.state||"")}</td>
      <td>${esc(c.seller_type||"")}</td><td>${c.day}</td>
      <td>${vpaAdLinks(c.url,false,c)}
        ${c.dup?dupTag(c.key):""}</td>
    </tr>`).join("")||'<tr><td colspan="7" class="flat">none</td></tr>'}</tbody></table>
    <div class="hint">The ${d.band.n} nearest on kilometres${
      d.band.basis==="year"?" (matched on year - this car has no odometer)":""},
      median ${money(d.band.median)}${d.band.vs!=null
        ?`, this car ${d.band.vs>0?"+":""}${d.band.vs}% against them`:""}.</div>`;

  /* --- this listing's own history, then the full over-time panel */
  let hist="";
  if(d.history){
    const h=d.history;
    hist=h.clash
      ?`<span class="tag warn">merged identity</span> two different cars share
         this identity, so no price change can be read from it`
      :h.moved?`was ${money(h.first_price)} on ${h.first_day}, now
         ${money(h.price)} &mdash; ${pct(h.change)} over at least ${h.at_least_days} days`
      :h.days>1?`asking price unchanged across ${h.days} capture days, at least
         ${h.at_least_days} days`
      :`seen once, on ${h.first_day} &mdash; no history yet`;
    hist=`<div class="kv"><div><span class="k">this listing</span>${hist}</div></div>`
      +overTime(h);
  }

  /* --- the user's own number: visibly not an engine figure */
  let mine="";
  if(TARGET&&s.price)mine=`<div class="kv"><div><span class="k mine">your target
    ${money(TARGET)}</span><b class="mine">${s.price>TARGET?"+":""}${money(s.price-TARGET)}
    (${((s.price/TARGET-1)*100).toFixed(1)}%)</b>
    <span class="flat">your number, not from the data</span></div></div>`;

  el.innerHTML=head+`<div class="grid2">
     <div>${strips}</div>
     <div><div class="mini-h">expected for its kilometres</div>${exp}
       <div class="mini-h" style="margin-top:12px">like for like</div>${like}</div>
    </div>
    ${hist}${mine}
    <div class="mini-h" style="margin-top:14px">nearest comparable ads</div>${cmp}
    <div class="hint">${esc(d.basis)}. Pool captured ${d.pool.first} to
      ${d.pool.last}${d.pool.stale?` &mdash; <b>${d.pool.days_since} days ago</b>`:""}.
      ${d.band.dup_in_pool?`<b>${d.band.dup_in_pool}</b> of these look like duplicate
        listings (same price and odometer under two identities), so the true car
        count is lower.`:""}
      ${d.unavailable.map(esc).join(" ")}
      ${d.caveats.length?d.caveats.map(esc).join(" &middot; "):""}</div>
    <div class="hint"><b>${esc(d.footer)}</b></div>`;
}

async function openCar(opts){
  const c=sel();
  // A car is always judged against ITS OWN trim, never against the pool it
  // happened to be listed in. With several trims pooled the selection has no
  // single variant, so the clicked row carries its own.
  const va=opts.variant||(PICK.length===1?PICK[0]:"");
  delete opts.variant;
  const q=new URLSearchParams({make:c.make,model:c.model,variant:va});
  Object.entries(opts).forEach(([k,v])=>{if(v!=null&&v!=="")q.set(k,v);});
  const got=await vpaLatest("car","/api/explore/car?"+q);
  if(!got)return;                        // another car was clicked meanwhile
  CAR=got;
  renderCar();renderVehicles();
  const u=new URLSearchParams(location.search);
  ["car","price","odo","year","seller","state","note"].forEach(k=>u.delete(k));
  Object.entries(opts).forEach(([k,v])=>{if(v!=null&&v!=="")u.set(k,v);});
  u.set("make",c.make);u.set("model",c.model);
  // the URL keeps the POOLED selection, not the one trim being judged, so
  // reloading a bookmarked verdict restores the page you were looking at
  u.delete("variant");PICK.forEach(v=>u.append("variant",v));
  history.replaceState(null,"","/explore?"+u);
  vpaReveal($("carcard"));
}
function clearCar(){CAR=null;TARGET=null;FIX=null;
  $("fixpanel").style.display="none";renderCar();renderVehicles();
  const u=new URLSearchParams(location.search);
  ["car","price","odo","year","seller","state","note"].forEach(k=>u.delete(k));
  history.replaceState(null,"","/explore?"+u);}
$("fixBtn").onclick=openFix;
$("carClose").onclick=clearCar;
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&CAR)clearCar();});
/* ---------- find one car from its saved ad page ------------------------
   Read-only end to end: the file is parsed server-side and matched against
   what is already stored. Nothing is imported - uploading an ad to ask "how
   does this compare" must not quietly add a listing. */
function findMsg(html,tone){
  $("findmsg").innerHTML=html?`<div class="${tone||"idcard"}">${html}</div>`:"";
}
const CSS_COLOURS=new Set(["black","white","silver","grey","gray","red","blue",
  "green","yellow","orange","brown","beige","gold","purple","maroon","navy",
  "ivory","tan","pink","teal","olive"]);
function dot(v){
  const w=String(v||"").toLowerCase();
  return `<span class="swatchdot" style="background:${
    CSS_COLOURS.has(w)?w:"#b9c2d0"}"></span>`;
}
async function identify(file){
  if(!file)return;
  if(!/\.html?$/i.test(file.name)){
    findMsg("That is not a saved web page. Use <b>Ctrl+S</b> on the ad and "
      +"pick the .htm file it saves.","warnbox");return;}
  findMsg(`Reading <b>${esc(file.name)}</b>...`);
  let r;
  try{
    const text=await file.text();
    r=await fetch("/api/explore/identify",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:file.name,text})}).then(x=>x.json());
  }catch(e){findMsg("Could not read that file: "+esc(e),"warnbox");return;}
  if(!r.ok){findMsg(esc(r.error),"warnbox");return;}
  await showFound(r);
}
/* Find from a pasted link: matched against the database's own records only -
   the browser never opens the advert. */
async function findByLink(link){
  link=(link||"").trim();
  if(!link){$("findLink").focus();return;}
  findMsg(`Looking for <b>${esc(link.length>70?link.slice(0,67)+"...":link)}</b> in your data...`);
  let r;
  try{
    r=await fetch("/api/explore/link",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({link})}).then(x=>x.json());
  }catch(e){findMsg("Could not look that up: "+esc(e),"warnbox");return;}
  if(!r.ok){findMsg(esc(r.error),"warnbox");return;}
  if(!r.in_data){
    const f=r.found||{};
    findMsg(`${f.listing_id?`Advert <b>${esc(f.listing_id)}</b>`:"That advert"} is not in your captures.
      <div class="hint" style="margin-top:6px">A link carries no price or kilometres, so
      there is nothing to score it on. Save the ad's page (<b>Ctrl+S</b>) and use
      <b>From a saved page</b> to rank it against its trim, or <b>Type one I've seen</b>.
      ${f.url?` ${vpaAdLinks(f.url,false)}`:""}</div>`,"warnbox");
    vpaReveal($("findmsg"));
    return;
  }
  await showFound(r);
}
/* Point the picker at one car's trim. Setting the dropdowns alone is not a
   selection - load() reads PICK - so without this a found car was judged
   against "all trims" and its card refused as too broad. */
function selectTrim(f){
  fillSelects(f.make,f.model,f.variant);
  PICK=f.variant&&$("va").value===f.variant?[f.variant]:[];
  if(typeof resetChips==="function")resetChips();
}
async function showFound(r){
  const f=r.found;
  const label=[f.make,f.model,f.variant].filter(Boolean).join(" ");
  const bits=[f.year,money(f.price),f.odometer_km?km(f.odometer_km):null,
              f.state,f.seller_type].filter(Boolean).join(" &middot; ");
  // colour comes ONLY from a details page, so it is shown as read off the
  // file and never presented as something the database knows. Exterior and
  // interior are stated separately because the ad states them separately.
  // Manufacturer paint names are not CSS colours - "Kinetic" is a Ford blue -
  // so the swatch is only filled for words a browser actually understands,
  // rather than silently rendering nothing.
  const col=f.colour?`${dot(f.colour)}<b>${esc(f.colour)}</b>${
    f.interior_colour?` outside, ${esc(f.interior_colour)} inside`:""}
    &mdash; read from the ad; colour is not stored in your data`:"";

  if(r.in_data){
    findMsg(`Found it: <b>${esc(label)}</b><br>${bits}<br>${col}
      <div class="hint" style="margin-top:6px">Matched on ${esc(r.basis)}.
      Seen on ${r.stored.days} capture day(s), ${r.stored.first_day}
      to ${r.stored.last_day}${r.stored.days>1
        ?` &mdash; the card below lists each sighting and which import recorded
           it, so you can check that yourself`:""}.
      ${(r.adds||[]).length?`The file also states its
        ${r.adds.map(a=>a==="seller_type"?"seller":a).join(" and ")}, which
        your captures did not record.`:""}</div>`);
    // point the page at that car's trim, then open its verdict card
    selectTrim(f);
    await load();
    await openCar({car:r.key});
  }else{
    // not captured - fall back to scoring it as a hypothetical, prefilled
    $("sPrice").value=f.price!=null?f.price:"";
    $("sOdo").value=f.odometer_km!=null?f.odometer_km:"";
    $("sYear").value=f.year||"";
    $("sState").value=f.state||"";
    $("sSeller").value=f.seller_type||"";
    $("sNote").value=f.colour?f.colour+(f.title?" - "+f.title:""):(f.title||"");
    findMsg(`<b>${esc(label)}</b> is not in your captures.<br>${bits}<br>${col}
      <div class="hint" style="margin-top:6px">${r.trim_known
        ?"Its trim IS in your data, so it can still be scored against it - the "
         +"form below is filled in from the file."
        :(r.line_known
          ?"That exact trim has never been captured, so there is nothing to "
           +"rank it against yet. Its model line is in your data."
          :"Neither that trim nor its model line is in your data yet.")}
        Nothing was imported - use the <b>Import</b> page to add it.</div>`);
    $("seencard").style.display="block";
    if(r.trim_known){
      selectTrim(f);
      await load();
      $("sGo").click();
    }
  }
  vpaReveal($("findmsg"));
}
$("findBtn").onclick=()=>{$("findFile").value="";$("findFile").click();};
$("linkForm").onsubmit=e=>{e.preventDefault();findByLink($("findLink").value);};
// pasting a link finds it straight away - no button press needed
$("findLink").addEventListener("paste",e=>{
  const t=(e.clipboardData||window.clipboardData).getData("text");
  if(t&&/^\s*\S+\s*$/.test(t)){e.preventDefault();$("findLink").value=t.trim();findByLink(t);}});
$("findFile").onchange=e=>identify(e.target.files[0]);
["dragenter","dragover"].forEach(ev=>$("findbar").addEventListener(ev,e=>{
  e.preventDefault();$("findbar").classList.add("drag");}));
["dragleave","drop"].forEach(ev=>$("findbar").addEventListener(ev,e=>{
  e.preventDefault();$("findbar").classList.remove("drag");}));
$("findbar").addEventListener("drop",e=>{
  if(e.dataTransfer.files&&e.dataTransfer.files[0]){identify(e.dataTransfer.files[0]);return;}
  const u=(e.dataTransfer.getData("text/uri-list")||e.dataTransfer.getData("text/plain")||"")
    .split(/\r?\n/).find(x=>x&&!x.startsWith("#"));
  if(u){$("findLink").value=u.trim();findByLink(u);}});

$("seenBtn").onclick=()=>{const c=$("seencard");
  c.style.display=c.style.display==="none"?"block":"none";
  if(c.style.display==="block")$("sPrice").focus();};
$("sGo").onclick=()=>{
  const t=$("sTarget").value.trim();
  TARGET=t?Number(String(t).replace(/[^0-9.]/g,""))||null:null;
  openCar({price:$("sPrice").value,odo:$("sOdo").value,year:$("sYear").value,
           seller:$("sSeller").value,state:$("sState").value,
           note:$("sNote").value});};
["sPrice","sOdo","sYear","sState","sTarget"].forEach(id=>
  $(id).onkeydown=e=>{if(e.key==="Enter")$("sGo").click();});

/* ---------- price projection: history, then the forecast with its band ----
   The workbook's Forecasts chart. History is solid, the projection dashed and
   shaded across its 80% interval - the interval IS the message, so it is
   drawn rather than left in a table. */
function forecastChart(d){
  const el=$("fcchart"),h=d.bymonth||[],f=d.forecast,pts=f.points||[];
  $("fcspan").textContent=pts.length
    ? `— ${esc(f.method.replace(/_/g," "))}, ${f.interval_level}% interval`
    : `— ${esc(f.method.replace(/_/g," "))}`;
  if(!h.length){el.innerHTML='<div class="flat">no monthly history yet</div>';return;}
  if(!pts.length){
    el.innerHTML=`<div class="warnbox" style="margin:0 0 8px">No projection:
      ${esc(f.verdict||"not enough history")}. The chart returns as soon as
      there are 3 monthly buckets and 8 prices.</div>`;
    return;}
  const cats=h.map(r=>r.d).concat(pts.map(p=>p.d));
  const n=cats.length,split=h.length-1;      // last history point joins both
  const W=560,H=250,L=64,R=16,T=14,B=32;
  const vals=h.map(r=>r.median).concat(pts.flatMap(p=>[p.v,p.lo,p.hi]))
    .filter(v=>v!=null);
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(lo===hi){lo*=0.95;hi*=1.05;}
  const pad=(hi-lo)*0.08;lo-=pad;hi+=pad;
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo||1));
  const grid=vpaTicks(lo,hi,5).map(v=>{return `
    <line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
  // the band starts at the last observed point (zero width there), so it
  // visibly fans out from what actually happened
  const last=h[h.length-1].median;
  const up=[`M${x(split).toFixed(1)},${y(last).toFixed(1)}`]
    .concat(pts.map((p,i)=>`L${x(split+1+i).toFixed(1)},${y(p.hi).toFixed(1)}`));
  const dn=pts.slice().reverse().map((p,i)=>
    `L${x(n-1-i).toFixed(1)},${y(p.lo).toFixed(1)}`);
  const band=up.join("")+dn.join("")+`L${x(split).toFixed(1)},${y(last).toFixed(1)}Z`;
  const hline=h.map((r,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(r.median).toFixed(1)}`).join("");
  const fline=`M${x(split).toFixed(1)},${y(last).toFixed(1)}`
    +pts.map((p,i)=>`L${x(split+1+i).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const hdots=h.map((r,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(r.median).toFixed(1)}"
    r="3.4" fill="#c04040"><title>${r.d}: observed median ${money(r.median)} (n=${r.n})</title></circle>`).join("");
  const fdots=pts.map((p,i)=>`<circle cx="${x(split+1+i).toFixed(1)}" cy="${y(p.v).toFixed(1)}"
    r="3.6" fill="#fff" stroke="#3060a0" stroke-width="2"><title>${p.d}: projected ${money(p.v)}
 (${f.interval_level}% interval ${money(p.lo)} to ${money(p.hi)})</title></circle>`).join("");
  const divider=`<line x1="${x(split).toFixed(1)}" y1="${T}" x2="${x(split).toFixed(1)}"
    y2="${H-B}" stroke="#c3c8d2" stroke-dasharray="3 3"/>
    <text x="${(x(split)+4).toFixed(1)}" y="${T+10}" class="ax">projection &rarr;</text>`;
  const ticks=cats.map((c,i)=>(n<=8||i%Math.ceil(n/8)===0)
    ?`<text x="${x(i).toFixed(1)}" y="${H-9}" text-anchor="middle" class="ax">${c}</text>`:"").join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="monthly median history and forecast with interval">${grid}
    <path d="${band}" fill="#3060a0" opacity=".12"/>${divider}
    <path d="${hline}" fill="none" stroke="#c04040" stroke-width="2"/>
    <path d="${fline}" fill="none" stroke="#3060a0" stroke-width="2" stroke-dasharray="6 4"/>
    ${hdots}${fdots}${ticks}</svg>`;
}

/* ---------- deeper analysis: one tab per question ---------- */
/* The projection and the per-car price changes used to be a card of their
   own below the tabs - the only analysis on the page that was not a tab. */
function fcTab(d){
  const f=d.forecast,m=d.mileage;
  return `<div class="act"><div class="panel" style="flex:1.4 1 420px">
      <div class="ptitle">price projection <span id="fcspan"></span></div>
      <div id="fcchart"></div></div>
    <div class="panel"><div class="ptitle">how it was worked out</div><div class="kv">
     <div><span class="k">method</span><b>${esc(f.method.replace(/_/g," "))}</b></div>
     <div><span class="k">${esc(f.verdict)}</span></div>
     ${f.points.length?f.points.map(p=>`<div><span class="k">${p.d}</span>
        <b>${money(p.v)}</b><span class="flat">${money(p.lo)} – ${money(p.hi)}
        (${f.interval_level}%)</span></div>`).join(""):""}
     <div><span class="k">mileage adjustment</span>${m.usable
        ?`<span class="up">${m.pct_per_10k_km.toFixed(1)}% / 10,000 km</span>
          <span class="flat">n=${m.n}</span>`
        :`<span class="flat">not usable — ${esc(m.reason)}</span>`}</div>
    </div></div></div>`;
}
function movesTab(d){
  return `<div class="ptitle">cars re-seen at a different price
      <span>&mdash; ${d.mover_total?`${d.movers.length} of ${d.mover_total}, biggest first`
        :"none yet"}</span></div>
    <div class="lst">${d.movers.length?d.movers.map(v=>
    `<div><span class="nm" title="${esc(v.title)}">${v.year||""} ${esc(v.variant||"")}</span>
      ${pct(v.change)}<span class="flat">${money(v.first_price)} → ${money(v.price)}</span>
      <span class="flat">${v.first_day} → ${v.last_day}</span></div>`).join("")
    :'<div class="flat">no vehicle here has been re-seen at a different price yet</div>'}</div>
    <div class="hint note">One car, its first price against its latest. Identities that
      hold two prices on one day are merged cars and are left out.</div>`;
}
let TAB="month";
const TABS={month:monthTab,fc:fcTab,moves:movesTab,spread:spreadTab,stand:standTab,
            mile:mileTab,turn:turnTab,who:whoTab,trims:trimTab};
let LIVEONLY=false, ZEND="low";
function renderTab(){
  const el=$("tabbody");
  if(!DATA||DATA.empty){el.innerHTML="";return;}
  el.innerHTML=TABS[TAB](DATA);
  if(TAB==="fc")forecastChart(DATA);
  if(TAB==="trims")document.querySelectorAll("#tabbody tr.pick").forEach(tr=>tr.onclick=()=>{
    pickOnly(tr.dataset.v);});
  // the Turnover tab holds two clickable tables: cars that left (judge one
  // against the trim it left) and the trim comparison (select that trim)
  // a click on an advert link opens the advert only, not the car's card too
  if(TAB==="turn")document.querySelectorAll("#tabbody tr.pick").forEach(tr=>tr.onclick=e=>{
    if(e.target.closest("a"))return;
    if(tr.dataset.v!=null){pickOnly(tr.dataset.v);return;}
    const k=tr.dataset.k;
    if(CAR&&CAR.subject&&CAR.subject.key===k)clearCar();else openCar({car:k});});
  if(TAB==="mile")document.querySelectorAll("circle.spt").forEach(c=>c.onclick=()=>{
    const k=c.dataset.k;
    if(CAR&&CAR.subject&&CAR.subject.key===k)clearCar();else openCar({car:k});});
  if(TAB==="stand"){
    const ze=$("zend"),zl=$("zlive");
    if(ze)ze.onchange=()=>{ZEND=ze.value;renderTab();};
    if(zl)zl.onchange=()=>{LIVEONLY=zl.value==="1";renderTab();};
    document.querySelectorAll("#tabbody tr.pick").forEach(tr=>tr.onclick=e=>{
      if(e.target.closest("a"))return;
      const k=tr.dataset.k;
      if(CAR&&CAR.subject&&CAR.subject.key===k)clearCar();else openCar({car:k});});
  }
}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{
  TAB=b.dataset.t;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===b));
  renderTab();});

/* monthly medians. NOTE the basis differs from the capture-day chart above:
   here each vehicle counts once per month (analyze.prep), which is what
   `vpa report` and the forecast use, so the two charts can differ slightly. */
function monthTab(d){
  const m=d.bymonth;
  if(!m.length)return '<div class="flat">no priced listings</div>';
  const adj=d.adjusted||[];
  const W=560,H=230,L=62,R=14,T=12,B=30,n=m.length;
  const vals=m.flatMap(r=>[r.q25,r.q75,r.median]).concat(adj.map(a=>a.median))
    .filter(v=>v!=null);
  let lo=Math.min(...vals),hi=Math.max(...vals);
  if(lo===hi){lo*=0.95;hi*=1.05;}
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo||1));
  const grid=vpaTicks(lo,hi,5).map(v=>{return `
    <line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
  const band=m.map((r,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(r.q75).toFixed(1)}`).join("")
    +m.slice().reverse().map((r,i)=>`L${x(n-1-i).toFixed(1)},${y(m[n-1-i].q25).toFixed(1)}`).join("")+"Z";
  const line=m.map((r,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(r.median).toFixed(1)}`).join("");
  const aline=adj.length===n
    ?`<path d="${adj.map((a,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(a.median).toFixed(1)}`).join("")}"
        fill="none" stroke="#3060a0" stroke-width="1.8" stroke-dasharray="5 4"/>`:"";
  const dots=m.map((r,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(r.median).toFixed(1)}" r="3.4"
    fill="#c04040"><title>${r.d}: ${money(r.median)} (n=${r.n})</title></circle>`).join("");
  const ticks=m.map((r,i)=>`<text x="${x(i).toFixed(1)}" y="${H-9}" text-anchor="middle"
    class="ax">${r.d}</text>`).join("");
  return `<div class="cols">
   <div><div class="mini-h">median by month${adj.length?" (dashed = mileage-adjusted)":""}</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="median price by month">${grid}
      <path d="${band}" fill="#c04040" opacity=".13"/>
      <path d="${line}" fill="none" stroke="#c04040" stroke-width="2"/>${aline}${dots}${ticks}</svg>
    <div class="hint note">Each vehicle counts once per month here, so a car re-seen
     three times in July does not weigh three times. The capture-day chart
     above counts every sighting - that is why the two can differ.</div></div>
   <div><div class="mini-h">the numbers behind it</div>
    <table><thead><tr><th>month</th><th class="num">n</th><th class="num">median</th>
     <th class="num">25th</th><th class="num">75th</th>
     <th class="num">vs prev</th></tr></thead><tbody>
    ${m.map((r,i)=>{const p=i?m[i-1].median:null;
      const ch=(p&&r.median!=null)?(r.median-p)/p*100:null;
      return `<tr><td>${r.d}</td><td class="num">${r.n}</td>
      <td class="num">${money(r.median)}</td><td class="num">${money(r.q25)}</td>
      <td class="num">${money(r.q75)}</td><td class="num">${pct(ch)}</td></tr>`;}).join("")}
    </tbody></table></div></div>`;
}

/* the distribution with a bell curve fitted to it.
   Fitted on LOG price (a log-normal), because used-car prices have a floor
   at zero and a long tail of rare cars - a plain normal would run its left
   tail below $0. The curve is drawn OVER the real histogram, and the panel
   states how many cars actually sit in each tail against how many the curve
   predicts, so its own misfit is visible rather than implied. */
function spreadTab(d){
  const hh=d.hist,c=d.curve;
  if(!hh)return '<div class="flat">too few prices here to show a distribution</div>';
  if(!c||!c.usable)
    return `<div class="warnbox">${esc(c&&c.reason||"not enough prices")} &mdash;
      the histogram below is still real data.</div>`+plainHist(hh);
  const W=580,H=270,L=52,R=16,T=18,B=40;
  const binW=hh.bins[0].hi-hh.bins[0].lo;
  // scale the density to counts so curve and bars share one y axis
  const curveCounts=c.y.map(v=>v*c.n*binW);
  const mx=Math.max(Math.max(...hh.bins.map(b=>b.n)),Math.max(...curveCounts))||1;
  const x0=Math.min(hh.bins[0].lo,c.x[0]);
  const x1=Math.max(hh.bins[hh.bins.length-1].hi,c.x[c.x.length-1]);
  const X=v=>L+(W-L-R)*(v-x0)/(x1-x0||1);
  const Y=n=>T+(H-T-B)*(1-n/mx);
  const grid=[0,.5,1].map(f=>{const v=Math.round(mx*f);return `
    <line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W-R}" y2="${Y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(Y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${v}</text>`;}).join("");
  const bars=hh.bins.map(b=>{const w=Math.max(1,X(b.hi)-X(b.lo)-1.5);
    return `<rect x="${X(b.lo).toFixed(1)}" y="${Y(b.n).toFixed(1)}" width="${w.toFixed(1)}"
      height="${(Y(0)-Y(b.n)).toFixed(1)}" fill="#7d9bc1" opacity=".75" rx="1.5">
      <title>${money(b.lo)} to ${money(b.hi)}: ${b.n} car(s)</title></rect>`;}).join("");
  const path=c.x.map((v,i)=>`${i?"L":"M"}${X(v).toFixed(1)},${Y(curveCounts[i]).toFixed(1)}`).join("");
  // shade beyond two standard deviations - that is the "standout" territory
  const m2=c.marks.find(m=>m.sd===-2),p2=c.marks.find(m=>m.sd===2);
  const tails=`<rect x="${L}" y="${T}" width="${Math.max(0,X(m2.price)-L).toFixed(1)}"
      height="${(H-B-T).toFixed(1)}" fill="#2e7d32" opacity=".07"/>
    <rect x="${X(p2.price).toFixed(1)}" y="${T}" width="${Math.max(0,W-R-X(p2.price)).toFixed(1)}"
      height="${(H-B-T).toFixed(1)}" fill="#a03030" opacity=".06"/>`;
  const marks=c.marks.map(m=>{
    const lab=m.sd===0?"median":(m.sd>0?"+":"")+m.sd+"σ";
    return `<line x1="${X(m.price).toFixed(1)}" y1="${T}" x2="${X(m.price).toFixed(1)}"
      y2="${H-B}" stroke="${m.sd===0?"#c04040":"#c3c8d2"}"
      stroke-width="${m.sd===0?2:1}" ${m.sd===0?"":'stroke-dasharray="3 3"'}/>
      <text x="${X(m.price).toFixed(1)}" y="${T-6}" text-anchor="middle" class="ax">${lab}</text>
      <text x="${X(m.price).toFixed(1)}" y="${H-24}" text-anchor="middle" class="ax">${money(m.price)}</text>`;
    }).join("");
  // the car on the verdict card, if there is one
  let subj="";
  if(CAR&&CAR.subject&&CAR.subject.price){
    const sx=X(CAR.subject.price),z=CAR.subject.price>0
      ?((Math.log(CAR.subject.price)-c.mu)/c.sigma):null;
    if(sx>=L&&sx<=W-R)subj=`<line x1="${sx.toFixed(1)}" y1="${T}" x2="${sx.toFixed(1)}"
      y2="${H-B}" stroke="#7048a0" stroke-width="2.5"/>
      <text x="${sx.toFixed(1)}" y="${H-10}" text-anchor="middle" class="ax"
        style="fill:#7048a0;font-weight:700">this car ${z!=null?(z>0?"+":"")+z.toFixed(2)+"σ":""}</text>`;
  }
  // the excess is a FIT-QUALITY number, not a verdict: more cars than the
  // curve predicts below the middle means more bargains, above it means more
  // premium outliers, and neither is good or bad. Bolded when large, never
  // coloured green/red.
  const tailRows=c.marks.filter(m=>m.sd!==0).map(m=>{
    const dir=m.sd<0?"below":"above";
    const excess=m.beyond-m.expected;
    return `<tr><td>${(m.sd>0?"+":"")+m.sd}&sigma; (${dir} ${money(m.price)})</td>
      <td class="num"><b>${m.beyond}</b></td><td class="num">${m.expected}</td>
      <td class="num"${Math.abs(excess)>=2?' style="font-weight:600"':""}>${
        excess>0?"+":""}${excess.toFixed(1)}</td></tr>`;}).join("");
  return `<div class="cols">
    <div><div class="mini-h">${c.n} cars, one price each &mdash; bars are real,
       the line is a bell curve fitted to them</div>
     <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
       aria-label="price distribution with a fitted bell curve">${grid}${tails}${bars}
       <path d="${path}" fill="none" stroke="#c04040" stroke-width="2.5"/>
       ${marks}${subj}</svg>
     <div class="hint">Green band = cheaper than 2&sigma; below the middle,
      red = dearer than 2&sigma; above. Those are the standouts; the
      <b>Standouts</b> tab lists them.</div></div>
    <div><div class="mini-h">how well the curve actually fits</div>
     <table><thead><tr><th>tail</th><th class="num">cars there</th>
      <th class="num">curve says</th><th class="num">excess</th></tr></thead>
      <tbody>${tailRows}</tbody></table>
     <div class="hint note">Skew of the raw prices is
      <b>${c.raw_skew.toFixed(2)}</b>; on log prices it falls to
      <b>${c.log_skew.toFixed(2)}</b> (0 would be a perfect bell), which is why
      the curve is fitted in log space. A positive excess on the right means
      real cars out-tail any tidy curve &mdash; treat &sigma; as a ruler, not
      as gospel.</div>
     <div class="hint">Middle half runs ${money(hh.q25)} to ${money(hh.q75)}.
      One price per car, its latest sighting &mdash; the capture-day chart
      above counts every sighting instead.</div></div></div>`;
}
function plainHist(hh){
  const W=560,H=200,L=52,R=14,T=14,B=30;
  const mx=Math.max(...hh.bins.map(b=>b.n))||1;
  const x0=hh.bins[0].lo,x1=hh.bins[hh.bins.length-1].hi;
  const X=v=>L+(W-L-R)*(v-x0)/(x1-x0||1),Y=n=>T+(H-T-B)*(1-n/mx);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
    ${hh.bins.map(b=>`<rect x="${X(b.lo).toFixed(1)}" y="${Y(b.n).toFixed(1)}"
      width="${Math.max(1,X(b.hi)-X(b.lo)-1.5).toFixed(1)}"
      height="${(Y(0)-Y(b.n)).toFixed(1)}" fill="#7d9bc1" rx="1.5"/>`).join("")}
    <text x="${L}" y="${H-8}" class="ax">${money(x0)}</text>
    <text x="${W-R}" y="${H-8}" text-anchor="end" class="ax">${money(x1)}</text></svg>`;
}

/* the cars in the tails, which is the point of fitting a curve at all.
   Two z-scores per car: on asking price, and on the mileage-adjusted price.
   A car can be 1.6 sigma cheap purely because it has done 290,000 km - the
   adjusted column is what tells those apart from a genuine standout. */
function standTab(d){
  const c=d.curve,rows=d.standouts||[],dist=d.distribution||{};
  if(!c||!c.usable)
    return `<div class="warnbox">${esc(c&&c.reason||"not enough prices to rank standouts")}</div>`;
  let list=rows.filter(r=>r.z!=null);
  if(LIVEONLY)list=list.filter(r=>r.live);
  const low=list.slice(0,14),high=list.slice(-14).reverse();
  const show=ZEND==="low"?low:(ZEND==="high"?high:list.slice(0,40));
  const zc=z=>z==null?'<span class="flat">&mdash;</span>'
    :`<b class="${z<=-1?"up":(z>=1?"down":"flat")}">${z>0?"+":""}${z.toFixed(2)}</b>`;
  const body=show.map(r=>`<tr class="pick" data-k="${esc(r.key)}">
     <td class="num">${zc(r.z)}</td><td class="num">${zc(r.z_adj)}</td>
     <td class="num">${money(r.price)}</td>
     <td class="num">${r.odometer_km==null?"":km(r.odometer_km)}</td>
     <td>${r.year||""}</td><td>${esc(r.state||"")}</td>
     <td>${esc(r.seller_type||"")}</td>
     <td>${r.live?'<span class="tag good">in latest capture</span>'
          :`<span class="flat">${r.day}</span>`}</td>
     <td>${vpaAdLinks(r.url,!r.live,r)}
       ${r.dup?dupTag(r.key):""}</td>
    </tr>`).join("")||'<tr><td colspan="9" class="flat">nothing matches</td></tr>';
  const dupWarn=dist.dup_extra?`<div class="warnbox"><b>${dist.dup_extra}</b> of
    these ${dist.pool_n} cars are the same car under two identities (identical
    price and odometer), so some will appear twice below and the counts are
    inflated by that much. It happens when one capture was pasted text and
    another was saved HTML - run <code>python -m vpa verify</code> for the
    whole-database figure.</div>`:"";
  return dupWarn+`<div class="row" style="margin-bottom:8px">
    <div><label>which end</label><select id="zend">
      <option value="low"${ZEND==="low"?" selected":""}>cheapest (bargain end)</option>
      <option value="high"${ZEND==="high"?" selected":""}>dearest (premium end)</option>
      <option value="all"${ZEND==="all"?" selected":""}>both, cheapest first</option>
    </select></div>
    <div><label>which captures</label><select id="zlive">
      <option value="0"${LIVEONLY?"":" selected"}>every capture (all time)</option>
      <option value="1"${LIVEONLY?" selected":""}>latest capture only${
        dist.live_day?" ("+dist.live_day+")":""}</option>
    </select></div>
    <div class="flat" style="font-size:12px;padding-bottom:6px">
      ${list.length} of ${dist.total} cars${LIVEONLY?" seen on "+dist.live_day:""}</div>
   </div>
   <table><thead><tr>
     <th class="num">&sigma; on price</th><th class="num">&sigma; adjusted</th>
     <th class="num">price</th><th class="num">odometer</th><th>year</th>
     <th>state</th><th>seller</th><th>last seen</th><th></th>
   </tr></thead><tbody>${body}</tbody></table>
   <div class="hint">Click a row to judge that car in full.</div>
   <div class="hint note" data-label="What the two &sigma; columns mean"><b>&sigma; on
    price</b> is distance from the middle of the bell curve; <b>&sigma;
    adjusted</b> restates every car at the same odometer first, so a car that
    is only cheap because it has done 300,000 km drops back towards zero.
    Anything past &minus;2&sigma; on the adjusted column is a genuine outlier.</div>
   <div class="hint note"><b>"In latest capture" is not "still for sale."</b> It
    means the car appeared in this selection's most recent capture
    (${esc(dist.live_day||"-")}). A car missing from it may have sold, or may
    simply not have been in the search you captured that day. Only
    ${dist.live_n} of ${dist.pool_n} cars here were in that capture.</div>`;
}

/* price vs odometer, with the fitted curve when the fit was allowed */
function mileTab(d){
  const pts=d.scatter||[],m=d.mileage;
  if(!pts.length)return '<div class="flat">no listing here has both a price and an odometer</div>';
  const W=560,H=250,L=62,R=14,T=12,B=34;
  const xs=pts.map(p=>p.odo),ys=pts.map(p=>p.price);
  let x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  if(x0===x1){x0*=0.95;x1*=1.05;} if(y0===y1){y0*=0.95;y1*=1.05;}
  const X=v=>L+(W-L-R)*(v-x0)/(x1-x0||1);
  const Y=v=>T+(H-T-B)*(1-(v-y0)/(y1-y0||1));
  const grid=vpaTicks(y0,y1,5).map(v=>{return `
    <line x1="${L}" y1="${Y(v).toFixed(1)}" x2="${W-R}" y2="${Y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(Y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
  const xt=[0,.5,1].map(f=>{const v=x0+(x1-x0)*f;return `
    <text x="${X(v).toFixed(1)}" y="${H-10}" text-anchor="middle" class="ax">${km(v)}</text>`;}).join("");
  const cur=CAR&&CAR.subject&&CAR.subject.key;
  const dots=pts.map(p=>`<circle class="spt" data-k="${esc(p.key)}"
    cx="${X(p.odo).toFixed(1)}" cy="${Y(p.price).toFixed(1)}" r="2.8"
    fill="#3060a0" opacity=".55" style="cursor:pointer"><title>${p.year||""} ${esc(p.variant||"")}: ${money(p.price)}
 at ${km(p.odo)} (${p.d}) - click to judge this car</title></circle>`).join("");
  // the subject ringed, with a drop-line to the fitted curve: the length of
  // that line IS the residual the "expected for its kilometres" panel quotes
  let here="";
  if(CAR&&CAR.subject&&CAR.subject.price&&CAR.subject.odometer_km){
    const sx=X(CAR.subject.odometer_km),sy=Y(CAR.subject.price);
    if(sx>=L&&sx<=W-R){
      let drop="";
      if(m.usable&&m.median_price&&CAR.expected){
        const ey=Y(CAR.expected.price);
        drop=`<line x1="${sx.toFixed(1)}" y1="${sy.toFixed(1)}" x2="${sx.toFixed(1)}"
          y2="${ey.toFixed(1)}" stroke="#c04040" stroke-width="1.5" stroke-dasharray="3 2"/>
          <text x="${(sx+6).toFixed(1)}" y="${((sy+ey)/2).toFixed(1)}" class="ax"
            style="fill:#c04040">${CAR.expected.gap>0?"+":""}${money(CAR.expected.gap)}</text>`;
      }
      here=drop+`<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="7" fill="none"
        stroke="#c04040" stroke-width="2.5"/>`;
    }
  }
  /* the same model the adjustment uses: price = median * exp(slope*(odo-median_odo)),
     slope = ln(1 + pct_per_10k/100) / 10000. Anchored on the FIT's own median
     price, not the headline one - they are over different row sets. */
  let fit="";
  if(m.usable&&m.median_odo&&m.median_price){
    const s=Math.log(1+m.pct_per_10k_km/100)/10000;
    const base=m.median_price;
    const seg=[];
    for(let i=0;i<=24;i++){const o=x0+(x1-x0)*i/24;
      seg.push(`${i?"L":"M"}${X(o).toFixed(1)},${Y(base*Math.exp(s*(o-m.median_odo))).toFixed(1)}`);}
    fit=`<path d="${seg.join("")}" fill="none" stroke="#c04040" stroke-width="2"
      stroke-dasharray="6 4"/>`;
  }
  const shown=pts.length<d.scatter_total
    ? `showing the newest ${pts.length} of ${d.scatter_total} points` : `${pts.length} points`;
  return `<div class="cols">
   <div><div class="mini-h">asking price against odometer${m.usable?" (dashed = fitted trend)":""}</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="price against odometer">${grid}${dots}${fit}${here}${xt}</svg>
    <div class="hint">${shown}. Click a point to judge that car${
      cur?"; the ringed one is the car on the card above":""}.</div></div>
   <div><div class="mini-h">what the fit says</div>
    <div class="lst">
     <div><span class="nm">usable</span><b>${m.usable?"yes":"no"}</b></div>
     ${m.usable?`
     <div><span class="nm">price change per 10,000 km</span>
       <b class="${m.pct_per_10k_km<0?"down":"up"}">${m.pct_per_10k_km.toFixed(1)}%</b></div>
     <div><span class="nm">listings in the fit</span><b>${m.n}</b></div>
     <div><span class="nm">their median odometer</span><b>${km(m.median_odo)}</b></div>
     <div><span class="nm">their median price</span><b>${money(m.median_price)}</b></div>`
     :`<div><span class="nm">why not</span><span class="flat">${esc(m.reason)}</span></div>`}
     <div><span class="nm">median odometer, all sightings</span>
       <b>${d.head.median_odo==null?"&mdash;":km(d.head.median_odo)}</b></div>
     <div><span class="nm">$ per 10,000 km at that median</span><b>${
       d.head.median_odo?money(d.head.median/(d.head.median_odo/10000)):"&mdash;"}</b></div>
    </div>
    <div class="hint note">The fit runs on one row per vehicle per month, so a car
     re-seen weekly cannot dominate it - that is why its ${m.n||0} listings and
     median odometer differ from the ${pts.length} points plotted, which are
     every sighting. A robust (Huber) fit, refused below 20 listings with an
     odometer or a narrow km spread. The workbook's RichStats sheet fits the
     same relationship with least squares, so small differences are
     expected.</div></div></div>`;
}

/* who is selling, from where, of what age - each split WITH its median,
   because a count alone cannot tell you dealers ask more */
function whoTab(d){
  const tbl=(title,rows,note)=>`<div><div class="mini-h">${title}</div>
    <table><thead><tr><th>${note}</th><th class="num">n</th><th class="num">cars</th>
     <th class="num">median</th><th class="num">range</th></tr></thead><tbody>
    ${rows.map(r=>`<tr><td>${esc(r.name)}</td><td class="num">${r.n}</td>
      <td class="num">${r.vehicles}</td><td class="num">${money(r.median)}</td>
      <td class="num flat">${money(r.lo)}&ndash;${money(r.hi)}</td></tr>`).join("")
    ||'<tr><td colspan="5" class="flat">nothing</td></tr>'}</tbody></table></div>`;
  const s=d.splits;
  return `<div class="cols">
    ${tbl("seller",s.seller,"type")}
    ${tbl("state",s.state,"where")}
   </div><div class="cols" style="margin-top:14px">
    ${tbl("source",s.source,"captured from")}
    <div><div class="mini-h">median price by model year</div>
      ${yearChart(s.year)}</div>
   </div>
   <div class="hint note">A dealer median above the private median is normal - it
    prices in warranty and on-road costs, not a better car. "(unknown)" is a
    listing that did not state it, and is left out of the year chart because
    it has no year to sit at.</div>`;
}

/* median price by model year, bar width carrying the sample size so a bar
   built on one listing cannot look as solid as one built on forty */
function yearChart(rows){
  const ys=(rows||[]).filter(r=>/^\d{4}$/.test(r.name)&&r.median!=null);
  if(ys.length<2)return '<div class="flat">not enough years with a price</div>';
  const W=520,H=210,L=58,R=12,T=12,B=32,n=ys.length;
  const mx=Math.max(...ys.map(r=>r.median)),mn=Math.min(...ys.map(r=>r.median));
  let lo=Math.max(0,mn-(mx-mn)*0.25),hi=mx+(mx-mn)*0.1||mx*1.1;
  const maxN=Math.max(...ys.map(r=>r.n));
  const slot=(W-L-R)/n;
  const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo||1));
  const grid=vpaTicks(lo,hi,3).map(v=>{return `
    <line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(y(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
  const bars=ys.map((r,i)=>{
    const w=Math.max(5,Math.min(slot-6,slot*(0.35+0.55*r.n/maxN)));
    const cx=L+slot*i+slot/2;
    return `<rect x="${(cx-w/2).toFixed(1)}" y="${y(r.median).toFixed(1)}" width="${w.toFixed(1)}"
      height="${(y(lo)-y(r.median)).toFixed(1)}" fill="#88aa88" rx="2">
      <title>${r.name}: median ${money(r.median)} over ${r.n} listing(s),
 ${money(r.lo)} to ${money(r.hi)}</title></rect>
    <text x="${cx.toFixed(1)}" y="${H-10}" text-anchor="middle" class="ax">${r.name}</text>`;}).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="median price by model year">${grid}${bars}</svg>
    <div class="hint">Wider bar = more listings behind that year.</div>`;
}

/* ---------- turnover: what happened between the captures -----------------
   Every number here comes from vpa/turnover.py, which is also where the
   reasons for refusing to produce one live. This lays it out and does no
   arithmetic of its own beyond formatting. */
function turnTab(d){
  const t=d.churn||{}, tc=d.trim_churn||{rows:[]};
  const cmp=trimCompare(tc);
  if(!t.usable){
    return `<div class="warnbox"><b>No turnover figure for this selection.</b>
      ${esc(t.reason||"not enough history")}.</div>
     <div class="hint">What makes it work: captures saved with <b>Ctrl+S</b>
      (those carry the site's own ad id, and only those cars can be recognised
      from one capture to the next), taken over the <b>same search</b> each
      time. ${t.cohort_cars||0} of ${(t.cohort_cars||0)+(t.excluded_no_id||0)}
      car(s) here are trackable.</div>`
     +(t.daily&&t.daily.length>1?`<div style="margin-top:14px">
        <div class="mini-h">what each capture was made of</div>
        <table><thead><tr><th>capture</th><th class="num">trackable</th>
         <th class="num">carried over</th><th class="num">new</th></tr></thead><tbody>
        ${t.daily.map(r=>`<tr><td>${r.d}${r.break_before
          ?'<span class="tag warn">coverage break</span>':""}</td>
         <td class="num">${r.n}</td><td class="num">${r.carry==null?"—":r.carry}</td>
         <td class="num">${r.new==null?"—":r.new}</td></tr>`).join("")}
        </tbody></table></div>`:"")+cmp;
  }
  const ps=t.price_signal||{};
  const tiles=[
    [t.rate30.toFixed(0)+"%","leave view per 30 days"],
    [t.half_life==null?"—":t.half_life.toFixed(0)+" days","half the stock gone"],
    [t.median_days_est==null?"—":t.median_days_est.toFixed(0)+" days","typical time listed"],
    [t.median_stock_age==null?"—":t.median_stock_age.toFixed(0)+" days","age of what is still up"],
    [t.exits,"left view"],[t.arrivals,"arrived"],[t.still,"up at the last capture"],
  ].map(([v,l])=>`<div class="tile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");

  const iv=(t.intervals||[]).map(i=>`<tr>
    <td>${i.from} &rarr; ${i.to}</td><td class="num">${i.elapsed}</td>
    <td class="num">${i.start_n}</td>
    <td class="num${i.counts_exits?"":" flat"}">${i.exits}${i.counts_exits?"":"*"}</td>
    <td class="num${i.counts_arrivals?"":" flat"}">${i.arrivals}${i.counts_arrivals?"":"*"}</td>
    <td class="num">${i.end_n}</td>
    <td>${i.scope==="steady"?'<span class="tag good">comparable</span>'
        :`<span class="tag warn">${i.scope==="widened"?"wider search":"narrower search"}</span>`}</td>
   </tr>`).join("");

  // "priced against its own day" is the only comparison that survives a
  // market that drifted between the two dates
  const rel=v=>v==null?'<span class="flat">&mdash;</span>'
    :`<span class="${v<-1?"up":(v>1?"down":"flat")}">${v>0?"+":""}${v.toFixed(1)}%</span>`;
  const signal=`<div class="kv">
    <div><span class="k">cars that left, median asking</span><b>${money(ps.exit_median)}</b>
      <span class="flat">n=${ps.exit_n}</span></div>
    <div><span class="k">cars still up, median asking</span><b>${money(ps.still_median)}</b>
      <span class="flat">n=${ps.still_n}</span></div>
    <div><span class="k">what left, vs its own day's median</span>${rel(ps.exit_rel)}</div>
    <div><span class="k">what stayed, vs its own day's median</span>${rel(ps.still_rel)}</div>
    <div><span class="k">gap between them</span>${rel(ps.spread)}</div>
    <div><span class="k">median odometer, left vs stayed</span>
      <b>${km(ps.exit_odo)}</b><span class="flat">vs ${km(ps.still_odo)}</span></div>
   </div>
   <div class="hint note">A negative gap means the cars that disappeared were the
    cheap ones for their day &mdash; the market clearing from the bottom.
    Near zero means price is not what decides which cars go.</div>`;

  const dep=(t.departed||[]).map(v=>`<tr class="pick" data-k="${esc(v.key)}">
    <td>${v.year||""} ${esc(v.variant||"")}</td>
    <td class="num">${money(v.price)}</td><td class="num">${rel(v.rel_pct)}</td>
    <td class="num">${v.odometer_km==null?"":km(v.odometer_km)}</td>
    <td>${v.first_day}</td><td>${v.last_day}</td>
    <td class="num">${v.censored?"&ge;"+v.days_listed
      :v.days_listed+"&ndash;"+v.days_max}</td>
    <td>${vpaAdLinks(v.url,true,v)}</td>
   </tr>`).join("");

  return `<p class="verdict">${esc(t.verdict||"")}</p>
   <div class="tiles">${tiles}</div>
   ${(t.notes||[]).length?`<div class="hint">${t.notes.map(esc).join(" &middot; ")}</div>`:""}
   <div class="cols" style="margin-top:16px">
    <div style="flex:2 1 380px">
     <div class="mini-h">capture by capture &mdash; ${t.span.start} to ${t.span.end}</div>
     <table><thead><tr><th>between</th><th class="num">days</th>
      <th class="num">up at the start</th><th class="num">left</th>
      <th class="num">arrived</th><th class="num">up at the end</th>
      <th>pair</th></tr></thead><tbody>${iv}</tbody></table>
     <div class="hint">* shown but not counted: when one capture is much
      bigger than the other, the difference is mostly the search changing
      size. A wider capture still proves an exit, a narrower one does not.</div>
    </div>
    <div style="flex:1 1 300px"><div class="mini-h">was price what decided it</div>
     ${signal}</div>
   </div>
   <div class="mini-h" style="margin-top:18px">the ${t.departed_total} car(s)
    that left view &mdash; click a row to judge it against the trim it left</div>
   ${dep?`<div class="scroll"><table><thead><tr><th>car</th>
     <th class="num">last price</th><th class="num">vs that day</th>
     <th class="num">odometer</th><th>first seen</th><th>last seen</th>
     <th class="num">days listed</th><th>listing</th></tr></thead>
     <tbody>${dep}</tbody></table></div>
    <div class="hint note">"days listed" is a range, not a measurement: the car was
     up by the capture that first saw it and gone by the capture that first
     missed it. &ge; means it was already there at the first capture, so only
     the floor is known.</div>`
    :'<div class="flat">nothing has left view within the comparable captures</div>'}
   ${cmp}`;
}

/* the same measurement for every trim of the line, so a trim that clears in
   three weeks is visibly different from one that sits for three months */
function trimCompare(tc){
  if(!tc||!tc.rows||!tc.rows.length)return "";
  const p=tc.pooled;
  // A model line can hold eighty trims and rate nine of them. Listing the
  // other seventy-one as seventy-one identical "no site ad id" rows buries
  // the comparison this table exists to make, so they are counted by reason
  // instead - the same information, one line each.
  const ok=tc.rows.filter(r=>r.usable), no=tc.rows.filter(r=>!r.usable);
  const why={};
  no.forEach(r=>{const k=r.reason||"not rateable";why[k]=(why[k]||0)+1;});
  const row=r=>`<tr class="pick${r.selected?" on":""}" data-v="${esc(r.variant)}">
    <td>${esc(r.variant)}${r.thin?'<span class="tag warn">thin</span>':""}</td>
    <td class="num">${r.cars}</td><td class="num">${r.cohort}</td>
    <td class="num">${r.rate30.toFixed(0)}%</td>
    <td class="num">${r.half_life==null?"—":r.half_life.toFixed(0)}</td>
    <td class="num">${r.median_days_est==null?"—":r.median_days_est.toFixed(0)}</td>
    <td class="num">${r.median_stock_age==null?"—":r.median_stock_age.toFixed(0)}</td>
    <td class="num">${r.exits}</td><td class="num">${r.arrivals}</td>
    <td class="num">${r.still}</td></tr>`;
  if(!ok.length)return `<div class="hint" style="margin-top:18px">No trim in
    this model line has enough comparable captures to rate yet.
    ${Object.entries(why).map(([k,n])=>`<b>${n}</b> ${esc(k)}`).join("; ")}.</div>`;
  return `<div class="mini-h" style="margin-top:20px">how this trim clears
    against its siblings &mdash; click a row to select it</div>
   <table><thead><tr><th>trim</th><th class="num">cars</th>
    <th class="num">trackable</th><th class="num">per 30 days</th>
    <th class="num">half gone (d)</th><th class="num">typical listed (d)</th>
    <th class="num">stock age (d)</th><th class="num">left</th>
    <th class="num">arrived</th><th class="num">up now</th></tr></thead>
   <tbody>${ok.map(row).join("")}
    ${p?`<tr style="font-weight:600;background:#f7f9fd"><td>all ${p.trims} trims pooled</td>
      <td class="num">&mdash;</td><td class="num">&mdash;</td>
      <td class="num">${p.rate30.toFixed(0)}%</td>
      <td class="num">${p.half_life==null?"—":p.half_life.toFixed(0)}</td>
      <td colspan="5"></td></tr>`:""}</tbody></table>
   <div class="hint note">Each trim is measured over its own captures, which are
    not the same days for every trim &mdash; they are made comparable by
    counting exits per car per day rather than per capture. The pooled row is
    the line's own rate; it is built from the trims because a whole model line
    is searched too differently from one day to the next to measure directly.
    ${no.length?`<br><b>${no.length}</b> other trim(s) here cannot be rated:
      ${Object.entries(why).sort((a,b)=>b[1]-a[1])
        .map(([k,n])=>`<b>${n}</b> ${esc(k)}`).join("; ")}.`:""}</div>`;
}

/* every trim in this model line, side by side - the workbook's RichStats
   variant breakdown and the Supply sheet's per-variant block in one table */
function trimTab(d){
  const s=d.siblings||[];
  if(!s.length)return '<div class="flat">pick a make and model to see its trims</div>';
  // the range chart lives here rather than beside the bell curve: it answers
  // "which trim" while the curve answers "which car within one trim"
  const sib=s.filter(x=>x.lo!=null&&x.hi!=null).slice(0,12);
  let boxes="";
  if(sib.length>1){
    const BW=560,rowH=26,BH=Math.max(90,sib.length*rowH+46),BL=170,BR=16,BT=14;
    const lo2=Math.min(...sib.map(x=>x.lo)),hi2=Math.max(...sib.map(x=>x.hi));
    const bx=v=>BL+(BW-BL-BR)*(v-lo2)/(hi2-lo2||1);
    const rows=sib.map((x,i)=>{const cy=BT+18+i*rowH;
      return `<line x1="${bx(x.lo).toFixed(1)}" y1="${cy}" x2="${bx(x.hi).toFixed(1)}" y2="${cy}"
          stroke="#a9b2c1" stroke-width="1.5"/>
        <line x1="${bx(x.lo).toFixed(1)}" y1="${cy-5}" x2="${bx(x.lo).toFixed(1)}" y2="${cy+5}" stroke="#a9b2c1"/>
        <line x1="${bx(x.hi).toFixed(1)}" y1="${cy-5}" x2="${bx(x.hi).toFixed(1)}" y2="${cy+5}" stroke="#a9b2c1"/>
        <circle cx="${bx(x.median).toFixed(1)}" cy="${cy}" r="5"
          fill="${x.selected?"#c04040":"#3060a0"}"><title>${esc(x.variant)}: median
 ${money(x.median)}, ${money(x.lo)} to ${money(x.hi)}, ${x.n} price(s)</title></circle>
        <text x="${BL-8}" y="${cy+4}" text-anchor="end" class="ax"
          style="font-size:11px;${x.selected?"fill:#c04040;font-weight:600":""}">${esc(x.variant)}</text>`;
      }).join("");
    const bt=vpaTicks(lo2,hi2,3).map(v=>{return `
      <text x="${bx(v).toFixed(1)}" y="${BH-8}" text-anchor="middle" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
    boxes=`<div class="mini-h">each trim's range and median &mdash; overlapping
      bars mean the trims are not really priced apart</div>
      <svg viewBox="0 0 ${BW} ${BH}" width="100%" height="${BH}" role="img"
        aria-label="price range per trim">${rows}${bt}</svg>`;
  }
  return boxes+`<div class="mini-h" style="margin-top:14px">every trim in
    ${esc(d.head.label.split(" ").slice(0,2).join(" "))}
    &mdash; click a row to select it</div>
   <table><thead><tr><th>trim</th><th class="num">prices</th><th class="num">cars</th>
    <th class="num">days</th><th class="num">median</th><th class="num">lowest</th>
    <th class="num">highest</th><th class="num">median odo</th>
    <th class="num">for sale</th><th class="num">median then</th>
    <th>latest capture</th></tr></thead><tbody>
   ${s.map(r=>`<tr class="pick${r.selected?" on":""}" data-v="${esc(r.variant)}">
     <td>${esc(r.variant)}</td><td class="num">${r.n}</td><td class="num">${r.vehicles}</td>
     <td class="num">${r.days}</td><td class="num">${money(r.median)}</td>
     <td class="num">${money(r.lo)}</td><td class="num">${money(r.hi)}</td>
     <td class="num">${r.odo==null?"":km(r.odo)}</td>
     <td class="num">${r.now}</td><td class="num">${money(r.now_median)}</td>
     <td>${r.last}</td></tr>`).join("")}</tbody></table>
   <div class="hint note">"for sale" and "median then" are at each trim's OWN latest
    capture, which is not always the same day for every trim - the date is in
    the last column.</div>`;
}

/* ---------- picker ---------- */
// How the trim list is ordered. The label shown after each trim changes with
// the sort, so whatever you are ordering by is the number you can see.
const plural=(n,word)=>`${n} ${word}${n===1?"":"s"}`;
const SORTS={
  n:        {cmp:(a,b)=>b.n-a.n,                    tag:v=>plural(v.n,"price")},
  name:     {cmp:(a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true}),
             tag:v=>plural(v.n,"price")},
  median_desc:{cmp:(a,b)=>(b.median??-Infinity)-(a.median??-Infinity), tag:v=>money(v.median)},
  median_asc: {cmp:(a,b)=>(a.median??Infinity)-(b.median??Infinity),   tag:v=>money(v.median)},
  days:     {cmp:(a,b)=>b.days-a.days||b.n-a.n,     tag:v=>plural(v.days,"day")},
  last:     {cmp:(a,b)=>(b.last||"").localeCompare(a.last||""),        tag:v=>v.last},
  vehicles: {cmp:(a,b)=>b.vehicles-a.vehicles,      tag:v=>plural(v.vehicles,"car")},
  odo:      {cmp:(a,b)=>(a.odo??Infinity)-(b.odo??Infinity),
             tag:v=>v.odo==null?"no odometer":km(v.odo)},
};
function sortedVariants(model){
  const s=SORTS[$("vsort").value]||SORTS.n;
  return {list:model.variants.slice().sort(s.cmp), tag:s.tag};
}
function fillSelects(keepMake,keepModel,keepVariant){
  const mk=$("mk"),mo=$("mo"),va=$("va");
  mk.innerHTML=TREE.makes.map(m=>`<option value="${esc(m.name)}">${esc(m.name)} (${m.n})</option>`).join("");
  if(keepMake)mk.value=keepMake;
  const make=TREE.makes.find(m=>m.name===mk.value)||TREE.makes[0];
  mo.innerHTML=make.models.map(m=>`<option value="${esc(m.name)}">${esc(m.name)} (${m.n})</option>`).join("");
  if(keepModel&&make.models.some(m=>m.name===keepModel))mo.value=keepModel;
  const model=make.models.find(m=>m.name===mo.value)||make.models[0];
  const {list,tag}=sortedVariants(model);
  va.innerHTML='<option value="">all trims ('+model.n+')</option>'
    +list.map(v=>`<option value="${esc(v.name)}">${esc(v.name)} — ${esc(tag(v))}</option>`).join("");
  if(keepVariant&&list.some(v=>v.name===keepVariant))va.value=keepVariant;
}
/* PICK is the set of trims pooled into one selection. Empty means every trim
   of the model, which is what "all trims" always meant - so one trim, three
   trims and all trims are one code path here exactly as they are on the
   server. */
let PICK=[];
/* Select exactly one trim (or all trims for ""). Setting the dropdown alone
   is not enough and never was: assigning .value fires no change event, so
   load() would re-read the OLD PICK and the click would appear to do
   nothing. Both tables that offer "click a row to select this trim" come
   through here. */
function pickOnly(v){
  v=(v==="(no trim)"?"":v)||"";
  const changed=!(PICK.length===1&&PICK[0]===v)&&!(PICK.length===0&&!v);
  PICK=v?[v]:[];
  $("va").value=v;
  // Moving to a different trim invalidates any car being judged: it was
  // ranked against a pool that is no longer on screen, and leaving it up
  // would rank an XR6 against a table of XR8 Sprints.
  if(changed&&CAR)clearCar();
  load();
}
function sel(){return {make:$("mk").value,model:$("mo").value,variants:PICK.slice()};}
function selLabel(){
  const s=sel();
  return PICK.length===1?[s.make,s.model,PICK[0]].filter(Boolean).join(" ")
       :PICK.length?`${s.make} ${s.model} — ${PICK.length} trims`
       :[s.make,s.model].filter(Boolean).join(" ");
}
function filterQuery(q){
  const put=(k,id)=>{const v=$(id).value.trim();if(v)q.set(k,v);};
  put("y0","fY0");put("y1","fY1");put("p0","fP0");put("p1","fP1");put("q","fKw");
  return q;
}
function anyFilter(){
  return ["fY0","fY1","fP0","fP1","fKw"].some(id=>$(id).value.trim());
}

/* Sibling trims as clickable chips (the workbook's Models sheet), in the
   same order as the dropdown so the two never disagree. A Falcon has 180
   trims, and drawing every one made a wall of chips that pushed the whole
   page down a screen. So: the first CHIP_CAP in the chosen order, every
   selected trim whatever its rank, a filter box, and "show all" on demand. */
let CHIPS=[], CHIPS_ALL=false;
const CHIP_CAP=24;
function renderChips(){
  const q=($("chipq").value||"").trim().toLowerCase();
  $("chipbar").style.display=CHIPS.length>1?"":"none";
  if(CHIPS.length<2){$("chips").innerHTML="";return;}
  const hits=q?CHIPS.filter(c=>c.variant.toLowerCase().includes(q)):CHIPS;
  const shown=(CHIPS_ALL||q)?hits
    :hits.filter((c,i)=>i<CHIP_CAP||c.selected);
  $("chipspan").textContent=q?`— ${hits.length} of ${CHIPS.length} match`
    :`— ${CHIPS.length}, each with its median and prices collected`;
  $("chips").innerHTML=shown.map(s2=>
    `<span class="chip${s2.selected?" on":""}" data-v="${esc(s2.variant)}"
      title="${s2.n} prices, ${s2.vehicles} vehicles over ${s2.days} capture day(s), last ${s2.last}">${esc(s2.variant)}
      <span class="flat">${money(s2.median)} · ${s2.n}</span></span>`).join("")
    +(()=>{const more=!q&&hits.length>shown.length,
                  fewer=!q&&CHIPS_ALL&&hits.length>CHIP_CAP;
      return more||fewer?`<span class="chip more" id="chipAll">${more
        ?"show all "+hits.length:"show fewer"}</span>`:"";})()
    +(q&&!hits.length?`<span class="flat" style="font-size:12.5px">no trim matches "${esc(q)}"</span>`:"");
  const all=$("chipAll");
  if(all)all.onclick=()=>{CHIPS_ALL=!CHIPS_ALL;renderChips();};
  // Plain click selects ONE trim, which is what it always did. Ctrl/Shift
  // click toggles that trim in and out of a pooled set, so the existing
  // gesture keeps its meaning and the new one is additive.
  document.querySelectorAll(".chip[data-v]").forEach(c=>c.onclick=ev=>{
    const v=c.dataset.v==="(no trim)"?"":c.dataset.v;
    if(ev.ctrlKey||ev.metaKey||ev.shiftKey){
      if(!v)return;                      // "(no trim)" cannot be pooled
      const i=PICK.indexOf(v);
      if(i>=0)PICK.splice(i,1);else PICK.push(v);
    }else PICK=v?[v]:[];
    $("va").value=PICK.length===1?PICK[0]:"";
    load();});
}

async function load(){
  const s=sel();
  // A subject is only meaningful against the pool it was ranked in, so
  // changing the selection drops it rather than silently re-ranking it
  // against a different set of cars.
  if(CAR&&CAR.label&&PICK.length!==1){
    CAR=null;TARGET=null;$("carcard").style.display="none";
  }
  const q=new URLSearchParams({make:s.make,model:s.model});
  PICK.forEach(v=>q.append("variant",v));
  filterQuery(q);
  $("selspan").textContent=`— loading ${selLabel()}…`;
  const d=await vpaLatest("detail","/api/explore/detail?"+q);
  if(!d)return;                          // a newer selection has taken over
  if(!d.ok){$("selspan").textContent=d.error||"failed";return;}
  DATA=d;
  if(d.empty){
    $("selspan").textContent="— "+d.reason;
    $("tiles").innerHTML="";$("trend").innerHTML="";$("supply").innerHTML="";
    CHIPS=[];renderChips();
    $("tabbody").innerHTML="";$("supplysum").innerHTML="";
    $("vtbl").querySelector("tbody").innerHTML="";return;
  }
  const h=d.head;
  $("selspan").textContent=`— ${h.label} (${h.level} level), ${h.first_day} to ${h.last_day}`;
  // What is being shown, stated rather than implied. A filtered page that
  // looks like an unfiltered one is the quickest way to misread a median.
  const badges=(PICK.length>1?[`${PICK.length} trims pooled`]:[])
    .concat((d.filters||[]).map(f=>f.text));
  $("pickhint").innerHTML=
    badges.map(b=>`<span class="fbadge">${esc(b)}</span>`).join("")
    +(badges.length
      ? (d.kept<d.unfiltered
         ? `showing <b>${d.kept}</b> of ${d.unfiltered} sighting(s) in this model. `
         : `all <b>${d.kept}</b> sighting(s) in this model match. `)
      : "")
    +"Click a trim to select it; <b>Ctrl-click</b> to pool several.";
  // the selection and its filters are bookmarkable
  const su=new URLSearchParams(location.search);
  su.delete("variant");
  su.set("make",$("mk").value);su.set("model",$("mo").value);
  PICK.forEach(v=>su.append("variant",v));
  ["y0","y1","p0","p1","q"].forEach(k=>su.delete(k));
  filterQuery(su);
  history.replaceState(null,"","/explore?"+su);
  $("tiles").innerHTML=[
    [money(h.median),"median asking"],
    [money(h.q25)+" – "+money(h.q75),"middle half (IQR)"],
    [money(h.lo)+" – "+money(h.hi),"full range"],
    [h.vehicles.toLocaleString(),"distinct vehicles"],
    [h.observations.toLocaleString(),"price observations"],
    [h.days,"capture days"],
    [h.median_odo==null?"—":km(h.median_odo),"median odometer"],
  ].map(([v,l])=>`<div class="tile"><b${String(v).includes("–")?' class="rng"':""}>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");

  let warn="";
  if(h.collisions)warn+=`<div class="warnbox"><b>${h.collisions}</b> vehicle identity(ies) here hold
    two prices on the same day, so two different cars were merged into one. They are flagged
    <span class="tag warn">merged identity</span> below and excluded from price movements.</div>`;
  if(d.caveats.length)warn+=`<div class="hint">${d.caveats.map(esc).join(" &middot; ")}</div>`;
  $("caveats").innerHTML=warn;

  trendChart(d.byday);supplyChart(d.byday);
  $("supplyspan").textContent=d.byday.length?`— peak ${Math.max(...d.byday.map(b=>b.vehicles))}`:"";
  const sp=d.supply,ch=d.churn||{};
  $("supplysum").innerHTML=sp.captures?[
    [sp.now,"at the latest capture"],
    [sp.change==null?"—":(sp.change>0?"+":"")+sp.change,"vs the capture before"],
    [sp.avg,"average per capture"],
    [sp.most,"most"],[sp.fewest,"fewest"],[sp.captures,"captures"],
  ].concat(ch.usable?[
    [ch.rate30.toFixed(0)+"%","leave view per 30 days"],
    [ch.half_life==null?"—":ch.half_life.toFixed(0)+"d","half of the stock gone"],
    [ch.median_days_est==null?"—":ch.median_days_est.toFixed(0)+"d","typical time listed"],
  ]:[]).map(([v,l])=>`<span><b>${esc(v)}</b> <span class="flat">${l}</span></span>`).join("")
   +(ch.usable?`<span class="flat note">More in the <b>Turnover</b> tab below.</span>`
              :`<span class="flat note">Turnover not rated: ${esc(ch.reason||"")}</span>`):"";


  // sibling trims as clickable chips (the workbook's Models sheet), in the
  // same order as the dropdown so the two never disagree
  const cs=SORTS[$("vsort").value]||SORTS.n;
  CHIPS=d.siblings.slice().sort((a,b)=>cs.cmp(
    {...a,name:a.variant},{...b,name:b.variant}));
  renderChips();

  // carry the current selection into Compare, so "how does this sit against
  // its siblings" is one click and not a re-pick
  const cq=new URLSearchParams();
  cq.append("sel",[h.label].join(""));
  (d.siblings||[]).filter(x=>!x.selected).slice(0,3).forEach(x=>
    cq.append("sel",`${$("mk").value} ${$("mo").value} ${x.variant}`));
  // the Compare TAB carries the current selection across, so the nav is the
  // deep link rather than a second button beside it
  const ct=document.querySelector('.navtab[href^="/compare"]');
  if(ct){ct.href="/compare?"+cq;
    ct.title="compare this selection with its nearest trims";}

  renderTab();renderVehicles();
}

/* ---------- individual vehicles ---------- */
function renderVehicles(){
  const d=DATA;if(!d||d.empty)return;
  const q=$("filter").value.trim().toLowerCase();
  let rows=d.vehicles.map(v=>({...v,
    per10k:(v.price!=null&&v.odometer_km)?v.price/(v.odometer_km/10000):null}));
  if(q)rows=rows.filter(v=>[v.year,v.variant,v.state,v.seller_type,v.source,v.title]
    .map(x=>String(x==null?"":x).toLowerCase()).join(" ").includes(q));
  rows.sort((a,b)=>{
    const x=a[sortKey],y=b[sortKey];
    if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1;
    return (x>y?1:x<y?-1:0)*sortDir;});
  const cur=CAR&&CAR.subject&&CAR.subject.key;
  // the trim's latest capture day: a car last seen before it has gone
  const newest=d.vehicles.reduce((m,v)=>v.last_day>m?v.last_day:m,"");
  $("vtbl").querySelector("tbody").innerHTML=rows.map(v=>`<tr class="pick${
      v.key===cur?" on":""}${v.starred?" starred":""}" data-k="${esc(v.key)}"
      data-v="${esc(v.variant||"")}">
    <td><button class="star${v.starred?" on":""}" data-star="${esc(v.key)}"
      title="${v.starred?"stop following this car":"follow this car"}"
      >${v.starred?"★":"☆"}</button></td>
    <td>${v.year||""}</td>
    <td>${esc(v.variant||"")}${v.clash?'<span class="tag warn" title="two cars share this identity - they show two prices on the same capture day">merged identity</span>':""}
      ${v.detail==="brief"?'<span class="tag">price only</span>':""}</td>
    <td class="num">${money(v.price)}${v.moved?`<span class="tag ${v.change>0?"good":"warn"}">${v.change>0?"+":""}${v.change}%</span>`:""}</td>
    <td class="num">${km(v.odometer_km)}</td>
    <td class="num">${v.per10k==null?"":money(v.per10k)}</td>
    <td>${esc(v.state||"")}</td><td>${esc(v.seller_type||"")}</td>
    <td>${esc(v.source)}</td><td class="num">${v.sightings}</td>
    <td>${v.last_day}</td>
    <td>${vpaAdLinks(v.url,v.last_day<newest,v)}</td>
  </tr>`).join("")||'<tr><td colspan="12" class="flat">nothing matches that filter</td></tr>';
  $("vspan").textContent=`— showing ${rows.length}`
    +(d.vehicle_total>d.vehicles.length?` of ${d.vehicle_total} (newest ${d.vehicles.length} loaded)`:"");
  $("vhint").textContent="Click any row to judge that car against the rest of "
    +"this trim. $/10k km divides asking price by odometer - a rough "
    +"value-per-wear comparison, only meaningful within one trim.";
  // clicking the already-selected row clears it, so the gesture toggles
  document.querySelectorAll("#vtbl .star").forEach(b=>b.onclick=e=>{
    e.stopPropagation();                     // starring is not selecting
    toggleStar(b.dataset.star,!b.classList.contains("on"));});
  document.querySelectorAll("#vtbl tr.pick").forEach(tr=>tr.onclick=e=>{
    if(e.target.closest("a")||e.target.closest(".star"))return;
    if(tr.dataset.k===cur)clearCar();
    else openCar({car:tr.dataset.k,variant:tr.dataset.v});});
}
document.querySelectorAll("th.s").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;
  if(sortKey===k)sortDir=-sortDir;else{sortKey=k;sortDir=(k==="price"||k==="last_day"||k==="sightings")?-1:1;}
  renderVehicles();});
$("filter").oninput=renderVehicles;

// Changing make or model drops the trim selection: a trim name belongs to
// one model, and carrying "SR5" across to a Falcon would select nothing.
$("mk").onchange=()=>{PICK=[];resetChips();fillSelects($("mk").value,null,null);load();};
$("mo").onchange=()=>{PICK=[];resetChips();fillSelects($("mk").value,$("mo").value,null);load();};
// a different model is a different set of trims: start the picker afresh
function resetChips(){CHIPS_ALL=false;$("chipq").value="";}
$("chipq").oninput=renderChips;
$("va").onchange=()=>{PICK=$("va").value?[$("va").value]:[];load();};
// re-ordering must not change the selection - keep the chosen trims selected
$("vsort").onchange=()=>{const c=sel();
  fillSelects(c.make,c.model,PICK.length===1?PICK[0]:null);load();};
$("clearBtn").onclick=()=>{PICK=[];$("va").value="";load();};

/* filters. Applied on Enter as well as the button, because typing a number
   and pressing Enter is what everyone does. */
$("fGo").onclick=load;
["fY0","fY1","fP0","fP1","fKw"].forEach(id=>
  $(id).onkeydown=e=>{if(e.key==="Enter")load();});
$("fClear").onclick=()=>{
  ["fY0","fY1","fP0","fP1","fKw"].forEach(id=>$(id).value="");load();};


/* ---------- stay in step with the database ---------- */
let seenVersion=null;
async function checkFresh(){
  let s;              // after Shut down the poll simply stops answering
  try{s=vpaApplyStatus(await fetch("/api/status").then(r=>r.json()));}catch(e){return;}
  if(seenVersion===null)seenVersion=s.data_version;
  else if(s.data_version!==seenVersion){
    seenVersion=s.data_version;
    if(TREE){ // new data arrived (an import in another tab) - rebuild silently
      TREE=await fetch("/api/explore/picker").then(r=>r.json());
      const cur=sel();fillSelects(cur.make,cur.model,cur.variant);load();
    }
  }
}

(async()=>{
  TREE=await fetch("/api/explore/picker").then(r=>r.json());
  if(!TREE.makes.length){$("subline").textContent="no data yet - import something first";return;}
  const p=new URLSearchParams(location.search);
  PICK=p.getAll("variant").filter(Boolean);
  [["y0","fY0"],["y1","fY1"],["p0","fP0"],["p1","fP1"],["q","fKw"]]
    .forEach(([k,id])=>{if(p.get(k))$(id).value=p.get(k);});
  fillSelects(p.get("make"),p.get("model"),PICK.length===1?PICK[0]:null);
  await checkFresh();await load();loadWatchlist();
  // a verdict is bookmarkable: restore whichever subject the URL names
  if(p.get("car"))await openCar({car:p.get("car")});
  else if(p.get("price")){
    ["price","odo","year","seller","state","note"].forEach(k=>{
      const f={price:"sPrice",odo:"sOdo",year:"sYear",seller:"sSeller",
               state:"sState",note:"sNote"}[k];
      if(p.get(k))$(f).value=p.get(k);});
    $("seencard").style.display="block";
    await openCar({price:p.get("price"),odo:p.get("odo"),year:p.get("year"),
                   seller:p.get("seller"),state:p.get("state"),note:p.get("note")});
  }
  // pick up imports made in another tab
  window.addEventListener("focus",checkFresh);
  setInterval(checkFresh,20000);
})();
__CHROME_JS__
</script></div></body></html>
"""


from .ui import chrome as _chrome   # noqa: E402

PAGE = _chrome(_PAGE, "explore")
