"""Searches and how much of each was saved - kept OUT of the database.

A results page usually states how many listings its search found ("1,234
cars for sale"), and many sites show only so many pages of a search. When
a search finds more than you can page through, a capture of it is SHORT:
the cars past the last page were never saved, and next week they would look
as if they had left the market. This module notices that, and helps plan
searches that fit.

  * `note()` records each imported results page: its search (capture day,
    page title, stated total) and the cars saved from it. Pages of one
    search share all three, so saving it a page at a time - or twice, sorted
    two ways - adds up to one search.
  * `searches()` / `coverage()`: for each search, stated against saved, and
    whether it was cut short.
  * `cut_keys()`: the cars of each day's short searches. The analysis frame
    carries them as the `cut` column (store._frame_from), and turnover never
    counts a car missing from a cut-short capture as gone.
  * `plan()`: price bands that each fit in one search, from your own prices.

Nothing here reaches a website; it only reads pages you saved and imported.
Kept in data/sweeps.json beside the database (never in vpa.db): it can be
deleted or rebuilt without touching a price, and losing it loses only these
notes. The stated total is read from the page's visible text; a site that
words it unusually can be given a `total_pattern` in the private site file
(vpa/sites.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime
from html import unescape
from pathlib import Path

FORMAT = "vpa-sweeps/1"
FILE_NAME = "sweeps.json"
#: a stated total the saved cars may fall short of by this much and still be
#: complete: listings come and go while you page through a search
SLACK_SHARE = 0.01
SLACK_MIN = 2
#: missing share above which a short capture is set aside by turnover. Below
#: it the possible error is a few cars against real exits in the dozens - on
#: the real data a Ute search 5 short of 159 would otherwise have thrown away
#: 114 good exits to guard against at most 5 false ones
CUT_SHARE = 0.05
#: the most a search is assumed to show when no short search has been seen
#: yet and the site file names none (`max_results`)
DEFAULT_CAPACITY = 400
#: bands are planned to this share of a search's capacity, leaving room for
#: the listings that arrive between planning and saving
FILL = 0.85

_SCRIPTS = re.compile(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1\s*>")
_BLOCK = re.compile(r"(?i)</?(p|div|li|tr|br|h[1-6]|section|article|header|footer|td|ul|ol|nav|main)\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
#: "1,234 Ford Falcon cars for sale", "312 results", "48 listings found":
#: a count, up to a dozen words (no prices), then a results phrase - on one line
_TOTAL = re.compile(
    r"(?<![\d$.,])(\d{1,3}(?:,\d{3})+|\d{1,6})\s+"
    r"(?:[A-Za-z0-9][\w'&./+-]*\s+){0,12}?"
    r"(?:for\s+sale|results?\b|listings?\s+found|matches\b|ads?\s+found|vehicles?\s+found)",
    re.I)


# ------------------------------------------------------------ reading a page
def visible_text(html: str) -> str:
    t = _SCRIPTS.sub(" ", html or "")
    t = _BLOCK.sub("\n", t)
    t = unescape(_TAG.sub(" ", t))
    return "\n".join(re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in t.split("\n") if ln.strip())


def stated_total(html: str, source: str | None = None) -> int | None:
    """How many listings the page says its search found, or None."""
    from . import sites
    text = visible_text(html)
    pat = None
    for s in sites.sites():
        if source and str(s.get("source")) == str(source) and s.get("total_pattern"):
            try:
                pat = re.compile(str(s["total_pattern"]), re.I)
            except re.error:
                pat = None
    m = (pat or _TOTAL).search(text)
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None
    return n if n > 0 else None


def page_title(html: str) -> str:
    m = _TITLE.search(html or "")
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:200] if m else ""


# ------------------------------------------------------------------ storage
def store_path(db_path) -> Path:
    """data/sweeps.json for data/vpa.db: beside the database it belongs to."""
    return Path(db_path).resolve().parent / FILE_NAME


def _read(path: Path, strict: bool = False) -> dict:
    from .phonecfg import read_text_retry
    try:
        d = json.loads(read_text_retry(path))
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    except ValueError:
        if strict:
            raise
        return {}
    s = d.get("searches") if isinstance(d, dict) else None
    return s if isinstance(s, dict) else {}


_CACHE: dict[str, tuple] = {}


def _load(db_path) -> dict:
    path = store_path(db_path)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    got = _read(path)
    _CACHE[str(path)] = (sig, got)
    return got


def _sid(day: str, title: str, total: int) -> str:
    return hashlib.sha1(f"{day}|{title.casefold()}|{total}".encode()).hexdigest()[:12]


def note(db_path, html: str, records, source: str | None = None) -> dict | None:
    """Record one imported results page. Returns its search's state, or None
    when the page states no total (nothing can be judged). Never raises into
    an import: the caller wraps it, and a failure costs only these notes."""
    records = [r for r in (records or []) if r is not None]
    if not records:
        return None
    total = stated_total(html, source or getattr(records[0], "source", None))
    if total is None or total < len(records):
        return None                  # no total, or not this page's search
    return note_page(db_path, page_title(html), total, records)


def note_page(db_path, title: str, total: int, records) -> dict:
    """note(), for a page whose title and stated total are already read."""
    day = max(str(r.capture_date)[:10] for r in records)
    keys = sorted({r.dedupe_key() for r in records})
    sig = hashlib.sha1("|".join(keys).encode()).hexdigest()[:12]
    sid = _sid(day, title, total)
    path = store_path(db_path)
    from .photos import _locked
    from .phonecfg import _replace
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with _locked(path):
        allx = _read(path, strict=True)
        s = allx.setdefault(sid, {"day": day, "title": title, "total": total,
                                  "pages": {}, "keys": [], "first": now})
        s["pages"][sig] = len(keys)
        s["keys"] = sorted(set(s["keys"]) | set(keys))
        s["last"] = now
        body = json.dumps({"format": FORMAT, "searches": allx}, separators=(",", ":"))
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        _replace(tmp, path)
    _CACHE.pop(str(path), None)
    return _state(sid, s)


def backfill(db_path) -> dict:
    """Note every kept results page (data/raw and the watch folder's
    archive) - pages imported before this was kept. Reads pages, writes
    only sweeps.json; the database is not opened for writing."""
    from .adapters import parse_file
    from .photos import kept_pages
    pages = kept_pages(db_path)
    noted = 0
    for p in pages:
        try:
            recs, fmt, _ = parse_file(p)
            if fmt == "html" and recs and note(db_path, p.read_text(encoding="utf-8", errors="replace"), recs):
                noted += 1
        except Exception:
            continue
    return {"pages": len(pages), "noted": noted, "searches": len(searches(db_path))}


def note_html(db_path, html: str, records, source=None) -> dict | None:
    try:
        return note(db_path, html, records, source)
    except Exception:
        return None


# ----------------------------------------------------------------- reading
def _short(total: int, saved: int) -> bool:
    return saved < total - max(SLACK_MIN, math.ceil(total * SLACK_SHARE))


def _state(sid: str, s: dict) -> dict:
    saved = len(s.get("keys") or [])
    total = int(s.get("total") or 0)
    return {"id": sid, "day": s.get("day"), "title": s.get("title") or "",
            "total": total, "saved": saved, "pages": len(s.get("pages") or {}),
            "per_page": max((s.get("pages") or {}).values(), default=0),
            "short": _short(total, saved), "missing": max(0, total - saved),
            "set_aside": _material(total, saved),
            "share": round(saved / total * 100, 1) if total else None}


def searches(db_path) -> list[dict]:
    """Every recorded search, newest day first."""
    out = [_state(k, v) for k, v in _load(db_path).items() if isinstance(v, dict)]
    out.sort(key=lambda s: (s["day"] or "", s["title"]), reverse=True)
    return out


def _material(total: int, saved: int) -> bool:
    return _short(total, saved) and (total - saved) > total * CUT_SHARE


def cut_keys(db_path) -> dict[str, set]:
    """day -> the cars saved from that day's searches that missed more than
    CUT_SHARE of what they stated (turnover sets those captures aside)."""
    out: dict[str, set] = {}
    for k, v in _load(db_path).items():
        if isinstance(v, dict) and _material(int(v.get("total") or 0), len(v.get("keys") or [])):
            out.setdefault(str(v.get("day")), set()).update(v.get("keys") or [])
    return out


def capacity(db_path) -> dict:
    """How many listings one search shows. Your site file's `max_results`
    when it names one - you know the site's page limit. Otherwise the most
    any cut-short search delivered, which is only a hint: a search is also
    short when you stopped saving early. Otherwise an assumption. The
    answer says which."""
    from . import sites
    mx = sites.config().get("max_results")
    if isinstance(mx, int) and mx > 0:
        return {"cars": mx, "basis": "site file",
                "note": f"{mx:,} cars, from your site file (max_results)"}
    shorts = [s for s in searches(db_path) if s["short"] and s["pages"] >= 2]
    if shorts:
        best = max(shorts, key=lambda s: s["saved"])
        return {"cars": best["saved"], "basis": "seen",
                "note": f"about {best['saved']:,} cars - the most a cut-short "
                        f"search has given you ({best['day']}). Set max_results in your "
                        f"site file if you know the real limit"}
    return {"cars": DEFAULT_CAPACITY, "basis": "assumed",
            "note": f"an assumed {DEFAULT_CAPACITY} cars - set max_results in "
                    f"your site file if you know the real limit"}


def coverage(db_path, days: int = 8) -> dict:
    """The latest capture days' searches, each stated against saved."""
    allx = searches(db_path)
    order = sorted({s["day"] for s in allx if s["day"]}, reverse=True)[:days]
    out = []
    for d in order:
        ss = [s for s in allx if s["day"] == d]
        out.append({"day": d, "searches": ss, "stated": sum(s["total"] for s in ss),
                    "saved": sum(s["saved"] for s in ss),
                    "short": sum(1 for s in ss if s["short"])})
    return {"ok": True, "days": out, "capacity": capacity(db_path),
            "recorded": len(allx)}


