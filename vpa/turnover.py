"""Turnover — what happens to the cars BETWEEN captures.

The supply chart answers "how many were up on each day". This answers the
question underneath it: of the cars that were up, which ones left, which ones
are new, how fast does this trim clear, and were the ones that left priced
differently from the ones that stayed.

    engine only. No I/O, no writes, no HTML. `explore.py` calls it with a
    selection it has already made and filtered.

---------------------------------------------------------------- the wording

**Nothing here ever says "sold".** A listing that stops appearing may have
sold, been withdrawn, expired, been re-listed under a new ad id, or simply
not been inside the search you happened to capture that day. The vocabulary
is deliberately *left the listings* / *exit* / *gone from view*, and every
duration is a **lower bound** — a car was almost always listed before your
first capture of it, and its exit is only located to the width of the gap
between the two captures that straddle it.

This is the same discipline as `analyze.live_keys()`, which refuses to call
its own result "still for sale". The numbers below are a good clearance
signal; they are not a sales ledger.

------------------------------------------------------- why this works now

NOTES.md §5.3 recorded turnover as *not measurable*: identity broke across
capture eras (v2 `BASE-`/`IMG-` ids, then real site ad ids, then a pasted
batch with no ids at all), so cross-day joins failed. That limitation was
real and is now only partly true, which is what this module exploits:

  * the cohort is restricted to keys carrying a **site-issued ad id**
    (`merge.site_id_prefixes()`). Derived ids — `IMG-` from an image URL,
    `BASE-` from the v2 database, and the id-less odometer fingerprint —
    cannot be rejoined across days with any confidence, so they are excluded
    and counted out loud rather than quietly diluting the rate;
  * captures are split into **spans** at any point where identity continuity
    fails (below). Measured on the real database, the *model line* Ford
    Falcon has pairs of consecutive captures sharing ZERO cars — the searches
    covered different eras of the car — while its individual *trims* carry
    82-100% of their cars from one capture to the next. So the same data that
    cannot support a line-level turnover figure supports a trim-level one
    comfortably, and the span rule is what tells those two cases apart
    instead of averaging them together into a number that is wrong twice.

---------------------------------------------------------------- the gating

Two consecutive captures are **linked** when they share at least one car and
the shared set is at least `MIN_LINK` of the smaller capture. A pair that
fails is not a market event — it is a change of what you searched for, and
counting it would report a 100% clearance rate for a trim nobody bought.
Failing pairs end the span, are reported as `breaks`, and their apparent
exits are never counted.

Filters are the other way to invent an exit: a car whose price drops out of
"under $40,000" looks like it left. So the caller passes the SAME selection
before filtering as `full`, and any "exit" that is still visible there on a
later day is vetoed and counted under `vetoed`.
"""
from __future__ import annotations

import copy
import hashlib
import math
import threading
from collections import OrderedDict
from datetime import date

import numpy as np
import pandas as pd

from . import links
from .merge import site_id_prefixes

#: shared cars, as a fraction of the smaller capture, before two consecutive
#: captures are treated as covering the same search. Chosen against the real
#: data, where genuine trim-level pairs sit at 0.45-1.00 and scope changes sit
#: at 0.00 — anywhere in the wide gap between would do, so the value is not
#: delicate. Deliberately low: a real three-week gap CAN clear half a trim.
MIN_LINK = 0.25
#: distinct id-bearing cars at risk before any rate is quoted at all
MIN_COHORT = 6
#: captures in a span before it can describe anything
MIN_CAPTURES = 2
#: rows of "what left" sent to the page
MAX_DEPARTED = 200
#: how much bigger one capture may be than its neighbour before the pair is
#: treated as a change of SCOPE rather than of stock. See `_scope()`.
SCOPE_RATIO = 1.5
#: below this many days of comparable coverage, or this many exits, the
#: 30-day rate is an extrapolation and is labelled as one
THIN_DAYS = 14
THIN_EXITS = 5


