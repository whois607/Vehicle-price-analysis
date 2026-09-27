"""The watchlist: specific cars you are following, and what happened to them.

Every other view here answers a question about a market — what is this trim
worth, how has it moved, which are cheap for their kilometres. This one
answers a question about *your* shortlist: the four cars you would actually
buy, and whether the last import changed anything about them.

Three things it is careful about:

  * **A star is an annotation, never data.** It lives in its own table, no
    statistic groups by it, and no median anywhere shifts because you starred
    something. Starring and unstarring are the only writes, and they touch
    nothing but `starred`.
  * **A car that has gone is the headline, not an error.** A star whose
    vehicle is no longer in the database, or whose listing was not in the
    latest capture, is reported as exactly that. "Gone" is what you want to
    know; a watchlist that quietly dropped it would hide the one event worth
    seeing.
  * **Rank uses the same gates as everything else.** `explore.CAR_MIN_PEERS`
    decides whether a position is stated at all, so a starred car in a
    four-car trim is not given a confident rank the rest of the app would
    refuse.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from . import links
from .store import Store, DEFAULT_DB


def star(db_path: str | Path = DEFAULT_DB, key: str = "",
         note: str | None = None, on: bool = True) -> dict:
    """Add or remove one star. The only write in this module."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "no car named"}
    with Store(db_path) as store:
        exists = store.conn.execute(
            "SELECT 1 FROM vehicles WHERE key = ?", (key,)).fetchone()
        if on:
            if not exists:
                return {"ok": False, "error": "that car is not in the database"}
            store.conn.execute(
                "INSERT INTO starred (key, starred_at, note) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET note = COALESCE(excluded.note, note)",
                (key, datetime.now().isoformat(sep=" ", timespec="seconds"), note))
        else:
            store.conn.execute("DELETE FROM starred WHERE key = ?", (key,))
        store.conn.commit()
        n = store.conn.execute("SELECT COUNT(*) FROM starred").fetchone()[0]
    return {"ok": True, "key": key, "starred": bool(on), "total": n}


def keys(db_path: str | Path = DEFAULT_DB) -> set[str]:
    """Just the starred keys — for pages that only need to mark rows."""
    with Store(db_path) as store:
        return {r[0] for r in store.conn.execute("SELECT key FROM starred")}


def _pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((float(now) - float(then)) / float(then) * 100, 1)