# ------------------------------------------------------------------ planning
def _nice(x: float) -> int:
    step = 500 if x < 20000 else 1000 if x < 60000 else 5000
    return int(round(x / step) * step)


def plan(db_path, make: str, model: str | None = None, variant: str | None = None,
         total: int | None = None) -> dict:
    """Price bands that each fit in one search, for one selection.

    The SIZE is the stated total of the latest search made mostly of this
    selection's cars (or `total`, or failing both the cars seen in the
    latest month). The SHAPE is this selection's own price spread: each
    car's latest price, over the last 60 days of captures - every car, not
    only the ones a cut-short sweep reached, which were all from one end."""
    import pandas as pd
    from .explore import _frame, _select
    df = _select(_frame(db_path), make, model, variant)
    label = " ".join(x for x in (make, model, variant) if x)
    cap = capacity(db_path)
    out = {"ok": True, "label": label, "capacity": cap, "bands": [], "size": None,
           "size_basis": "", "fits": False, "tips": []}
    if df.empty:
        return dict(out, ok=False, error=f"no cars of {label} in your data yet")
    keys = set(df["key"].astype(str))
    if total is None:
        best = None
        for s in searches(db_path):
            raw = _load(db_path).get(s["id"]) or {}
            ks = set(raw.get("keys") or [])
            if ks and len(ks & keys) >= 0.8 * len(ks):
                best = s
                break                                     # newest first
        if best:
            total = best["total"]
            out["size_basis"] = f"the search saved {best['day']} stated {total:,}"
    if total is None:
        last = df["capture_date"].max()
        recent = df[df["capture_date"] >= last - pd.Timedelta(days=30)]
        total = int(recent["key"].nunique())
        out["size_basis"] = f"{total:,} cars seen in your captures over the latest month"
    elif not out["size_basis"]:
        out["size_basis"] = f"{total:,}, as entered"
    out["size"] = int(total)
    room = max(1, int(cap["cars"] * FILL))
    if total <= cap["cars"]:
        out["fits"] = True
        out["tips"].append("It fits in one search: save every page of it.")
        return out
    last = df["capture_date"].max()
    rec = df[df["capture_date"] >= last - pd.Timedelta(days=60)]
    latest = (rec.dropna(subset=["price_aud"]).sort_values("capture_date", kind="stable")
              .groupby("key").tail(1)["price_aud"].astype(float).sort_values().to_numpy())
    if len(latest) < 10:
        return dict(out, ok=False, error="too few priced cars in the latest 60 days to plan bands")
    k = max(2, math.ceil(total / room))
    cuts = []
    for i in range(1, k):
        q = latest[min(len(latest) - 1, int(round(i / k * len(latest))))]
        c = _nice(q)
        if not cuts or c > cuts[-1]:
            cuts.append(c)
    edges = [None] + cuts + [None]
    scale = total / len(latest)
    bands = []
    for lo, hi in zip(edges, edges[1:]):
        n = sum(1 for p in latest if (lo is None or p >= lo) and (hi is None or p < hi))
        est = int(round(n * scale))
        bands.append({"from": lo, "to": hi, "estimate": est, "fits": est <= cap["cars"]})
    out["bands"] = bands
    if total <= 2 * cap["cars"]:
        out["tips"].append("Or save it twice, sorted by price low to high and high to low: "
                           "the overlap is recognised and counted once.")
    out["tips"].append("Bands follow your own prices; the site's count for each band "
                       "tells you straight away whether it fits.")
    return out