# ------------------------------------------------------------------ helpers
def _num(v):
    """pandas/numpy scalar -> plain JSON-safe python (None for NaN)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        return None
    f = float(v)
    return int(f) if f.is_integer() else round(f, 2)


def _d(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _gap(a: str, b: str) -> int:
    return (_d(b) - _d(a)).days


def _median(vals):
    vals = [f for f in (_num(v) for v in vals) if f is not None]
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    return _num(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)


def cohort(df: pd.DataFrame) -> pd.DataFrame:
    """The rows whose identity is a site-issued ad id, and can therefore be
    rejoined across capture days. See the module docstring for why derived
    ids are excluded rather than merely distrusted."""
    if df.empty or "listing_id" not in df.columns:
        return df.iloc[0:0]
    # A plain pass rather than .astype("string").str.startswith: this runs
    # once per trim per page load, and pandas' string machinery cost ~4 ms a
    # call for a few hundred values. Missing ids are None/NaN, never str, so
    # they fall out exactly as the old fillna("") did.
    pre = site_id_prefixes()
    mask = np.fromiter((isinstance(v, str) and bool(pre) and v.startswith(pre)
                        for v in df["listing_id"]), dtype=bool, count=len(df))
    return df[mask]


def _day_sets(df: pd.DataFrame) -> dict:
    """day -> set of keys seen that day, in day order.

    One pass over two columns. It used to be a groupby with a Python loop
    per group, which on a model line with 150 trims ran a few hundred
    thousand pandas indexing calls per page load for what is a dict of sets.
    Same result: groupby skips a missing day and sorts the days, so this does
    both."""
    out = {}
    if df.empty:
        return out
    for day, k in zip(df["day"], df["key"]):
        if pd.isna(day):
            continue
        out.setdefault(str(day), set()).add(str(k))
    return {d: out[d] for d in sorted(out)}


def _scope(n_a: int, n_b: int) -> str:
    """Whether a pair of captures can carry an exit count, an arrival count,
    or both — decided by their sizes, and deliberately ASYMMETRIC.

    Exits and arrivals fail in opposite directions, so one verdict for the
    pair would throw away half the evidence:

      * the search WIDENED (b much bigger than a) — the flood of unseen cars
        is your search reaching further, not the market restocking, so the
        arrival count is worthless. The exit count is *better* than usual:
        a wider net that still fails to find a car is stronger evidence it
        has gone.
      * the search NARROWED (a much bigger than b) — the mirror image. Cars
        "missing" from the smaller capture were mostly never looked for, so
        the exit count is worthless while arrivals still mean something.

    Measured need: on the real database, Ford Falcon went from 66 cars on
    06/08 to 168 on 10/08 — 113 "arrivals" in four days against a standing
    stock of 66. No market does that; the 10/08 sweep was simply broader.
    Its ELEVEN exits over the same four days are perfectly good data.
    """
    if n_a and n_b > n_a * SCOPE_RATIO:
        return "widened"
    if n_b and n_a > n_b * SCOPE_RATIO:
        return "narrowed"
    return "steady"


def spans(days: dict) -> tuple[list, list]:
    """Split the capture days into runs of CONTINUOUS coverage.

    Returns (spans, breaks). A break carries both counts and the overlap, so
    the page can say "these two captures shared no cars, so they are not
    comparable" rather than showing a cliff and letting it be read as the
    market emptying.
    """
    ds = sorted(days)
    if not ds:
        return [], []
    runs, breaks, cur = [], [], [ds[0]]
    for a, b in zip(ds, ds[1:]):
        A, B = days[a], days[b]
        carry = len(A & B)
        small = max(1, min(len(A), len(B)))
        link = carry / small
        if carry >= 1 and link >= MIN_LINK:
            cur.append(b)
        else:
            breaks.append({"from": a, "to": b, "a": len(A), "b": len(B),
                           "carry": carry, "link": round(link, 2),
                           "days": _gap(a, b)})
            runs.append(cur)
            cur = [b]
    runs.append(cur)
    return runs, breaks


# -------------------------------------------------------------------- engine
def turnover(sub: pd.DataFrame, full: pd.DataFrame | None = None,
             label: str = "") -> dict:
    """Churn for one selection, measured on its latest continuous span.

    `sub` is the selection as shown on the page (filters applied). `full` is
    the same selection BEFORE filters; it is used only to veto false exits —
    see the module docstring. Passing None skips the veto and says so.

    Every returned figure is JSON-safe. `usable` False always carries a
    `reason` in plain words, because "not enough data" printed on screen is a
    better answer than a rate computed from four cars.
    """
    # Every key the payload can ever carry is present from the start, set to
    # the value that means "nothing measured". A refusal returning a smaller
    # dict than a success is how a caller ends up with a KeyError in the one
    # case the module exists to handle gracefully.
    out = {
        "usable": False, "reason": "", "label": label,
        "cohort_cars": 0, "excluded_no_id": 0, "captures": 0,
        "breaks": [], "spans": [], "veto_checked": full is not None,
        "span": None, "daily": [], "intervals": [], "vetoed": 0,
        "at_risk": 0, "exits": 0, "arrivals": 0, "arrivals_raw": 0,
        "still": 0, "car_days": 0, "rated_days": 0, "thin": True,
        "hazard": None, "rate30": None, "half_life": None,
        "median_days_listed": None, "median_days_est": None,
        "median_stock_age": None, "censored_share": 0, "resolution": None,
        "departed": [], "departed_total": 0, "latest": {},
        "price_signal": {}, "notes": [], "verdict": "", "cut_days": [],
    }
    if sub is None or sub.empty:
        out["reason"] = "nothing in this selection"
        return out

    coh = cohort(sub)
    out["cohort_cars"] = int(coh["key"].nunique()) if not coh.empty else 0
    out["excluded_no_id"] = int(sub["key"].nunique() - out["cohort_cars"])
    if coh.empty:
        out["reason"] = ("no listing here carries a site ad id, so the same "
                         "car cannot be recognised from one capture to the next")
        return out

    days = _day_sets(coh)
    # captures saved from a search that was cut short (sweeps.py): cars
    # missing from them may simply have been past the last page saved
    cut_days = (set(str(d) for d in coh.loc[coh["cut"].astype(bool), "day"].dropna())
                if "cut" in coh.columns else set())
    out["cut_days"] = sorted(cut_days)
    runs, breaks = spans(days)
    out["captures"] = len(days)
    out["breaks"] = breaks
    out["spans"] = [{"start": r[0], "end": r[-1], "captures": len(r),
                     "days": _gap(r[0], r[-1])} for r in runs]
    span = runs[-1] if runs else []

    # ---- per-day composition, for EVERY capture day and not only the rated
    # span. "How many of these were already here" is worth drawing even when
    # the rate itself has to be refused, and a day sitting the wrong side of
    # a coverage break is exactly the day a reader most needs marked.
    ds = sorted(days)
    broke = {(b["from"], b["to"]) for b in breaks}
    inspan = set(span)
    seen_before, daily = set(), []
    for i, d in enumerate(ds):
        D = days[d]
        prev = ds[i - 1] if i else None
        daily.append({
            "d": d, "n": len(D),
            "carry": None if prev is None else len(D & days[prev]),
            "new": None if prev is None else len(D - seen_before),
            "exits": 0, "rated": False,
            "break_before": bool(prev and (prev, d) in broke),
            "inspan": d in inspan, "cut": d in cut_days,
        })
        seen_before |= D
    out["daily"] = daily
    by_d = {r["d"]: r for r in daily}

    if len(span) < MIN_CAPTURES:
        out["reason"] = (
            "only one usable capture" if len(days) < 2 else
            "the last two captures of this selection share no cars, so they "
            "cover different searches and cannot be compared")
        return out

    # ---- membership, forwards and backwards, so "gone" means gone from the
    # WHOLE remainder of the span and not merely from the very next capture.
    # A car missing on one day and back on the next was never gone; it was a
    # capture that did not reach it.
    later, seen = {}, set()
    acc = set()
    for d in reversed(span):
        acc = acc | days[d]
        later[d] = set(acc)              # this day and every day after it
    earlier = {}
    for d in span:
        earlier[d] = set(seen)           # every day BEFORE this one
        seen |= days[d]

    first_in_span = {}
    for d in span:
        for k in days[d]:
            first_in_span.setdefault(k, d)

    # ---- the veto set: where a filtered-out row is still visible
    full_days = _day_sets(cohort(full)) if full is not None else {}

    def still_visible_after(key: str, day: str) -> bool:
        return any(key in ks for d, ks in full_days.items() if d > day)

    # ---- one row per interval; this is the churn chart's data
    intervals, exits_by_day, vetoed = [], {}, 0
    car_days = 0.0
    total_exits = total_arrivals = 0
    rated_days = 0
    for a, b in zip(span, span[1:]):
        A, B = days[a], days[b]
        elapsed = max(1, _gap(a, b))
        # a capture cut short is treated like a narrowed one: the cars it
        # lacks were not looked at, so they are shown but never counted gone
        scope = "cut short" if b in cut_days else _scope(len(A), len(B))
        gone = {k for k in A - later[b] if not still_visible_after(k, a)}
        vetoed += len(A - later[b]) - len(gone)
        arrived = B - earlier[b]
        # a narrowed pair's "exits" are cars the smaller search never looked
        # for, so they are shown but never rated; a widened pair's arrivals
        # likewise. Both stay visible in the table with their scope named.
        counts_exits = scope not in ("narrowed", "cut short")
        counts_arrivals = scope != "widened"
        exits_by_day[a] = gone if counts_exits else set()
        if counts_exits:
            car_days += len(A) * elapsed
            total_exits += len(gone)
            rated_days += elapsed
        if counts_arrivals:
            total_arrivals += len(arrived)
        intervals.append({
            "from": a, "to": b, "elapsed": elapsed,
            "start_n": len(A), "end_n": len(B), "scope": scope,
            "carry": len(A & B), "exits": len(gone), "arrivals": len(arrived),
            "counts_exits": counts_exits, "counts_arrivals": counts_arrivals,
            "rate": round(len(gone) / len(A) * 100, 1) if A else None,
        })
    out["vetoed"] = vetoed
    out["intervals"] = intervals
    for i in intervals:
        row = by_d.get(i["from"])
        if row is not None:
            row["exits"] = len(exits_by_day.get(i["from"], ()))
            row["rated"] = i["counts_exits"]

    rated = [i for i in intervals if i["counts_exits"]]
    if not rated:
        out["reason"] = (
            "every capture after the first came from a search that showed more "
            "cars than were saved, so cars missing from them may simply have "
            "been past the last page"
            if all(i["scope"] == "cut short" for i in intervals) else
            "every pair of captures here changed size too much to compare — the "
            "searches got narrower, so cars missing from the smaller one were "
            "never looked for")
        out["span"] = {"start": span[0], "end": span[-1],
                       "captures": len(span), "days": _gap(span[0], span[-1])}
        return out

    at_risk = len(set().union(*[days[i["from"]] for i in rated]))
    if at_risk < MIN_COHORT:
        out["reason"] = (f"only {at_risk} car(s) with a site ad id were up "
                         f"before the latest capture — too few to rate")
        out["span"] = {"start": span[0], "end": span[-1],
                       "captures": len(span), "days": _gap(span[0], span[-1])}
        return out

    # ---- pooled hazard. Intervals here are 1 to 22 days long, so a plain
    # exits/at-risk average would weight a two-day gap the same as a
    # three-week one. Exits per car-day does not, and it is the only form in
    # which two trims captured on different rhythms can be compared.
    hazard = (total_exits / car_days) if car_days else 0.0
    rate30 = (1 - math.exp(-hazard * 30)) * 100 if hazard > 0 else 0.0
    half_life = (math.log(2) / hazard) if hazard > 0 else None

    # ---- what left, and what is still standing
    # Vectorised rather than iterrows/per-car sorts: a model line has
    # thousands of rows, and these two lookups were most of a page load.
    priced = sub[sub["price_aud"].notna()]
    price_by = {(str(k), str(d)): _num(v) for k, d, v in
                zip(priced["key"], priced["day"], priced["price_aud"])}
    day_median = {}
    for day, g in sub.dropna(subset=["price_aud"]).groupby("day"):
        day_median[str(day)] = _num(g["price_aud"].median())
    # each car's latest sighting: one stable sort, then the last row per car
    tail = sub.sort_values("day", kind="stable").groupby("key").tail(1)
    last_row = {str(k): r for k, r in zip(tail["key"], tail.to_dict("records"))}

    def rel(price, day):
        m = day_median.get(day)
        if not price or not m:
            return None
        return round((price / m - 1) * 100, 1)

    # A car's time on the market is bracketed, never known. It was listed by
    # the capture that first saw it and gone by the capture that first missed
    # it, so the truth lies between (last - first) and (next - the capture
    # BEFORE first). Reporting only the floor makes every car first seen and
    # last seen on the same day read as "listed 0 days", which is how a lively
    # trim ends up looking instantaneous. Both edges are carried, and the
    # midpoint is what the headline uses.
    departed = []
    for day, gone in exits_by_day.items():
        i = span.index(day)
        nxt = span[i + 1]
        for k in gone:
            r = last_row.get(k)
            if r is None:
                continue
            f = first_in_span[k]
            fi = span.index(f)
            prev = span[fi - 1] if fi > 0 else None
            floor = _gap(f, day)
            ceil = _gap(prev, nxt) if prev else None
            price = price_by.get((k, day))
            departed.append({
                "key": k, "last_day": day, "first_day": f,
                "days_listed": floor, "days_max": ceil,
                "days_est": (None if ceil is None else round((floor + ceil) / 2, 1)),
                "left_before": nxt, "window": _gap(day, nxt),
                "censored": f == span[0],
                "price": price, "rel_pct": rel(price, day),
                "odometer_km": _num(r["odometer_km"]),
                "year": None if pd.isna(r["year"]) else int(r["year"]),
                "variant": None if pd.isna(r["variant"]) else str(r["variant"]),
                "state": None if pd.isna(r["state"]) else str(r["state"]),
                "seller_type": (None if pd.isna(r["seller_type"])
                                else str(r["seller_type"])),
                "title": None if pd.isna(r["raw_title"]) else str(r["raw_title"]),
                "url": links.ad_url(r["url"], r.get("source")),
            })
    # the key breaks exact ties (same day, same price). Without it the order
    # of tied cars came from iterating a set of strings, which Python
    # reshuffles on every restart - the same data drew a different list.
    departed.sort(key=lambda v: (v["last_day"], v["price"] or 0, v["key"]),
                  reverse=True)

    end = span[-1]
    standing = []
    for k in days[end]:
        r = last_row.get(k)
        if r is None:
            continue
        f = first_in_span[k]
        price = price_by.get((k, end))
        standing.append({"key": k, "first_day": f, "age": _gap(f, end),
                         "censored": f == span[0], "price": price,
                         "rel_pct": rel(price, end)})

    # ---- the signal worth having: were the cars that left priced
    # differently from the cars still up. Both sides are stated relative to
    # the median of the SAME day, so a market-wide drift between the two
    # dates cannot masquerade as a discount.
    exit_rel = _median([v["rel_pct"] for v in departed])
    still_rel = _median([v["rel_pct"] for v in standing])
    price_signal = {
        "exit_median": _median([v["price"] for v in departed]),
        "still_median": _median([v["price"] for v in standing]),
        "exit_rel": exit_rel, "still_rel": still_rel,
        "spread": (None if exit_rel is None or still_rel is None
                   else round(exit_rel - still_rel, 1)),
        "exit_odo": _median([v["odometer_km"] for v in departed]),
        "still_odo": _median([last_row[v["key"]]["odometer_km"]
                              for v in standing if v["key"] in last_row]),
        "exit_n": len(departed), "still_n": len(standing),
    }

    last = intervals[-1] if intervals else {}
    thin = rated_days < THIN_DAYS or total_exits < THIN_EXITS
    out.update({
        "usable": True,
        "span": {"start": span[0], "end": span[-1], "captures": len(span),
                 "days": _gap(span[0], span[-1])},
        "at_risk": at_risk, "exits": total_exits,
        "arrivals": total_arrivals,
        "arrivals_raw": sum(i["arrivals"] for i in intervals),
        "still": len(days[end]),
        "car_days": int(car_days), "rated_days": rated_days,
        "thin": thin,
        "hazard": round(hazard, 5),
        "rate30": round(rate30, 1),
        "half_life": None if half_life is None else round(half_life, 1),
        "median_days_listed": _median([v["days_listed"] for v in departed]),
        "median_days_est": _median([v["days_est"] for v in departed]),
        "median_stock_age": _median([v["age"] for v in standing]),
        "censored_share": (round(sum(1 for v in departed if v["censored"])
                                 / len(departed) * 100) if departed else 0),
        "resolution": _median([i["elapsed"] for i in intervals]),
        "departed": departed[:MAX_DEPARTED],
        "departed_total": len(departed),
        "price_signal": price_signal,
        "latest": {"from": last.get("from"), "to": last.get("to"),
                   "exits": last.get("exits"), "arrivals": last.get("arrivals"),
                   "net": (None if not last else
                           last["arrivals"] - last["exits"])},
    })
    out["notes"] = _notes(out)
    out["verdict"] = _verdict(out)
    return out


def _notes(t: dict) -> list[str]:
    """The caveats that must travel with the numbers, in the app's own voice."""
    n = []
    if t.get("excluded_no_id"):
        n.append(f"{t['excluded_no_id']} car(s) here carry no site ad id and "
                 f"are left out — they cannot be recognised across captures")
    if t.get("vetoed"):
        n.append(f"{t['vetoed']} apparent exit(s) were still visible in the "
                 f"unfiltered selection later, so they were filter artefacts "
                 f"and are not counted")
    if t.get("breaks"):
        b = t["breaks"][-1]
        n.append(f"coverage breaks at {b['from']}→{b['to']} (they share "
                 f"{b['carry']} car(s)); only captures since {t['span']['start']} "
                 f"are used")
    wide = [i for i in t.get("intervals", []) if i["scope"] == "widened"]
    narrow = [i for i in t.get("intervals", []) if i["scope"] == "narrowed"]
    if wide:
        n.append(f"{len(wide)} capture(s) were much bigger than the one "
                 f"before, so that jump is your search widening, not the "
                 f"market restocking — those arrivals are shown but not counted")
    if narrow:
        n.append(f"{len(narrow)} capture(s) were much smaller than the one "
                 f"before, so cars 'missing' from them were never looked for "
                 f"— those exits are shown but not rated")
    cut = [i for i in t.get("intervals", []) if i["scope"] == "cut short"]
    if cut:
        n.append(f"{len(cut)} capture(s) came from a search that showed more cars "
                 f"than were saved, so cars missing from them may simply have been "
                 f"past the last page — those exits are shown but not rated")
    res = t.get("resolution")
    if res:
        n.append(f"an exit is located to within about {res} day(s) — the usual "
                 f"gap between your captures")
    if t.get("thin"):
        n.append(f"only {t['rated_days']} day(s) of comparable coverage and "
                 f"{t['exits']} exit(s) behind the 30-day figure — it is an "
                 f"extrapolation, not a month you actually watched")
    if t.get("censored_share"):
        n.append(f"{t['censored_share']}% of the cars that left were already "
                 f"up at the first capture, so their time listed has no upper "
                 f"edge — they had been listed for an unknown while already")
    return n