def watchlist_payload(db_path: str | Path = DEFAULT_DB) -> dict:
    """Every starred car, what it is doing, and where it sits in its own trim.

    `since_capture` is the comparison that answers "how do they stack up when
    new data is uploaded": for each car, what changed between the two most
    recent capture days OF ITS OWN TRIM. Its own trim, not the database's
    latest day — trims are captured on different days, and comparing a Falcon
    against the day you happened to sweep Audis would invent movements.
    """
    from . import analyze
    from .explore import CAR_MIN_PEERS, _frame, _num, _rank_in, _select

    with Store(db_path) as store:
        stars = [dict(r) for r in store.conn.execute(
            "SELECT * FROM starred ORDER BY starred_at DESC")]
    if not stars:
        return {"ok": True, "cars": [], "total": 0,
                "reason": "nothing starred yet - open a car and click the star "
                          "to follow it"}

    df = _frame(db_path)
    cars, gone = [], 0
    for s in stars:
        key = s["key"]
        own = df[df["key"] == key] if not df.empty else df
        item = {"key": key, "note": s["note"] or "",
                "starred_at": s["starred_at"]}
        if own.empty:
            # the vehicle was deleted, or merged into another identity
            gone += 1
            cars.append({**item, "present": False,
                         "label": "(no longer in the database)",
                         "why": "this car has been removed or merged into "
                                "another identity since you starred it"})
            continue

        row = own.sort_values("capture_date").iloc[-1]
        make, model = str(row["make"]), str(row["model"])
        variant = None if pd.isna(row["variant"]) else str(row["variant"])
        label = " ".join(x for x in (make, model, variant) if x)
        own_sorted = own.sort_values("capture_date")
        prices = [float(p) for p in own_sorted["price_aud"].dropna()]
        days = [str(d) for d in own_sorted["day"]]

        # its own trim, one price per car, the same pool the verdict card uses
        trim = _select(df, make, model, variant)
        pool = analyze.latest_per_vehicle(analyze.prep(trim, freq="MS"))
        peers = pool[pool["key"] != key]
        peer_prices = [float(p) for p in peers["price_aud"].dropna()]
        rank = _rank_in(peer_prices, _num(row["price_aud"]))

        trim_days = sorted(trim["day"].unique())
        last_day = trim_days[-1] if trim_days else None
        prev_day = trim_days[-2] if len(trim_days) > 1 else None
        live = last_day is not None and last_day in set(days)

        # what the newest capture of this trim did to this car
        since = {"capture_day": last_day, "previous_day": prev_day}
        if prev_day:
            def med(day):
                v = trim[trim["day"] == day]["price_aud"].dropna()
                return _num(v.median()) if len(v) else None
            since["trim_median"] = med(last_day)
            since["trim_median_before"] = med(prev_day)
            since["trim_move"] = _pct(since["trim_median"],
                                      since["trim_median_before"])
            was = own_sorted[own_sorted["day"] == prev_day]["price_aud"].dropna()
            now = own_sorted[own_sorted["day"] == last_day]["price_aud"].dropna()
            since["was"] = _num(was.iloc[-1]) if len(was) else None
            since["now"] = _num(now.iloc[-1]) if len(now) else None
            since["move"] = _pct(since["now"], since["was"])
            since["seen_now"] = bool(len(now))
            since["seen_before"] = bool(len(was))
            # the only comparison that matters on a shortlist: did THIS car
            # move differently from the trim it sits in?
            if since["move"] is not None and since["trim_move"] is not None:
                since["vs_trim"] = round(since["move"] - since["trim_move"], 1)

        cars.append({
            **item, "present": True, "label": label,
            "make": make, "model": model, "variant": variant,
            "year": None if pd.isna(row["year"]) else int(row["year"]),
            "state": None if pd.isna(row["state"]) else str(row["state"]),
            "seller_type": (None if pd.isna(row["seller_type"])
                            else str(row["seller_type"])),
            "url": links.ad_url(row["url"], row.get("source")),
            "price": _num(row["price_aud"]),
            "odometer_km": _num(row["odometer_km"]),
            "first_price": _num(prices[0]) if prices else None,
            "first_day": days[0], "last_day": days[-1],
            "sightings": int(len(own)), "days": int(own["day"].nunique()),
            "change": _pct(prices[-1], prices[0]) if len(prices) > 1 else None,
            "live": live, "trim_last_day": last_day,
            "days_since": (datetime.now()
                           - pd.Timestamp(days[-1]).to_pydatetime()).days,
            "rank": rank if rank.get("usable") else None,
            "rank_reason": None if rank.get("usable") else rank.get("reason"),
            "trim_median": _num(pd.Series(peer_prices, dtype="float64").median()),
            "vs_median": _pct(_num(row["price_aud"]),
                              _num(pd.Series(peer_prices, dtype="float64").median())),
            "peers": len(peer_prices),
            "since": since,
        })

    moved = [c for c in cars if c.get("since", {}).get("move")]
    vanished = [c for c in cars
                if c.get("present") and not c.get("live")]
    return {"ok": True, "cars": cars, "total": len(cars),
            "gone": gone, "moved": len(moved), "not_in_latest": len(vanished),
            "gates": {"min_peers": CAR_MIN_PEERS},
            "basis": ("each car is compared against its OWN trim's capture "
                      "days, not the database's latest day - trims are swept "
                      "on different days and mixing them invents movement")}
