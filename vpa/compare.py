"""Compare — many selections side by side, plus the catalogue of every
selection you can pick.

    served at /compare by importui's server

The third read-only page, and deliberately its own one. `/` owns ingestion and
answers dataset-level questions; `/explore` answers "what is this ONE vehicle
worth"; this page answers "how do these vehicles compare to each other", which
neither of the others can do without becoming two pages in a trenchcoat.

It is the workbook's **Comparison** and **Models** sheets brought across:

    Comparison  ->  the side-by-side stat block (same rows, same order, same
                    definitions as viewer_xlsx._CMP_ROWS) and the median-by-
                    month chart over every selection at once
    Models      ->  the catalogue: every selection that exists, with its
                    counts and median, searchable and sortable

Selection strings are exactly the workbook's: a make ("Ford"), a model line
("Ford Falcon") or a line plus trim ("Ford Falcon XR8"), matched by exact
equality on three disjoint identity columns - viewer_xlsx.Match's rule, so a
label typed into the workbook's row 3 and the same label here pool the same
rows.

ONE deliberate difference, called out on the page and in `basis` below:
median-by-month here counts each vehicle once per month (analyze.prep), the
same basis as `vpa report` and Explore. The workbook's month block cannot
de-duplicate in a formula, so it counts every sighting. Everything else on
this page is over raw sightings and matches the workbook exactly.

Writes nothing, caches nothing.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .explore import _frame, _num, _select
from .store import DEFAULT_DB

#: Same six slots as the workbook's Comparison sheet.
MAX_SELECTIONS = 6
#: scatter points sent per column (six columns share one chart)
MAX_SCATTER_PER_COLUMN = 300

#: Stat rows, in display order, mirroring viewer_xlsx._CMP_ROWS. Kept as data
#: so the labels here and the payload keys can never drift apart.
#: (key, label, kind, group-header-or-None)
CMP_ROWS = [
    ("n",            "prices (n)",             "int",   "how much data"),
    ("full",         "full detail",            "int",   None),
    ("brief",        "price only (brief)",     "int",   None),
    ("days",         "distinct capture days",  "int",   None),
    ("vehicles",     "distinct vehicles",      "int",   None),
    ("median",       "median",                 "money", "price"),
    ("mean",         "average",                "money", None),
    ("q25",          "25th percentile",        "money", None),
    ("q75",          "75th percentile",        "money", None),
    ("lo",           "lowest",                 "money", None),
    ("hi",           "highest",                "money", None),
    ("spread",       "spread (high - low)",    "money", None),
    ("median_odo",   "median odometer",        "km",    "wear and age"),
    ("per_10k",      "$ per 10,000 km",        "money", None),
    ("median_year",  "median year",            "year",  None),
    ("dealer",       "dealer listings",        "int",   "who is selling"),
    ("private",      "private listings",       "int",   None),
    ("first_day",    "first capture",          "date",  "movement"),
    ("last_day",     "latest capture",         "date",  None),
    ("median_first", "median @ first",         "money", None),
    ("median_last",  "median @ latest",        "money", None),
    ("change",       "change first -> latest", "pct",   None),
    ("fc_method",    "forecast method",        "text",  "outlook"),
    ("fc_next",      "next month (forecast)",  "money", None),
    ("fc_direction", "direction",              "text",  None),
    # How fast each selection CLEARS, which is the one axis on which a cheap
    # slow car and a dear quick one are genuinely different propositions.
    # Computed by vpa/turnover.py, which refuses far more often than it
    # answers - so `turn_basis` carries the refusal into the column rather
    # than leaving a row of dashes to be read as "zero".
    ("turn_rate",    "leaves view / 30 days",  "rate",  "turnover"),
    ("turn_half",    "half the stock gone",    "days",  None),
    ("turn_listed",  "typical time listed",    "days",  None),
    ("turn_age",     "age of what is still up", "days", None),
    ("turn_left",    "left view",              "int",   None),
    ("turn_arrived", "arrived",                "int",   None),
    ("turn_basis",   "measured over",          "text",  None),
    ("rank",         "rank by median",         "int",   "against each other"),
    ("vs_cheapest",  "vs cheapest selection",  "pct",   None),
]


# ------------------------------------------------------------------ labels
def selection_index(df: pd.DataFrame) -> dict:
    """label -> (make, model, variant, level) for every selection that exists.

    The three levels are disjoint by construction: a make is one token, a
    model line is 'make model', a trim is 'make model variant'. That is the
    same disjointness viewer_xlsx.Match relies on when it OR-sums the three
    identity columns."""
    index: dict[str, tuple] = {}
    if df.empty:
        return index
    for mk in sorted(df["make"].dropna().astype(str).unique()):
        index[mk] = (mk, None, None, "make")
    pairs = df.dropna(subset=["make", "model"])[["make", "model"]].drop_duplicates()
    for mk, mo in pairs.itertuples(index=False):
        index[f"{mk} {mo}"] = (str(mk), str(mo), None, "line")
    trios = (df.dropna(subset=["make", "model", "variant"])
             [["make", "model", "variant"]].drop_duplicates())
    for mk, mo, va in trios.itertuples(index=False):
        if str(va).strip():
            index[f"{mk} {mo} {va}"] = (str(mk), str(mo), str(va), "trim")
    return index


def resolve(label: str, index: dict) -> tuple | None:
    """A label the user typed -> a selection, or None if it matches nothing.
    Case-insensitive and whitespace-tolerant, because these get typed."""
    label = " ".join(str(label or "").split())
    if not label:
        return None
    if label in index:
        return index[label]
    folded = {k.casefold(): v for k, v in index.items()}
    return folded.get(label.casefold())


# --------------------------------------------------------------- catalogue
def catalogue_payload(db_path: str | Path = DEFAULT_DB) -> dict:
    """Every selection that exists, with the numbers needed to choose between
    them - the workbook's Models sheet, but sortable and searchable.

    Near-duplicate rows here mean the same car was entered with different
    text; that is a data-entry problem to fix at entry, and seeing them side
    by side is how you spot it."""
    df = _frame(db_path)
    if df.empty:
        return {"ok": True, "rows": [], "levels": {}}

    index = selection_index(df)
    rows = []
    for label, (mk, mo, va, level) in index.items():
        sub = _select(df, mk, mo, va)
        priced = sub.dropna(subset=["price_aud"])
        if priced.empty:
            continue
        rows.append({
            "label": label, "level": level,
            "make": mk, "model": mo, "variant": va,
            "n": int(len(priced)),
            "full": int((priced["detail_level"] == "full").sum()),
            "brief": int((priced["detail_level"] == "brief").sum()),
            "vehicles": int(priced["key"].nunique()),
            "days": int(priced["day"].nunique()),
            "median": _num(priced["price_aud"].median()),
            "lo": _num(priced["price_aud"].min()),
            "hi": _num(priced["price_aud"].max()),
            "odo": _num(priced["odometer_km"].median()),
            "first": str(priced["day"].min()), "last": str(priced["day"].max()),
        })
    rows.sort(key=lambda r: (r["level"] != "make", r["level"] != "line", -r["n"]))
    levels = {}
    for r in rows:
        levels[r["level"]] = levels.get(r["level"], 0) + 1
    return {"ok": True, "rows": rows, "levels": levels}


# -------------------------------------------------------------- comparison
def _one(df: pd.DataFrame, label: str, sel: tuple, horizon: int,
         live_only: bool = False) -> dict:
    """Every stat in CMP_ROWS for one selection, computed the same way the
    workbook computes it: over priced sightings, no de-duplication (the one
    exception is `months`, which is deduped - see the module docstring)."""
    from . import analyze, forecast

    mk, mo, va, level = sel
    sub = _select(df, mk, mo, va)
    # Turnover is measured BEFORE the live filter and never after it: keeping
    # only the cars in the newest capture deletes exactly the cars that left,
    # which is the entire measurement.
    whole = sub
    # "currently listed" = seen in THIS selection's own most recent capture.
    # Applied before anything is computed, so every stat, chart and forecast
    # on the page describes the same rows - a live filter that reached only
    # the charts would put two different populations on one screen.
    live_keys, live_day = analyze.live_keys(sub)
    if live_only:
        sub = sub[sub["key"].isin(live_keys)]
    priced = sub.dropna(subset=["price_aud"])
    priced = priced[priced["price_aud"] > 0]
    out = {"label": label, "level": level, "make": mk, "model": mo,
           "variant": va, "n": int(len(priced)),
           "live_day": live_day, "live_cars": int(len(live_keys))}
    if priced.empty:
        out["empty"] = True
        return out

    p = priced["price_aud"]
    odo = priced.loc[priced["odometer_km"] > 0, "odometer_km"]
    med = _num(p.median())
    med_odo = _num(odo.median()) if len(odo) else None
    first_day, last_day = str(priced["day"].min()), str(priced["day"].max())
    med_first = _num(priced.loc[priced["day"] == first_day, "price_aud"].median())
    med_last = _num(priced.loc[priced["day"] == last_day, "price_aud"].median())

    # monthly medians, deduped per vehicle per month - the report/Explore basis
    prepped = analyze.prep(sub, freq="MS")
    stats = analyze.bucket_stats(prepped)

    # the bell curve, over ONE price per car - the same pool Explore's curve
    # and the verdict card use, so the two pages cannot draw different
    # distributions for the same selection
    curve = analyze.price_curve(
        analyze.latest_per_vehicle(prepped)["price_aud"])
    months = [{"d": pd.Timestamp(r["bucket"]).strftime("%Y-%m"),
               "n": int(r["n"]), "median": _num(r["median"])}
              for _, r in stats.iterrows()]

    fc = forecast.forecast_segment(prepped, stats, horizon=horizon)
    pts = fc.get("points")
    fc_next = (_num(pts.iloc[0]["forecast"]) if pts is not None and len(pts)
               else None)
    fc_points = ([] if pts is None else
                 [{"d": pd.Timestamp(r["bucket"]).strftime("%Y-%m"),
                   "v": _num(r["forecast"]), "lo": _num(r["lo"]),
                   "hi": _num(r["hi"])} for _, r in pts.iterrows()])

    # per capture day: median AND how many were up. Two charts feed off this -
    # supply over time, and the day-by-day price line.
    byday = []
    for day, g in priced.groupby("day"):
        byday.append({"d": str(day), "n": int(len(g)),
                      "vehicles": int(g["key"].nunique()),
                      "median": _num(g["price_aud"].median()),
                      "q25": _num(g["price_aud"].quantile(0.25)),
                      "q75": _num(g["price_aud"].quantile(0.75))})
    byday.sort(key=lambda r: r["d"])

    # scatter points, capped PER COLUMN: six selections at the full cap would
    # be 7,200 circles, which is slower to draw than it is useful to read
    odo_rows = priced[priced["odometer_km"] > 0]
    scatter = [{"odo": _num(r["odometer_km"]), "price": _num(r["price_aud"]),
                "year": None if pd.isna(r["year"]) else int(r["year"])}
               for _, r in odo_rows.tail(MAX_SCATTER_PER_COLUMN).iterrows()]

    out.update({
        "empty": False,
        "full": int((priced["detail_level"] == "full").sum()),
        "brief": int((priced["detail_level"] == "brief").sum()),
        "vehicles": int(priced["key"].nunique()),
        "days": int(priced["day"].nunique()),
        "median": med, "mean": _num(p.mean()),
        "q25": _num(p.quantile(0.25)), "q75": _num(p.quantile(0.75)),
        "lo": _num(p.min()), "hi": _num(p.max()),
        "spread": _num(p.max() - p.min()),
        "median_odo": med_odo,
        "per_10k": (None if not med_odo else _num(med / (med_odo / 10000))),
        "median_year": (None if priced["year"].dropna().empty
                        else _num(priced["year"].dropna().median())),
        "dealer": int((priced["seller_type"] == "dealer").sum()),
        "private": int((priced["seller_type"] == "private").sum()),
        "first_day": first_day, "last_day": last_day,
        "median_first": med_first, "median_last": med_last,
        # one capture day cannot show a change; saying "-" beats saying 0%
        "change": (None if first_day == last_day or not med_first else
                   round((med_last - med_first) / med_first * 100, 1)),
        "months": months, "byday": byday, "curve": curve,
        "scatter": scatter, "scatter_total": int(len(odo_rows)),
        "fc_method": fc["method"], "fc_next": fc_next,
        "fc_points": fc_points, "fc_interval": fc["interval_level"],
        "fc_direction": fc.get("direction", "unknown"),
        "fc_verdict": fc.get("verdict", ""),
        "collisions": len(analyze.collided_keys(sub)),
    })

    from .turnover import turnover as _turnover
    t = _turnover(whole, whole, label=label)
    # the same per-day composition Explore draws, attached to the day series
    # this page already shares across columns, so one capture's bar and one
    # capture's point are describing the same rows
    per_day = {r["d"]: r for r in t.get("daily", [])}
    for b in byday:
        c = per_day.get(b["d"])
        b.update({"cohort": c["n"] if c else 0,
                  "carry": c["carry"] if c else None,
                  "new": c["new"] if c else None,
                  "exits": c["exits"] if c else 0,
                  "rated": bool(c and c["rated"]),
                  "break_before": bool(c and c["break_before"])})
    out.update({
        "turn_rate": t["rate30"], "turn_half": t["half_life"],
        "turn_listed": t["median_days_est"], "turn_age": t["median_stock_age"],
        "turn_left": t["exits"] if t["usable"] else None,
        "turn_arrived": t["arrivals"] if t["usable"] else None,
        "turn_basis": (f"{t['span']['captures']} captures, "
                       f"{t['span']['start']} to {t['span']['end']}"
                       f"{' (thin)' if t['thin'] else ''}")
                      if t["usable"] else t["reason"],
        "turn_verdict": t.get("verdict", ""),
        "turn_usable": bool(t["usable"]),
        "turn_intervals": t.get("intervals", []),
        "turn_notes": t.get("notes", []),
        "turn_spread": (t.get("price_signal") or {}).get("spread"),
    })
    return out


def compare_payload(db_path: str | Path = DEFAULT_DB,
                    labels: list[str] | None = None,
                    horizon: int = 3, live_only: bool = False) -> dict:
    """Side-by-side stats for up to MAX_SELECTIONS labels, plus the shared
    month axis the chart needs. Unknown labels are reported rather than
    silently dropped - a typo that quietly compares nothing is worse than an
    error message."""
    df = _frame(db_path)
    if df.empty:
        return {"ok": True, "columns": [], "months": [], "days": [],
                "unknown": [], "basis": _BASIS, "rows": _row_meta(),
                "live_only": bool(live_only), "max": MAX_SELECTIONS}

    index = selection_index(df)
    cols, unknown = [], []
    for raw in (labels or [])[:MAX_SELECTIONS]:
        sel = resolve(raw, index)
        if sel is None:
            unknown.append(str(raw))
            continue
        label = f"{sel[0]}" + (f" {sel[1]}" if sel[1] else "") + \
                (f" {sel[2]}" if sel[2] else "")
        if any(c["label"] == label for c in cols):
            continue                       # same selection picked twice
        cols.append(_one(df, label, sel, horizon, live_only))

    # rank and "vs cheapest" only mean anything across the columns on screen,
    # exactly like the workbook's RANK over $B$10:$G$10
    medians = [c["median"] for c in cols if not c.get("empty") and c.get("median")]
    cheapest = min(medians) if medians else None
    for c in cols:
        if c.get("empty") or not c.get("median"):
            c["rank"], c["vs_cheapest"] = None, None
            continue
        c["rank"] = 1 + sum(1 for m in medians if m < c["median"])
        c["vs_cheapest"] = (None if not cheapest else
                            round((c["median"] / cheapest - 1) * 100, 1))

    # shared axes: every month/day ANY column has, so the charts line up
    # instead of each column drawing on its own private scale
    all_months = sorted({m["d"] for c in cols for m in c.get("months", [])}
                        | {p["d"] for c in cols for p in c.get("fc_points", [])})
    all_days = sorted({b["d"] for c in cols for b in c.get("byday", [])})
    return {"ok": True, "columns": cols, "months": all_months,
            "days": all_days, "unknown": unknown, "basis": _BASIS,
            "rows": _row_meta(), "max": MAX_SELECTIONS,
            "live_only": bool(live_only)}


_BASIS = ("Counts and price stats are over every priced sighting, identical to "
          "the workbook's Comparison sheet. Median by month counts each "
          "vehicle once per month (the `vpa report` basis), so it can differ "
          "slightly from the workbook's month block, which counts every "
          "sighting because an Excel formula cannot de-duplicate.")


def _row_meta() -> list[dict]:
    return [{"key": k, "label": lab, "kind": kind, "group": grp}
            for k, lab, kind, grp in CMP_ROWS]


# -------------------------------------------------------------------- page
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Compare &middot; VPA</title>
<style>
__CHROME_CSS__
 /* picked selections */
 .picks{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;min-height:30px;align-items:center}
 .pick{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);
       background:#fafbfd;border-radius:16px;padding:3px 5px 3px 11px;font-size:12.5px}
 .pick b{font-weight:600}
 /* comparison grid. Columns are a fixed width and the table only as wide as
    it needs: with two selections, a full-width table put each value 500px
    from the label it belongs to. */
 #cmp{width:auto;min-width:520px}
 #cmp th:first-child,#cmp td:first-child{position:sticky;left:0;background:#fff;
    width:220px;min-width:220px;box-shadow:1px 0 0 var(--line-2)}
 #cmp th:first-child{background:var(--head)}
 #cmp th.num{text-transform:none;letter-spacing:0;font-size:12.5px;color:var(--ink);
   width:168px;min-width:150px;white-space:normal;vertical-align:bottom;
    line-height:1.3}
 #cmp th .swatch{margin-right:5px;vertical-align:-1px}
 #cmp tr.grp td{background:var(--head);font-weight:600;color:var(--ink-2);font-size:11px;
    text-transform:uppercase;letter-spacing:.05em;padding-top:9px;border-top:1px solid var(--line)}
 #cmp tr.grp td:first-child{background:var(--head)}
 #cmp td:first-child{color:var(--ink-2)}
 #cmp td.v{text-align:right}
 #cmp td.best{background:var(--good-soft);font-weight:600}
 #cmp .empty{width:auto}
 .cmpwrap{overflow-x:auto}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Compare vehicles</h1>
 <div class="sub"><span id="subline">Makes, models or trims side by side, on identical statistics. Read-only.</span></div>
</div>__ACTIONS__</div>

<div class="card keep" id="help" style="display:none">
 <p><b>This page never writes anything.</b> It is the workbook's
 <b>Comparison</b> and <b>Models</b> sheets: the same selections, the same
 stat rows, the same definitions.</p>
 <ul style="font-size:13px">
  <li>Pick up to <b id="maxn">6</b> selections. A make ("Ford") pools every
      model, a model line ("Ford Falcon") pools every trim, and a trim
      ("Ford Falcon XR8") is that trim only - exactly as in the workbook.</li>
  <li><b>Best value per row</b> is shaded green: the cheapest median, the most
      data, the lowest odometer. It is a hint, not a verdict.</li>
  <li>The link in each column header opens that selection in <b>Explore</b>
      for the full history, forecast interval and individual cars.</li>
  <li><span class="tag warn">merged identity</span> counts vehicle identities
      holding two prices on one day. Those are two cars sharing one identity,
      so their difference is never counted as a price movement.</li>
  <li><b>The charts</b> all read the same payload as the table: median by
      month with each selection's projection carried forward dashed (the
      shaded band is the 80% interval, drawn for one or two selections so it
      stays readable), range-and-median bars, any single stat as bars via the
      dropdown, supply per capture day, price against odometer, and the
      dealer/private split. Hover any point for its numbers.</li>
  <li><b>Bell curves</b> are fitted on log price (so they cannot run below
      $0) over one price per car. Each is scaled to its own peak, not its
      sample size, because the question is what SHAPE a trim prices in - a
      tall narrow curve is a consistent trim, a wide flat one is a trim where
      kilometres and condition matter more than the badge.</li>
  <li><b>Latest capture only</b> keeps just the cars seen in each selection's
      most recent capture, and keeps their full price history so the trend
      and forecast still work. It is applied before any number is computed,
      so the whole page describes one population. It is <b>not</b> proof a
      car is still for sale - only that it was in the last search you
      captured.</li>
  <li id="basis" class="flat"></li>
 </ul></div>

<div class="card keep">
 <div class="ptitle">what to compare</div>
 <div class="row">
  <div style="flex:1;min-width:260px"><label>add a selection</label>
   <input type="text" id="add" list="alllist" placeholder="type a make, model line or trim..."
     style="width:100%"><datalist id="alllist"></datalist></div>
  <div><button class="ghost" id="addBtn">Add</button></div>
  <div><button class="ghost" id="clearBtn">Clear all</button></div>
  <div><button class="ghost" id="topBtn" title="compare the selections with the most data">Top 4</button></div>
  <div><label>which captures</label><select id="live">
    <option value="0">every capture (all time)</option>
    <option value="1">latest capture only</option>
  </select></div>
 </div>
 <div class="picks" id="picks"></div>
 <div class="hint" id="pickhint"></div>
 <div class="hint" id="livehint" style="margin-top:2px"></div>
</div>

<div class="card">
 <div class="ptitle">side by side <span id="cmpspan"></span></div>
 <div class="cmpwrap"><table id="cmp"></table></div>
 <div class="hint note">Counts and prices are over every priced sighting - the same
  rows the workbook counts. Empty columns are ignored.</div>
</div>

<div class="card">
 <div class="cardhead">
  <div class="ptitle">median price by month
    <span>&mdash; each vehicle counted once per month</span></div>
  <label style="display:inline-flex;gap:6px;align-items:center;margin:0;font-size:12.5px;color:var(--ink-2)">
   <input type="checkbox" id="showFc" checked> show projection</label>
 </div>
 <div id="chart"></div><div class="lgd" id="legend"></div>
 <div class="hint" id="charthint"></div>
</div>

<div class="card">
 <div class="ptitle">bell curves <span id="bellspan">&mdash; one fitted to each selection's prices</span></div>
 <div id="bell"></div><div class="lgd" id="belllgd"></div>
 <div class="hint note">Fitted on log price, so the curve cannot run below $0 and
  the long tail of rare cars does not drag it. A tall narrow curve is a trim
  that prices consistently; a wide flat one is a trim where condition and
  kilometres matter more than the badge. Where two curves overlap, the same
  money buys either.</div>
</div>

<div class="card">
 <div class="act">
  <div class="panel"><div class="ptitle">price range and median
    <span>&mdash; overlapping bars are not really priced apart</span></div>
   <div id="boxes"></div></div>
  <div class="panel">
   <div class="cardhead">
    <div class="ptitle">side-by-side bars <span id="metricspan"></span></div>
    <div><select id="metric">
      <option value="median">median price</option>
      <option value="per_10k">$ per 10,000 km</option>
      <option value="n">prices collected</option>
      <option value="vehicles">distinct vehicles</option>
      <option value="median_odo">median odometer</option>
      <option value="spread">price spread</option>
      <option value="days">capture days</option>
      <option value="median_year">median year</option>
    </select></div>
   </div>
   <div id="bars"></div></div>
 </div>
</div>

<div class="card">
 <div class="act">
  <div class="panel"><div class="ptitle">how many were for sale at each capture</div>
   <div id="supply"></div></div>
  <div class="panel"><div class="ptitle">price against odometer
    <span id="scatspan"></span></div><div id="scatter"></div></div>
 </div>
 <div class="hint note">Supply counts distinct vehicles seen that day. A gap means
  that selection was not captured that day - not that it vanished. A hollow
  point marks a capture that shares no cars with the one before it: that is
  a different search, not a market that emptied, and the two are not
  comparable.</div>
</div>

<div class="card">
 <div class="ptitle">what changed between the captures
   <span>&mdash; arrivals above the line, cars that left below it</span></div>
 <div id="churn"></div><div class="lgd" id="churnlgd"></div>
 <div class="lst" id="churnsum" style="margin-top:10px"></div>
 <div class="hint note">Only cars carrying the site's own ad id can be recognised
  from one capture to the next, so this counts fewer cars than the supply
  chart above - the difference is stated per selection. A faded bar is a pair
  of captures too different in size to count: a much bigger capture inflates
  arrivals, a much smaller one inflates exits. <b>None of this means "sold"</b>
  - a listing can go by selling, being withdrawn, expiring, or being
  re-listed under a new id.</div>
</div>

<div class="card">
 <div class="ptitle">who is selling <span>&mdash; dealer against private, per selection</span></div>
 <div id="sellers"></div>
</div>

<div class="card">
 <div class="cardhead">
  <div class="ptitle">every selection you can pick <span id="catspan"></span></div>
  <div><select id="lvl">
    <option value="">all levels</option><option value="make">makes</option>
    <option value="line">model lines</option><option value="trim">trims</option>
   </select></div>
  <div><input type="text" id="filter" placeholder="search..." style="width:230px"></div>
 </div>
 <div class="scroll"><table id="cat"><thead><tr>
  <th class="s" data-k="label">selection</th><th class="s" data-k="level">level</th>
  <th class="s num" data-k="n">prices</th><th class="s num" data-k="full">full</th>
  <th class="s num" data-k="brief">brief</th>
  <th class="s num" data-k="vehicles">vehicles</th>
  <th class="s num" data-k="days">days</th>
  <th class="s num" data-k="median">median</th>
  <th class="s num" data-k="lo">lowest</th><th class="s num" data-k="hi">highest</th>
  <th class="s num" data-k="odo">median odo</th>
  <th class="s" data-k="last">last capture</th><th></th>
 </tr></thead><tbody></tbody></table></div>
 <div class="hint note">Near-duplicates here mean the same car was entered with
  different text - fix that at entry, not here.</div>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money=v=>v==null?"":"$"+Math.round(v).toLocaleString();
const km=v=>v==null?"":Math.round(v).toLocaleString()+" km";
const COLORS=["#c04040","#3060a0","#2e7d32","#a06010","#7048a0","#0d7a80"];
const DASH="&mdash;";
const MAXGUESS=6;   // slot count before the first payload lands

let CAT=null, DATA=null, PICKS=[], catSort="n", catDir=-1;

/* format one stat cell by its declared kind, so the table and the payload
   can never disagree about what a number means */
function fmt(kind,v){
  if(v==null||v==="")return `<span class="flat">${DASH}</span>`;
  if(kind==="money")return money(v);
  if(kind==="km")return km(v);
  if(kind==="int")return Number(v).toLocaleString();
  if(kind==="year")return String(Math.round(v));
  if(kind==="pct"){const c=v>0.05?"up":(v<-0.05?"down":"flat");
    return `<span class="${c}">${v>0?"+":""}${Number(v).toFixed(1)}%</span>`;}
  // a clearance RATE is not a change: no sign, and no green/red, because
  // fast turnover is good news to a seller and bad news to a buyer
  if(kind==="rate")return `${Number(v).toFixed(0)}%`;
  if(kind==="days")return `${Number(v).toFixed(0)} d`;
  return esc(v);
}
/* which column wins a row, where "wins" is meaningful at all */
const BEST={median:"min",per_10k:"min",lo:"min",vs_cheapest:"min",
            median_odo:"min",rank:"min",
            n:"max",full:"max",days:"max",vehicles:"max",median_year:"max"};

function renderCompare(){
  const d=DATA,t=$("cmp");
  if(!d||!d.columns.length){
    t.innerHTML=`<tr><td class="empty">Add a selection above, or pick one from
      the catalogue below, to compare.</td></tr>`;
    $("cmpspan").textContent="";$("legend").innerHTML="";$("belllgd").innerHTML="";
    $("churnlgd").innerHTML="";$("churnsum").innerHTML="";
    ["chart","bell","boxes","bars","supply","churn","scatter","sellers"].forEach(id=>
      noData($(id),"pick a selection to draw this"));
    return;}
  const cols=d.columns;
  let h=`<thead><tr><th>stat</th>`+cols.map((c,i)=>
    `<th class="num"><span class="swatch" style="background:${COLORS[i%6]}"></span>
      <a class="ext" href="/explore?make=${encodeURIComponent(c.make||"")}&model=${encodeURIComponent(c.model||"")}&variant=${encodeURIComponent(c.variant||"")}"
       title="open in Explore">${esc(c.label)}</a>
      ${c.collisions?`<span class="tag warn" title="${c.collisions} identity(ies) hold two prices on one day">${c.collisions} merged</span>`:""}
    </th>`).join("")+`</tr></thead><tbody>`;
  let grp=null;
  for(const r of d.rows){
    if(r.group&&r.group!==grp){grp=r.group;
      h+=`<tr class="grp"><td>${esc(grp)}</td>`+cols.map(()=>"<td></td>").join("")+`</tr>`;}
    const vals=cols.map(c=>c[r.key]);
    let win=null;
    if(BEST[r.key]){
      const nums=vals.filter(v=>typeof v==="number");
      if(nums.length>1)win=BEST[r.key]==="min"?Math.min(...nums):Math.max(...nums);
    }
    h+=`<tr><td>${esc(r.label)}</td>`+vals.map(v=>
      `<td class="v${(win!=null&&v===win)?" best":""}">${fmt(r.kind,v)}</td>`).join("")+`</tr>`;
  }
  t.innerHTML=h+`</tbody>`;
  $("cmpspan").textContent=`— ${cols.length} selection${cols.length===1?"":"s"}, `
    +`shaded cell = best in that row`;
  drawCharts();
}

/* ---------- charts: inline SVG, no libraries ----------------------------
   All of them share these two helpers so every chart on the page uses the
   same axis treatment, and a gap in a series always stays a gap rather than
   being joined by a line pretending a price was observed there. */
/* `lo` is drawn at px0 and `hi` at px1: a y axis passes (bottom, top), an x
   axis (left, right). It used to always draw lo at px1, which is right for
   y and silently mirrored every x axis - the price-range and odometer charts
   ran right-to-left, with the cheapest car at the right-hand end. */
function scale(lo,hi,px0,px1,padFrac){
  const nonneg=lo>=0;   // prices, kilometres and counts never go below zero
  if(lo===hi){lo=lo*0.95||-1;hi=hi*1.05||1;}
  if(padFrac){const p=(hi-lo)*padFrac;lo-=p;hi+=p;}
  // out to the round numbers either side, so the axis is labelled at both ends
  const t=vpaTicks(lo,hi,5),st=t.length>1?t[1]-t[0]:0;
  if(st){lo=Math.floor(lo/st)*st;hi=Math.ceil(hi/st)*st;}
  if(nonneg&&lo<0)lo=0;
  return {lo,hi,at:v=>px0+(px1-px0)*((v-lo)/(hi-lo||1))};
}
function yGrid(s,L,R,W,fmt,steps){
  if(fmt===money)fmt=vpaAxMoney;
  return vpaTicks(s.lo,s.hi,steps?steps.length:5).map(v=>{return `
    <line x1="${L}" y1="${s.at(v).toFixed(1)}" x2="${W-R}" y2="${s.at(v).toFixed(1)}" stroke="#eef0f4"/>
    <text x="${L-7}" y="${(s.at(v)+3.5).toFixed(1)}" text-anchor="end" class="ax">${fmt(v)}</text>`;}).join("");
}
function gapPath(pts,x,y){
  let dd="",pen=false;
  pts.forEach((v,i)=>{
    if(v==null){pen=false;return;}
    dd+=`${pen?"L":"M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;pen=true;});
  return dd;
}
function noData(el,msg){el.innerHTML=`<div class="empty">${msg}</div>`;}

/* median by month for every selection, with each one's projection carried
   forward dashed. Interval bands are only drawn for one or two selections -
   six translucent bands over each other is mud, not information. */
function monthChart(d){
  const el=$("chart"),months=d.months,withFc=$("showFc").checked;
  const series=d.columns.map((c,i)=>{
    const hist=months.map(m=>{const h=(c.months||[]).find(x=>x.d===m);
      return h?h.median:null;});
    const fc=months.map(m=>{const p=(c.fc_points||[]).find(x=>x.d===m);
      return p?p.v:null;});
    return {label:c.label,color:COLORS[i%6],hist,fc,
            band:(c.fc_points||[]).map(p=>({d:p.d,lo:p.lo,hi:p.hi})),
            interval:c.fc_interval,method:c.fc_method,
            lastHist:months.reduce((a,m,i2)=>hist[i2]!=null?i2:a,-1)};});
  const vals=series.flatMap(s=>s.hist.concat(withFc?s.fc:[])
    .concat(withFc?s.band.flatMap(b=>[b.lo,b.hi]):[])).filter(v=>v!=null);
  if(!vals.length||!months.length){
    noData(el,"no monthly data for these selections");
    $("legend").innerHTML="";$("charthint").textContent="";return;}
  const W=1180,H=300,L=70,R=14,T=14,B=34,n=months.length;
  const s=scale(Math.min(...vals),Math.max(...vals),H-B,T,0.08);
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const y=s.at;
  // a dashed rule halfway between the last observed month and the first
  // projected one, so "past" and "guess" are never read as one line
  const firstFc=withFc?months.findIndex(m=>series.some(sr=>
    sr.band.some(b=>b.d===m))):-1;
  let divider="";
  if(firstFc>0&&n>1){
    const dx=((x(firstFc)+x(firstFc-1))/2).toFixed(1);
    divider=`<line x1="${dx}" y1="${T}" x2="${dx}" y2="${H-B}" stroke="#c3c8d2"
      stroke-dasharray="3 3"/><text x="${(+dx+5).toFixed(1)}" y="${T+11}"
      class="ax">projection &rarr;</text>`;
  }
  const body=series.map(sr=>{
    let out="";
    if(withFc&&sr.band.length&&series.length<=2&&sr.lastHist>=0){
      const anchor=sr.hist[sr.lastHist];
      const up=[`M${x(sr.lastHist).toFixed(1)},${y(anchor).toFixed(1)}`]
        .concat(sr.band.map(b=>`L${x(months.indexOf(b.d)).toFixed(1)},${y(b.hi).toFixed(1)}`));
      const dn=sr.band.slice().reverse().map(b=>
        `L${x(months.indexOf(b.d)).toFixed(1)},${y(b.lo).toFixed(1)}`);
      out+=`<path d="${up.join("")}${dn.join("")}Z" fill="${sr.color}" opacity=".11"/>`;
    }
    out+=`<path d="${gapPath(sr.hist,x,y)}" fill="none" stroke="${sr.color}"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    if(withFc&&sr.band.length&&sr.lastHist>=0){
      const link=`M${x(sr.lastHist).toFixed(1)},${y(sr.hist[sr.lastHist]).toFixed(1)}`
        +sr.band.map(b=>{const p=sr.fc[months.indexOf(b.d)];
          return `L${x(months.indexOf(b.d)).toFixed(1)},${y(p).toFixed(1)}`;}).join("");
      out+=`<path d="${link}" fill="none" stroke="${sr.color}" stroke-width="2"
        stroke-dasharray="6 4"/>`;
      out+=sr.band.map(b=>{const i2=months.indexOf(b.d);
        return `<circle cx="${x(i2).toFixed(1)}" cy="${y(sr.fc[i2]).toFixed(1)}" r="3.6"
          fill="#fff" stroke="${sr.color}" stroke-width="2"><title>${esc(sr.label)} ${b.d}:
 projected ${money(sr.fc[i2])} (${sr.interval}% interval ${money(b.lo)} to ${money(b.hi)})</title></circle>`;}).join("");
    }
    out+=sr.hist.map((v,i)=>v==null?"":
      `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="3.2" fill="${sr.color}">
        <title>${esc(sr.label)} ${months[i]}: observed ${money(v)}</title></circle>`).join("");
    return out;}).join("");
  const ticks=months.map((m,i)=>(n<=12||i%Math.ceil(n/12)===0)
    ?`<text x="${x(i).toFixed(1)}" y="${H-10}" text-anchor="middle" class="ax">${m}</text>`:"").join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="median price by month, with projection, for each selection">
    ${yGrid(s,L,R,W,money)}${divider}${body}${ticks}</svg>`;
  $("legend").innerHTML=series.map(sr=>
    `<span><span class="swatch" style="background:${sr.color}"></span>${esc(sr.label)}
     ${sr.band.length?"":'<span class="flat">(no projection)</span>'}</span>`).join("");
  const projected=series.filter(sr=>sr.band.length);
  $("charthint").textContent=!withFc?"Projection hidden."
    :projected.length?`Solid = observed, dashed = projected `
      +`(${[...new Set(projected.map(p=>p.method.replace(/_/g," ")))].join(", ")}).`
      +(series.length<=2?" Shaded band is the 80% interval."
        :" Interval bands are drawn for one or two selections only - six would overlap into mud.")
    :"None of these selections has enough history to project yet (3 monthly "
      +"buckets and 8 prices are the minimum).";
}

/* one row per selection: full range as a whisker, middle half as a box,
   median as a dot. The single best "which is dearer" chart, because it shows
   the OVERLAP that a table of medians hides. */
function boxChart(d){
  const el=$("boxes"),cols=d.columns.filter(c=>!c.empty&&c.lo!=null);
  if(!cols.length){noData(el,"nothing to plot yet");return;}
  const rowH=34,W=560,L=Math.min(210,Math.max(90,...cols.map(c=>c.label.length*6.2))),
        R=16,T=12,H=T+cols.length*rowH+30;
  const s=scale(Math.min(...cols.map(c=>c.lo)),Math.max(...cols.map(c=>c.hi)),
                L,W-R,0.04);
  const x=s.at,body=cols.map((c,i)=>{
    const cy=T+16+i*rowH,col=COLORS[i%6];
    return `<line x1="${x(c.lo).toFixed(1)}" y1="${cy}" x2="${x(c.hi).toFixed(1)}" y2="${cy}"
        stroke="#b6bdc9" stroke-width="1.5"/>
      <line x1="${x(c.lo).toFixed(1)}" y1="${cy-6}" x2="${x(c.lo).toFixed(1)}" y2="${cy+6}" stroke="#b6bdc9"/>
      <line x1="${x(c.hi).toFixed(1)}" y1="${cy-6}" x2="${x(c.hi).toFixed(1)}" y2="${cy+6}" stroke="#b6bdc9"/>
      <rect x="${x(c.q25).toFixed(1)}" y="${cy-9}" width="${Math.max(1,x(c.q75)-x(c.q25)).toFixed(1)}"
        height="18" fill="${col}" opacity=".22" rx="3"/>
      <circle cx="${x(c.median).toFixed(1)}" cy="${cy}" r="5.5" fill="${col}">
        <title>${esc(c.label)}: median ${money(c.median)}, middle half ${money(c.q25)} to ${money(c.q75)},
 full range ${money(c.lo)} to ${money(c.hi)} over ${c.n} price(s)</title></circle>
      <text x="${L-8}" y="${cy+4}" text-anchor="end" class="ax"
        style="font-size:11px">${esc(c.label)}</text>`;}).join("");
  const xt=vpaTicks(s.lo,s.hi,5).map(v=>{return `
    <text x="${x(v).toFixed(1)}" y="${H-8}" text-anchor="middle" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="price range, middle half and median for each selection">${body}${xt}</svg>
   <div class="hint">Whisker = full range, shaded box = middle half (25-75%),
    dot = median.</div>`;
}

/* any single stat as bars, so every row of the table above can be seen as a
   picture without adding eight more charts to the page */
function barChart(d){
  const el=$("bars"),key=$("metric").value;
  const meta=d.rows.find(r=>r.key===key)||{label:key,kind:"money"};
  const cols=d.columns.filter(c=>typeof c[key]==="number");
  $("metricspan").textContent=cols.length?`— ${meta.label}`:"";
  if(!cols.length){noData(el,"no column has that number yet");return;}
  const f=v=>fmt(meta.kind,v).replace(/<[^>]*>/g,"");
  const W=560,T=12,B=30,barH=30,H=T+cols.length*barH+B,
        L=Math.min(210,Math.max(90,...cols.map(c=>c.label.length*6.2))),R=76;
  const mx=Math.max(...cols.map(c=>c[key])),mn=Math.min(0,...cols.map(c=>c[key]));
  const x=v=>L+(W-L-R)*((v-mn)/((mx-mn)||1));
  const best=BEST[key]==="min";   // BEST already knows which way each row wins
  const body=cols.map((c,i)=>{const cy=T+i*barH,v=c[key];
    return `<rect x="${L}" y="${cy+5}" width="${Math.max(1,x(v)-L).toFixed(1)}" height="19"
        fill="${COLORS[i%6]}" opacity=".82" rx="3">
        <title>${esc(c.label)}: ${f(v)}</title></rect>
      <text x="${L-8}" y="${cy+19}" text-anchor="end" class="ax" style="font-size:11px">${esc(c.label)}</text>
      <text x="${(x(v)+6).toFixed(1)}" y="${cy+19}" class="ax" style="font-size:11px">${f(v)}</text>`;
    }).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="${esc(meta.label)} per selection">${body}</svg>
   <div class="hint">${best?"Lower is better here.":"Bars share one scale, so lengths are comparable."}</div>`;
}

/* supply over time: how many distinct vehicles each selection had up on each
   capture day */
function supplyChart(d){
  const el=$("supply"),days=d.days||[];
  const series=d.columns.map((c,i)=>{
    const at=day=>(c.byday||[]).find(x=>x.d===day);
    return {label:c.label,color:COLORS[i%6],
      pts:days.map(day=>{const b=at(day);return b?b.vehicles:null;}),
      brk:days.map(day=>{const b=at(day);return !!(b&&b.break_before);}),
      // The composition behind the point, so hovering answers "is that dip
      // cars leaving, or a smaller capture?" without leaving the chart.
      // Every part is named and they sum to the point's own value - a
      // tooltip that accounts for 6 of 66 cars is worse than none, because
      // the reader assumes the other 60 fall into the categories shown.
      mix:days.map(day=>{const b=at(day);
        if(!b||b.carry==null)return "";
        const back=Math.max(0,(b.cohort||0)-b.carry-b.new),
              untracked=Math.max(0,b.vehicles-(b.cohort||0));
        const parts=[[b.carry,"carried over"],[back,"back in view"],
                     [b.new,"new"],[untracked,"with no ad id"]]
          .filter(([v])=>v>0).map(([v,t])=>`${v} ${t}`);
        return ` (${parts.join(", ")}`
          +(b.exits?`; ${b.exits} last seen here`:"")+")";})};});
  const vals=series.flatMap(s=>s.pts).filter(v=>v!=null);
  if(!vals.length){noData(el,"no capture days for these selections");return;}
  const W=560,H=240,L=48,R=14,T=14,B=34,n=days.length;
  const s=scale(0,Math.max(...vals),H-B,T,0.06);
  const x=i=>L+(n<2?(W-L-R)/2:i*(W-L-R)/(n-1));
  const body=series.map(sr=>
    `<path d="${gapPath(sr.pts,x,s.at)}" fill="none" stroke="${sr.color}" stroke-width="2"
       stroke-linejoin="round"/>`
    +sr.pts.map((v,i)=>v==null?"":
      // hollow = this capture shares no cars with the one before it, so the
      // step between the two points is a change of search and not of stock.
      // Drawing it identically would publish a scope change as a cliff.
      `<circle cx="${x(i).toFixed(1)}" cy="${s.at(v).toFixed(1)}" r="${sr.brk[i]?4:3}"
        fill="${sr.brk[i]?"#fff":sr.color}" stroke="${sr.color}" stroke-width="${sr.brk[i]?2:0}">
        <title>${esc(sr.label)} ${days[i]}: ${v} for sale${sr.mix[i]||""}</title></circle>`).join("")).join("");
  const ticks=days.map((day,i)=>(n<=6||i%Math.ceil(n/6)===0)
    ?`<text x="${x(i).toFixed(1)}" y="${H-10}" text-anchor="middle" class="ax">${day.slice(5)}</text>`:"").join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="vehicles for sale at each capture, per selection">
    ${yGrid(s,L,R,W,v=>Math.round(v).toLocaleString(),[0,.5,1])}${body}${ticks}</svg>`;
}

/* Churn, side by side: for each gap BETWEEN captures, what arrived (up) and
   what left view (down), one grouped bar per selection.

   A line chart cannot show this - supply holding flat at forty is the same
   picture whether it is the same forty cars or forty out and forty in, and
   those are opposite markets. Bars diverge from a shared zero so "building"
   and "emptying" are distinguishable at a glance, and every interval a
   selection could not be measured over is faded rather than dropped. */
function churnChart(d){
  const el=$("churn"),cols=d.columns||[];
  // one slot per capture interval that ANY selection has, so columns line up
  const slots=[...new Set(cols.flatMap(c=>(c.turn_intervals||[])
    .map(i=>i.from+"|"+i.to)))].sort();
  if(!slots.length){noData(el,"no selection here has two comparable captures");
    $("churnlgd").innerHTML="";$("churnsum").innerHTML="";return;}
  const get=(c,slot)=>(c.turn_intervals||[]).find(i=>i.from+"|"+i.to===slot);
  const vals=cols.flatMap(c=>(c.turn_intervals||[]).flatMap(i=>[i.arrivals,i.exits]));
  const top=Math.max(1,...vals);
  const W=560,H=250,L=44,R=14,T=16,B=44,n=slots.length,m=cols.length;
  const PH=H-T-B, zero=T+PH/2, sc=(PH/2)/top;
  const gw=(W-L-R)/n, bw=Math.max(3,Math.min(16,(gw-8)/Math.max(1,m)));
  const gx=i=>L+gw*i+(gw-bw*m)/2;
  const grid=[top,0,-top].map(v=>{const y=zero-v*sc;return `
    <line x1="${L}" y1="${y.toFixed(1)}" x2="${W-R}" y2="${y.toFixed(1)}"
      stroke="${v?"#eef0f4":"#c3c8d2"}"/>
    <text x="${L-7}" y="${(y+3.5).toFixed(1)}" text-anchor="end" class="ax">${Math.abs(v)}</text>`;}).join("");
  const bars=slots.map((slot,i)=>cols.map((c,j)=>{
    const iv=get(c,slot); if(!iv)return "";
    const x=gx(i)+bw*j, col=COLORS[j%6];
    const tip=(what,v,ok)=>`<title>${esc(c.label)} ${iv.from} to ${iv.to}
 (${iv.elapsed} day${iv.elapsed===1?"":"s"}): ${v} ${what}${ok?""
 :" - not counted, the two captures are too different in size ("+iv.start_n+" then "+iv.end_n+")"}</title>`;
    let s="";
    if(iv.arrivals)s+=`<rect x="${x.toFixed(1)}" y="${(zero-iv.arrivals*sc).toFixed(1)}"
      width="${bw.toFixed(1)}" height="${(iv.arrivals*sc).toFixed(1)}" fill="${col}"
      opacity="${iv.counts_arrivals?.85:.28}">${tip("arrived",iv.arrivals,iv.counts_arrivals)}</rect>`;
    if(iv.exits)s+=`<rect x="${x.toFixed(1)}" y="${zero.toFixed(1)}"
      width="${bw.toFixed(1)}" height="${(iv.exits*sc).toFixed(1)}" fill="${col}"
      opacity="${iv.counts_exits?.55:.2}">${tip("left view",iv.exits,iv.counts_exits)}</rect>`;
    return s;}).join("")).join("");
  const ticks=slots.map((slot,i)=>(n<=7||i%Math.ceil(n/7)===0)
    ?`<text x="${(gx(i)+bw*m/2).toFixed(1)}" y="${H-24}" text-anchor="middle"
       class="ax">${slot.split("|")[1].slice(5)}</text>`:"").join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="arrivals and exits between captures, per selection">
    ${grid}${bars}${ticks}
    <text x="${L}" y="${H-8}" class="ax">labelled by the capture each gap ends at</text></svg>`;
  $("churnlgd").innerHTML=cols.map((c,i)=>
    `<span><i style="background:${COLORS[i%6]}"></i>${esc(c.label)}</span>`).join("")
    +`<span class="flat">solid up = arrived &middot; faded down = left view</span>`;
  // The verdict sentence per selection, which is where the comparison
  // actually lands: two trims can churn the same number of cars and mean
  // completely different things once the rate and the direction are stated.
  $("churnsum").innerHTML=cols.map((c,i)=>
    `<div><span class="swatch" style="background:${COLORS[i%6]}"></span>
      <span class="nm"><b>${esc(c.label)}</b> &mdash;
      ${esc(c.turn_usable?c.turn_verdict:c.turn_basis)}</span></div>`).join("");
}

/* every selection's listings on one price-vs-odometer plane, colour-coded -
   this is where "dearer" turns into "dearer for the same wear" */
function scatterChart(d){
  const el=$("scatter");
  const sets=d.columns.map((c,i)=>({label:c.label,color:COLORS[i%6],
    pts:c.scatter||[],total:c.scatter_total||0}));
  const all=sets.flatMap(s=>s.pts);
  const shown=all.length,total=sets.reduce((a,s)=>a+s.total,0);
  $("scatspan").textContent=shown
    ?`— ${shown<total?`newest ${shown} of ${total}`:`${shown}`} points`:"";
  if(!all.length){noData(el,"no listing here has both a price and an odometer");return;}
  const W=560,H=240,L=62,R=14,T=14,B=34;
  const sx=scale(Math.min(...all.map(p=>p.odo)),Math.max(...all.map(p=>p.odo)),L,W-R,0.03);
  const sy=scale(Math.min(...all.map(p=>p.price)),Math.max(...all.map(p=>p.price)),H-B,T,0.06);
  const body=sets.map(s=>s.pts.map(p=>
    `<circle cx="${sx.at(p.odo).toFixed(1)}" cy="${sy.at(p.price).toFixed(1)}" r="2.6"
      fill="${s.color}" opacity=".55"><title>${esc(s.label)}${p.year?" "+p.year:""}:
 ${money(p.price)} at ${km(p.odo)}</title></circle>`).join("")).join("");
  const xt=vpaTicks(sx.lo,sx.hi,5).map(v=>{return `
    <text x="${sx.at(v).toFixed(1)}" y="${H-10}" text-anchor="middle" class="ax">${km(v)}</text>`;}).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="price against odometer for each selection">
    ${yGrid(sy,L,R,W,money)}${body}${xt}</svg>`;
}

/* dealer vs private as a proportion bar per selection - the counts are in
   the table, the SHAPE is what is worth seeing side by side */
function sellerChart(d){
  const el=$("sellers");
  const cols=d.columns.filter(c=>(c.dealer||0)+(c.private||0)>0);
  if(!cols.length){noData(el,"no selection here records who is selling");return;}
  const W=1180,rowH=30,L=Math.min(260,Math.max(120,...cols.map(c=>c.label.length*6.4))),
        R=140,T=10,H=T+cols.length*rowH+12;
  const body=cols.map((c,i)=>{
    const cy=T+i*rowH,tot=c.dealer+c.private,w=W-L-R;
    const dw=w*c.dealer/tot;
    return `<rect x="${L}" y="${cy+6}" width="${dw.toFixed(1)}" height="18" fill="#3060a0"
        opacity=".8" rx="2"><title>${esc(c.label)}: ${c.dealer} dealer of ${tot}</title></rect>
      <rect x="${(L+dw).toFixed(1)}" y="${cy+6}" width="${(w-dw).toFixed(1)}" height="18"
        fill="#88aa88" opacity=".85" rx="2"><title>${esc(c.label)}: ${c.private} private of ${tot}</title></rect>
      <text x="${L-8}" y="${cy+19}" text-anchor="end" class="ax" style="font-size:11px">${esc(c.label)}</text>
      <text x="${W-R+8}" y="${cy+19}" class="ax" style="font-size:11px">${
        Math.round(c.dealer/tot*100)}% dealer &middot; ${c.dealer}/${c.private}</text>`;}).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="dealer against private share per selection">${body}</svg>
   <div class="lgd"><span><span class="swatch" style="background:#3060a0"></span>dealer</span>
    <span><span class="swatch" style="background:#88aa88"></span>private</span></div>
   <div class="hint note">Selections with no seller recorded are left out. A dealer-heavy
    selection usually carries a higher median for the same car.</div>`;
}

/* one bell curve per selection on a shared price axis. Each is scaled to its
   own peak, NOT to its sample size: the question here is "what shape is this
   trim's pricing", and scaling by n would flatten a 30-car trim into the
   floor beside a 700-car one and say nothing about either. The car count is
   in the legend instead. */
function bellChart(d){
  const el=$("bell");
  const sets=d.columns.filter(c=>c.curve&&c.curve.usable)
    .map((c,i)=>({label:c.label,curve:c.curve,
                  color:COLORS[d.columns.indexOf(c)%6]}));
  const skipped=d.columns.filter(c=>!c.curve||!c.curve.usable);
  if(!sets.length){
    noData(el,"no selection here has enough prices for a curve");
    $("belllgd").innerHTML="";$("bellspan").textContent="";return;}
  const W=1180,H=300,L=64,R=16,T=22,B=40;
  const x0=Math.min(...sets.map(s=>s.curve.x[0]));
  const x1=Math.max(...sets.map(s=>s.curve.x[s.curve.x.length-1]));
  const X=v=>L+(W-L-R)*(v-x0)/(x1-x0||1);
  const Y=f=>T+(H-T-B)*(1-f);
  const grid=[0,.5,1].map(f=>`
    <line x1="${L}" y1="${Y(f).toFixed(1)}" x2="${W-R}" y2="${Y(f).toFixed(1)}" stroke="#eef0f4"/>`).join("");
  const body=sets.map(s=>{
    const peak=Math.max(...s.curve.y)||1;
    const path=s.curve.x.map((v,i)=>
      `${i?"L":"M"}${X(v).toFixed(1)},${Y(s.curve.y[i]/peak).toFixed(1)}`).join("");
    const med=s.curve.marks.find(m=>m.sd===0);
    return `<path d="${path}" fill="${s.color}" opacity=".10"
        stroke="none" transform="translate(0,0)"/>
      <path d="${path}" fill="none" stroke="${s.color}" stroke-width="2.5"/>
      <line x1="${X(med.price).toFixed(1)}" y1="${Y(0).toFixed(1)}"
        x2="${X(med.price).toFixed(1)}" y2="${Y(1).toFixed(1)}"
        stroke="${s.color}" stroke-width="1" stroke-dasharray="3 3"/>
      <circle cx="${X(med.price).toFixed(1)}" cy="${Y(1).toFixed(1)}" r="4" fill="${s.color}">
        <title>${esc(s.label)}: middle ${money(med.price)}, ${s.curve.n} cars,
 68% of them between ${money(s.curve.marks.find(m=>m.sd===-1).price)} and
 ${money(s.curve.marks.find(m=>m.sd===1).price)}</title></circle>`;}).join("");
  const xt=vpaTicks(x0,x1,6).map(v=>{return `
    <text x="${X(v).toFixed(1)}" y="${H-14}" text-anchor="middle" class="ax">${vpaAxMoney(v)}</text>`;}).join("");
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
    aria-label="fitted price distribution for each selection">${grid}${body}${xt}</svg>`;
  $("belllgd").innerHTML=sets.map(s=>{
    const lo=s.curve.marks.find(m=>m.sd===-1).price,
          hi=s.curve.marks.find(m=>m.sd===1).price;
    return `<span><span class="swatch" style="background:${s.color}"></span>${esc(s.label)}
      <span class="flat">${s.curve.n} cars &middot; middle ${money(s.curve.marks.find(m=>m.sd===0).price)}
      &middot; 68% within ${money(lo)}&ndash;${money(hi)}</span></span>`;}).join("");
  $("bellspan").innerHTML=`&mdash; each scaled to its own peak, so shapes are
    comparable and heights are not`
    +(skipped.length?` &middot; <span class="flat">${skipped.length} selection(s)
      have too few prices for a curve</span>`:"");
}

function drawCharts(){
  if(!DATA)return;
  monthChart(DATA);bellChart(DATA);boxChart(DATA);barChart(DATA);
  supplyChart(DATA);churnChart(DATA);scatterChart(DATA);sellerChart(DATA);
}
$("metric").onchange=()=>barChart(DATA);
$("showFc").onchange=()=>monthChart(DATA);

/* ---------- picked selections ---------- */
function renderPicks(){
  $("picks").innerHTML=PICKS.length?PICKS.map((p,i)=>
    `<span class="pick"><span class="swatch" style="background:${COLORS[i%6]}"></span>
      <b>${esc(p)}</b><button class="x" data-p="${esc(p)}" title="remove">&times;</button></span>`).join("")
    :'<span class="flat" style="font-size:12.5px">nothing picked yet</span>';
  document.querySelectorAll(".x").forEach(b=>b.onclick=()=>{
    PICKS=PICKS.filter(p=>p!==b.dataset.p);apply();});
  const max=(DATA&&DATA.max)||6;
  $("pickhint").textContent=PICKS.length>=max
    ?`That is the maximum of ${max} - remove one to add another.`
    :`${max-PICKS.length} more can be added.`;
}
function pick(label){
  const max=(DATA&&DATA.max)||6;
  if(PICKS.includes(label)||PICKS.length>=max)return;
  PICKS.push(label);apply();
}

/* selection lives in the URL, so a comparison can be bookmarked or shared
   with yourself in another tab */
function pushUrl(){
  const q=new URLSearchParams();PICKS.forEach(p=>q.append("sel",p));
  if($("live").value==="1")q.set("live","1");
  history.replaceState(null,"",PICKS.length?"/compare?"+q:"/compare");
}
async function apply(){
  pushUrl();renderPicks();
  const q=new URLSearchParams();PICKS.forEach(p=>q.append("sel",p));
  if($("live").value==="1")q.set("live","1");
  const got=await vpaLatest("compare","/api/compare?"+q);
  if(!got)return;                        // a newer pick has taken over
  DATA=got;
  if(DATA.unknown&&DATA.unknown.length){
    PICKS=PICKS.filter(p=>!DATA.unknown.some(u=>u.toLowerCase()===p.toLowerCase()));
    alert("Not a selection that exists: "+DATA.unknown.join(", "));
    pushUrl();renderPicks();}
  $("basis").textContent=DATA.basis||"";
  $("maxn").textContent=DATA.max||6;
  renderCompare();renderCatalogue();
}

/* ---------- catalogue (the Models sheet) ---------- */
function renderCatalogue(){
  if(!CAT)return;
  const q=$("filter").value.trim().toLowerCase(),lvl=$("lvl").value;
  let rows=CAT.rows.filter(r=>(!lvl||r.level===lvl)
    &&(!q||r.label.toLowerCase().includes(q)));
  rows.sort((a,b)=>{const x=a[catSort],y=b[catSort];
    if(x==null&&y==null)return 0;if(x==null)return 1;if(y==null)return -1;
    return (x>y?1:x<y?-1:0)*catDir;});
  const max=(DATA&&DATA.max)||6;
  $("cat").querySelector("tbody").innerHTML=rows.map(r=>{
    const on=PICKS.includes(r.label);
    return `<tr>
    <td>${esc(r.label)}</td><td><span class="tag">${r.level}</span></td>
    <td class="num">${r.n}</td><td class="num">${r.full}</td>
    <td class="num">${r.brief}</td><td class="num">${r.vehicles}</td>
    <td class="num">${r.days}</td><td class="num">${money(r.median)}</td>
    <td class="num">${money(r.lo)}</td><td class="num">${money(r.hi)}</td>
    <td class="num">${r.odo==null?"":km(r.odo)}</td><td>${r.last}</td>
    <td><button class="ghost mini" data-add="${esc(r.label)}" ${on||PICKS.length>=max?"disabled":""}
      style="min-width:74px">${on?"added":"compare"}</button></td></tr>`;}).join("")
    ||'<tr><td colspan="13" class="empty">nothing matches</td></tr>';
  document.querySelectorAll("[data-add]").forEach(b=>b.onclick=()=>pick(b.dataset.add));
  $("catspan").textContent=`— ${rows.length} of ${CAT.rows.length}`;
}
document.querySelectorAll("#cat th.s").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;
  if(catSort===k)catDir=-catDir;
  else{catSort=k;catDir=(k==="label"||k==="level")?1:-1;}
  renderCatalogue();});
$("filter").oninput=renderCatalogue;
$("lvl").onchange=renderCatalogue;

$("addBtn").onclick=()=>{const v=$("add").value.trim();if(v){pick(v);$("add").value="";}};
$("add").onkeydown=e=>{if(e.key==="Enter")$("addBtn").click();};
$("clearBtn").onclick=()=>{PICKS=[];apply();};
// the live filter is applied server-side BEFORE any stat is computed, so the
// whole page describes one population rather than a mix of two
$("live").onchange=()=>{
  const on=$("live").value==="1";
  // said beside the control that caused it - it used to overwrite the page
  // subtitle, and the 20-second status poll then silently overwrote it back
  $("livehint").textContent=on?"Latest capture only: cars seen in each selection's "
    +"most recent capture. Their full price history is kept, so the trend and "
    +"forecast still work. Not proof they are still for sale.":"";
  apply();};
$("topBtn").onclick=()=>{
  if(!CAT)return;
  PICKS=CAT.rows.filter(r=>r.level==="line").slice(0,4).map(r=>r.label);
  apply();};


/* ---------- stay in step with the database ---------- */
let seenVersion=null;
async function checkFresh(){
  let s;              // after Shut down the poll simply stops answering
  try{s=vpaApplyStatus(await fetch("/api/status").then(r=>r.json()));}catch(e){return;}
  if(seenVersion===null)seenVersion=s.data_version;
  else if(s.data_version!==seenVersion){
    seenVersion=s.data_version;
    CAT=await fetch("/api/compare/catalogue").then(r=>r.json());
    fillList();await apply();
  }
}
function fillList(){
  $("alllist").innerHTML=CAT.rows.map(r=>
    `<option value="${esc(r.label)}">`).join("");
}

(async()=>{
  CAT=await fetch("/api/compare/catalogue").then(r=>r.json());
  if(!CAT.rows.length){$("subline").textContent="no data yet - import something first";
    renderCompare();return;}
  fillList();
  const p=new URLSearchParams(location.search);
  if(p.get("live")==="1")$("live").value="1";
  PICKS=p.getAll("sel").slice(0,MAXGUESS);
  if(!PICKS.length)PICKS=CAT.rows.filter(r=>r.level==="line").slice(0,2).map(r=>r.label);
  await checkFresh();await apply();
  window.addEventListener("focus",checkFresh);
  setInterval(checkFresh,20000);
})();
__CHROME_JS__
</script></div></body></html>
"""


from .ui import chrome as _chrome   # noqa: E402  (page assembly, not logic)

PAGE = _chrome(_PAGE, "compare")