def _verdict(t: dict) -> str:
    """One sentence a person can act on, or an honest shrug."""
    if not t.get("usable"):
        return t.get("reason", "")
    rate, hl = t["rate30"], t["half_life"]
    speed = ("very quickly" if rate >= 60 else "quickly" if rate >= 40 else
             "at a normal pace" if rate >= 20 else "slowly")
    s = (f"{'Roughly' if t['thin'] else 'About'} {rate:.0f}% of what is listed "
         f"leaves view every 30 days — stock turns over {speed}")
    if hl:
        s += f", half of it gone within {hl:.0f} days"
    est = t.get("median_days_est")
    if est:
        s += (f". A car that left had been listed about {est:.0f} days "
              f"(at least {t['median_days_listed']:.0f})")
    # Supply direction is only stated where BOTH sides of it were countable;
    # a widened capture inflates arrivals and would print "building" for a
    # trim that is actually emptying.
    counted = [i for i in t["intervals"] if i["counts_arrivals"] and i["counts_exits"]]
    if counted:
        arr = sum(i["arrivals"] for i in counted)
        ext = sum(i["exits"] for i in counted)
        net = arr - ext
        s += (f". Across the captures where both sides are comparable, {arr} "
              f"arrived and {ext} left, so supply is "
              + ("building" if net > 1 else "shrinking" if net < -1 else "steady"))
    else:
        s += (". Supply direction is not stated: every pair of captures here "
              "changed size too much for arrivals and exits to be read together")
    sp = t["price_signal"]["spread"]
    if sp is not None and abs(sp) >= 3:
        s += (f". The cars that left were priced {abs(sp):.0f}% "
              f"{'below' if sp < 0 else 'above'} the ones still up, "
              f"measured against each day's own median")
    return s + "."


# ------------------------------------------------------- trim vs trim vs line
#: by_variant results, keyed by a hash of the rows they were computed from.
#: Clicking from trim to trim within one model asks the same question of the
#: same rows every time - only the highlighted row differs - and answering it
#: afresh was the single largest cost of an Explore page load.
_BV_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_BV_MAX = 16
_BV_LOCK = threading.Lock()      # the server answers requests on threads


def _fingerprint(df: pd.DataFrame | None) -> str:
    """A hash of every value, column, dtype and index label in `df`.

    This, not a row count or an import counter, is the cache key: a manual
    correction changes a price without changing any count, and a key that
    missed it would serve the old answer. Any change to any cell of the
    input changes the key, so a stale hit is not possible - only a miss."""
    if df is None:
        return "none"
    h = hashlib.sha1()
    h.update(repr((list(df.columns), [str(t) for t in df.dtypes])).encode())
    h.update(pd.util.hash_pandas_object(df, index=True).to_numpy().tobytes())
    return h.hexdigest()


def by_variant(line: pd.DataFrame, selected=None,
               full_line: pd.DataFrame | None = None) -> dict:
    """by_variant_uncached, remembered per input. Returns a fresh copy every
    time, with `selected` applied to the copy, so no caller can alter what
    the next one is given."""
    try:
        key = _fingerprint(line) + ":" + _fingerprint(full_line)
    except Exception:             # unhashable input: just compute it
        key = None
    hit = None
    if key is not None:
        with _BV_LOCK:
            hit = _BV_CACHE.get(key)
            if hit is not None:
                _BV_CACHE.move_to_end(key)
    if hit is None:
        hit = by_variant_uncached(line, None, full_line)
        if key is not None:
            with _BV_LOCK:
                _BV_CACHE[key] = hit
                while len(_BV_CACHE) > _BV_MAX:
                    _BV_CACHE.popitem(last=False)
    out = copy.deepcopy(hit)
    picked = set(selected or [])
    for r in out["rows"]:
        r["selected"] = r["variant"] in picked
    return out


def by_variant_uncached(line: pd.DataFrame, selected=None,
                        full_line: pd.DataFrame | None = None) -> dict:
    """The same measurement for every trim of one model, side by side.

    Each trim is measured on ITS OWN span — trims are captured on different
    days, and forcing them onto a shared calendar would silently drop the
    ones that were not in the last sweep. Comparability comes from the
    per-car-day hazard, not from a shared window.

    The pooled row is the sum of the trims' exits over the sum of their
    car-days: the model line's own rate WITHOUT the line-level scope problem
    that makes a direct line-level measurement meaningless (module docstring).
    """
    out = {"rows": [], "pooled": None, "usable": False}
    if line is None or line.empty:
        return out
    picked = set(selected or [])
    fulls = ({str(v): g for v, g in full_line.groupby(full_line["variant"]
                                                      .fillna("(no trim)"))}
             if full_line is not None else {})
    exits = car_days = 0.0
    for v, g in line.groupby(line["variant"].fillna("(no trim)")):
        t = turnover(g, fulls.get(str(v)), label=str(v))
        row = {"variant": str(v), "selected": str(v) in picked,
               "usable": t["usable"], "reason": t.get("reason", ""),
               "cars": int(g["key"].nunique()),
               "cohort": t["cohort_cars"], "captures": t["captures"]}
        if t["usable"]:
            row.update({
                "at_risk": t["at_risk"], "exits": t["exits"],
                "arrivals": t["arrivals"], "still": t["still"],
                "rate30": t["rate30"], "half_life": t["half_life"],
                "thin": t["thin"],
                "median_days_listed": t["median_days_listed"],
                "median_days_est": t["median_days_est"],
                "median_stock_age": t["median_stock_age"],
                "spread": t["price_signal"]["spread"],
                "span": t["span"],
            })
            exits += t["exits"]
            car_days += t["car_days"]
        out["rows"].append(row)
    # fastest-clearing first; unusable trims sink to the bottom keeping their
    # reason, because "not enough data for this one" is itself worth seeing
    out["rows"].sort(key=lambda r: (not r["usable"], -(r.get("rate30") or 0)))
    if car_days:
        hz = exits / car_days
        out["pooled"] = {
            "exits": int(exits), "car_days": int(car_days),
            "rate30": round((1 - math.exp(-hz * 30)) * 100, 1),
            "half_life": round(math.log(2) / hz, 1) if hz > 0 else None,
            "trims": sum(1 for r in out["rows"] if r["usable"]),
        }
        out["usable"] = True
    return out
