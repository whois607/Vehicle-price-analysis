"""Listing details: what your saved pages state about each car beyond the
price - kept OUT of the database.

A results page states, for every listing, more than the app stores in
vpa.db: whether the car is used, a demo or new; its engine, fuel and
gearbox; whether the price is drive-away or excludes government charges;
the site's own price rating; and a few flags ("just listed", a listing the
site added that was not part of your search). A details page adds drive,
doors, seats, engine size and colour. This module keeps those, keyed by the
site's ad id, in data/details.json beside the database:

* the price data is never touched - details can be added, rebuilt or
  deleted without a write to vpa.db;
* the ad id survives everything the database does to its own keys (merges,
  repairs), the same as the photo list (photos.py);
* losing the file loses only these extras. `python -m vpa details
  --backfill` rebuilds what the kept pages hold.

The analysis frame reads them on load as extra columns (store._frame_from):
the stored columns are never changed, except that a blank stored state or
seller type is filled from what the page said - see `frame_cols`.

Each value is tied to its car only when that is PROVED, never by position:

* a listing tile is the smallest part of the page's own data that holds
  the tile's key details and names exactly ONE ad; a part naming two ads
  is skipped;
* an analytics object is read only when it names one ad, and a field whose
  copies disagree is left out rather than chosen between.

Nothing here reaches a website; it only reads text you already saved.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from html import unescape
from pathlib import Path

FORMAT = "vpa-details/1"
FILE_NAME = "details.json"

_AD = re.compile(r"\b([A-Z]{2,5}-AD-\d+)\b")
_JSON_SCRIPT = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_STATES = {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"}

# ------------------------------------------------------------ the fields
#: per car - the newest page that states one wins
CAR_FIELDS = ("condition", "seller", "state", "engine", "cylinders", "litres",
              "induction", "fuel", "gearbox", "drive", "doors", "seats", "cc",
              "colour", "seller_id")
#: per sighting (car and capture day) - these change between captures
DAY_FIELDS = ("price_type", "rating", "just_listed", "suggested", "promoted")

#: condition, most specific first
CONDITIONS = ("used", "demo", "new", "new to order")
RATINGS = ("great", "good", "fair")
PRICE_TYPES = ("excl. charges", "drive away")


# ------------------------------------------------------------ normalisers
def _condition(*words) -> str | None:
    """'Dealer demo', 'New car to order', 'Used', 'Private'... -> one of
    CONDITIONS. The first word that says something wins."""
    for w in words:
        low = str(w or "").casefold()
        if not low:
            continue
        if "to order" in low:
            return "new to order"
        if "demo" in low:
            return "demo"
        if "new" in low or "showroom" in low:
            return "new"
        if "used" in low or "private" in low:
            return "used"
    return None


def _seller(word) -> str | None:
    low = str(word or "").casefold()
    if "private" in low:
        return "private"
    if "dealer" in low or "new car" in low:
        return "dealer"
    return None


def _price_type(text) -> str | None:
    low = re.sub(r"\s+", " ", str(text or "")).casefold()
    if "drive away" in low or "driveaway" in low:
        return "drive away"
    if "excl" in low and ("gov" in low or "charge" in low):
        return "excl. charges"
    return None


def _rating(text) -> str | None:
    """The site's price badge: 'GREAT PRICE' or its other wording, 'Well
    below market price' (the same badge, same icon) -> great/good/fair."""
    low = str(text or "").casefold()
    if "great" in low or "well below" in low:
        return "great"
    if "good" in low or "below market" in low:
        return "good"
    if "fair" in low or "around market" in low or "average" in low:
        return "fair"
    return None


_ENGINE = re.compile(
    r"^\s*(?:(\d{1,2})\s*cyl)?\s*(?:(\d{1,3}(?:\.\d)?)\s*L)?\s*([TS])?\s*"
    r"(Petrol|Diesel|Gas Only|LPG|Hybrid|Electric|Petrol/LPG|Plug-in Hybrid)?\s*$", re.I)


def parse_engine(text) -> dict:
    """'6cyl 4.0L T Petrol' -> cylinders 6, litres 4.0, induction turbo,
    fuel petrol. A tile writes the engine this way; anything else gives
    only `engine` (the text as shown). A classic's size is written in
    cubic inches with the same 'L' ('8cyl 289L Petrol' is a 289 V8, not
    289 litres): three digits are read as cubic inches and turned into
    litres (4.7)."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    out: dict = {}
    if not t:
        return out
    out["engine"] = t[:40]
    m = _ENGINE.match(t)
    if not m or not any(m.groups()):
        return out
    cyl, lit, ind, fuel = m.groups()
    if cyl:
        out["cylinders"] = int(cyl)
    if lit:
        v = float(lit)
        if v >= 60:                       # cubic inches
            v = round(v * 0.0163871, 1)
        if 0 < v < 15:
            out["litres"] = v
    out["induction"] = {"T": "turbo", "S": "supercharged"}.get((ind or "").upper(),
                                                              "natural" if (cyl or lit) else None)
    if out["induction"] is None:
        out.pop("induction")
    if fuel:
        out["fuel"] = _fuel(fuel)
    return out


def _fuel(text) -> str | None:
    low = str(text or "").casefold()
    if not low:
        return None
    if "hybrid" in low:
        return "hybrid"
    if "electric" in low:
        return "electric"
    if "diesel" in low:
        return "diesel"
    if "gas" in low or "lpg" in low:
        return "lpg"
    if "petrol" in low or "ulp" in low:
        return "petrol"
    return None


def _gearbox(text) -> str | None:
    low = str(text or "").casefold()
    if "manual" in low:
        return "manual"
    if "auto" in low or "cvt" in low or "dct" in low:
        return "auto"
    return None


def _int(v) -> int | None:
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# ------------------------------------------------------------ results tiles
def _tiles(node, out: list) -> tuple[set, int]:
    """Post-order walk of a page's data: collect each listing tile - the
    smallest object holding exactly one `keyDetailsTexts` and at least one
    ad id - with the ads it names. Returns (ads, key-detail lists) below."""
    if isinstance(node, dict):
        ads, kd, kids = set(), 0, []
        for k, v in node.items():
            a, n = _tiles(v, out)
            kids.append((a, n))
            ads |= a
            kd += n
            if k == "keyDetailsTexts":
                kd += 1
        if kd == 1 and ads and not any(n == 1 and a for a, n in kids):
            out.append((node, ads))
        return ads, kd
    if isinstance(node, list):
        ads, kd = set(), 0
        for v in node:
            a, n = _tiles(v, out)
            ads |= a
            kd += n
        return ads, kd
    if isinstance(node, str):
        return set(m.upper() for m in _AD.findall(node)), 0
    return set(), 0


def _find(node, key: str, acc: list) -> list:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                acc.append(v)
            _find(v, key, acc)
    elif isinstance(node, list):
        for v in node:
            _find(v, key, acc)
    return acc


def _texts(node, acc: list) -> list:
    """Every shown text below a node: `value`, `label` and `title` strings."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("value", "label", "title") and isinstance(v, str) and v.strip():
                acc.append(v.strip())
            else:
                _texts(v, acc)
    elif isinstance(node, list):
        for v in node:
            _texts(v, acc)
    return acc


def _icons(node, acc: dict) -> dict:
    """Icon name -> the text it labels, e.g. 'transmission' -> 'Automatic'."""
    if isinstance(node, dict):
        if node.get("type") == "Icon" and isinstance(node.get("source"), str) \
                and isinstance(node.get("title"), str) and node["title"].strip():
            acc.setdefault(node["source"], node["title"].strip())
        for v in node.values():
            _icons(v, acc)
    elif isinstance(node, list):
        for v in node:
            _icons(v, acc)
    return acc


def _from_tile(node: dict) -> tuple[dict, dict]:
    """(car fields, sighting fields) from one listing tile."""
    car: dict = {}
    day: dict = {}
    kd = node.get("keyDetailsTexts")
    kd = [str(x).strip() for x in kd if isinstance(x, str)] if isinstance(kd, list) else []
    icons = _icons(node.get("keyDetails"), {})
    eng = icons.get("engine") or next((t for t in kd if re.search(r"\d\s*cyl|\d\.\dL", t, re.I)), None)
    if eng:
        car.update(parse_engine(eng))
    gear = icons.get("transmission") or next((t for t in kd if _gearbox(t) and len(t) <= 20), None)
    if gear and _gearbox(gear):
        car["gearbox"] = _gearbox(gear)
    # "Private • VIC", "Dealer used • NSW", "Dealer new • <dealer> • WA",
    # "New car to order" - the kind of seller and the state; a dealer's own
    # name is not kept
    for t in _texts(node.get("sellerTitle"), []):
        parts = [p.strip() for p in t.split("•")]
        cond = _condition(parts[0])
        if cond or _seller(parts[0]):
            if cond:
                car.setdefault("condition", cond)
            if _seller(parts[0]) or cond == "new to order":
                car.setdefault("seller", _seller(parts[0]) or "dealer")
            st = parts[-1].upper() if len(parts) > 1 else ""
            if st in _STATES:
                car.setdefault("state", st)
    car = {k: v for k, v in car.items() if v is not None}
    pt = _price_type(" ".join(_texts(node.get("priceInfo"), [])))
    if pt:
        day["price_type"] = pt
    for badge in _find(node, "priceBadge", []):
        r = next((x for x in map(_rating, _texts(badge, [])) if x), None)
        if r:
            day["rating"] = r
            break
    if any(t.casefold() == "just listed" for t in _texts(node.get("galleryBadge"), [])):
        day["just_listed"] = True
    return car, day


# ------------------------------------------------------------ analytics
_NET = re.compile(r'"networkId"\s*:\s*"([A-Za-z]{2,5}-AD-\d+)"')
_KV = re.compile(r'"(adtype|adType|type|sellerId|result_type|resultType|suggestedresult|'
                 r'sortby|listingresultcount)"\s*:\s*("[^"]{0,60}"|true|false|\d{1,7})')


def _analytics(html: str) -> tuple[dict, dict]:
    """ad id -> its analytics fields, and the page's own {sort, total}.

    Read only from flat objects that name exactly one ad (bounded by the
    object's own braces), and only fields every copy agrees on."""
    seen: dict[str, dict[str, set]] = {}
    for m in _NET.finditer(html):
        a = html.rfind("{", 0, m.start())
        b = html.find("}", m.end())
        if a < 0 or b < 0 or b - a > 4000:
            continue
        obj = html[a:b + 1]
        if len({x.upper() for x in _NET.findall(obj)}) != 1:
            continue
        got = seen.setdefault(m.group(1).upper(), {})
        for k, v in _KV.findall(obj):
            k = {"adType": "adtype", "resultType": "result_type"}.get(k, k)
            got.setdefault(k, set()).add(v.strip('"'))
    per_ad: dict[str, dict] = {}
    sorts, totals = set(), set()
    for ad, got in seen.items():
        one = {k: next(iter(v)) for k, v in got.items() if len(v) == 1}
        per_ad[ad] = one
        if "sortby" in one:
            sorts.add(one["sortby"])
        if "listingresultcount" in one:
            totals.add(one["listingresultcount"])
    page = {}
    if len(sorts) == 1:
        page["sort"] = next(iter(sorts))[:60]
    if len(totals) == 1 and (n := _int(next(iter(totals)))):
        page["total"] = n
    return per_ad, page


def _from_analytics(a: dict) -> tuple[dict, dict]:
    car: dict = {}
    day: dict = {}
    cond = _condition(a.get("adtype"), a.get("type"))
    if cond:
        car["condition"] = cond
    if a.get("sellerId") and re.fullmatch(r"[\w-]{1,40}", a["sellerId"]):
        car["seller_id"] = a["sellerId"]
    rt = str(a.get("result_type") or "").casefold()
    if rt == "suggested" or str(a.get("suggestedresult")).casefold() == "true":
        day["suggested"] = True
    if rt == "promoted":
        day["promoted"] = True
    return car, day


# ------------------------------------------------------------ details pages
def _from_detail_page(html: str) -> tuple[str | None, dict]:
    from .adapters import detailpage
    d = detailpage.parse_html(html) or {}
    ad = (d.get("listing_id") or "").upper()
    if not _AD.fullmatch(ad):
        return None, {}
    car: dict = {}
    for blob in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            data = json.loads(unescape(blob))
        except ValueError:
            continue
        for n in (data if isinstance(data, list) else [data]):
            if not isinstance(n, dict) or not n.get("vehicleTransmission") and not n.get("fuelType"):
                continue
            if g := _gearbox(n.get("vehicleTransmission")):
                car.setdefault("gearbox", g)
            if f := _fuel(n.get("fuelType")):
                car.setdefault("fuel", f)
            if dr := str(n.get("driveWheelConfiguration") or "").strip().upper():
                if re.fullmatch(r"[A-Z0-9]{2,4}", dr):
                    car.setdefault("drive", dr)
            for k, src in (("doors", "numberOfDoors"), ("seats", "seatingCapacity")):
                if (v := _int(n.get(src))) and v < 20:
                    car.setdefault(k, v)
            eng = n.get("vehicleEngine") or {}
            disp = eng.get("engineDisplacement") if isinstance(eng, dict) else None
            if isinstance(disp, dict) and str(disp.get("unitCode", "")).casefold() in ("cc", "cmq"):
                if (cc := _int(disp.get("value"))) and cc < 20000:
                    car.setdefault("cc", cc)
                    car.setdefault("litres", round(cc / 1000, 1))
            cond = str(n.get("itemCondition") or "")
            if "Used" in cond:
                car.setdefault("condition", "used")
            elif "New" in cond:
                car.setdefault("condition", "new")
    if d.get("colour"):
        car["colour"] = str(d["colour"])[:40]
    if s := _seller(d.get("seller_type")):
        car["seller"] = s
    if st := str(d.get("state") or "").upper():
        if st in _STATES:
            car["state"] = st
    return ad, car


# ------------------------------------------------------------ one page
def extract(html: str) -> dict:
    """Everything one saved page states per car. Read-only.

    -> {"ads": {ad id: {"car": {...}, "day": {...}}}, "page": {sort, total}}
    """
    from .adapters import detailpage
    html = html or ""
    ads: dict[str, dict] = {}
    if detailpage.is_detail_page(html):
        ad, car = _from_detail_page(html)
        if ad and car:
            ads[ad] = {"car": car, "day": {}}
        return {"ads": ads, "page": {}}
    per_ad, page = _analytics(html)
    for ad, a in per_ad.items():
        car, day = _from_analytics(a)
        if car or day:
            ads[ad] = {"car": car, "day": day}
    for m in _JSON_SCRIPT.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        tiles: list = []
        _tiles(data, tiles)
        for node, found in tiles:
            if len(found) != 1:
                continue                     # two ads in one part: unprovable
            ad = next(iter(found))
            car, day = _from_tile(node)
            cur = ads.setdefault(ad, {"car": {}, "day": {}})
            # the tile is what the page SHOWS: it wins over analytics
            cur["car"].update(car)
            cur["day"].update(day)
    return {"ads": {k: v for k, v in ads.items() if v["car"] or v["day"]}, "page": page}


# ------------------------------------------------------------------ storage
def store_path(db_path) -> Path:
    """data/details.json for data/vpa.db: beside the database it belongs to."""
    return Path(db_path).resolve().parent / FILE_NAME


def _clean_car(d) -> dict:
    out = {}
    for k in CAR_FIELDS:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, bool) or v is None or v == "":
            continue
        if isinstance(v, (int, float)) or (isinstance(v, str) and len(v) <= 60):
            out[k] = v
    return out


def _clean_day(d) -> dict:
    out = {}
    for k in DAY_FIELDS:
        v = d.get(k) if isinstance(d, dict) else None
        if v is True or (isinstance(v, str) and 0 < len(v) <= 30):
            out[k] = v
    return out


def _read(path: Path, strict: bool = False) -> dict:
    """The stored ads. Missing reads as empty; `strict` raises when the
    file is there but could not be read, so a save never rewrites it from
    nothing."""
    from .phonecfg import read_text_retry
    try:
        d = json.loads(read_text_retry(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    ads = d.get("ads") if isinstance(d, dict) else None
    out = {}
    for k, v in (ads or {}).items():
        if not isinstance(v, dict) or not _AD.fullmatch(str(k).upper()):
            continue
        days = {str(dd)[:10]: _clean_day(x) for dd, x in (v.get("s") or {}).items()
                if re.fullmatch(r"\d{4}-\d\d-\d\d", str(dd)[:10])}
        out[str(k).upper()] = {"d": str(v.get("d") or ""), "f": _clean_car(v.get("f")),
                               "s": {dd: x for dd, x in days.items() if x}}
    return out


_CACHE: dict[str, tuple] = {}


def load(db_path) -> dict:
    """ad id -> {"d": newest day, "f": car fields, "s": {day: fields}}.
    Cached until the file changes."""
    path = store_path(db_path)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        got = _read(path, strict=True)
    except (OSError, ValueError):
        return hit[1] if hit else {}
    _CACHE[str(path)] = (sig, got)
    return got


def stat(db_path):
    """(mtime, size) of the details file, or None - for change detection."""
    try:
        st = store_path(db_path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def remember(db_path, found: dict, day=None) -> int:
    """Merge one page's details in. Returns how many ads changed.

    Car fields: a page at least as new as the stored one overwrites the
    fields it states and keeps the ones it does not; an older page only
    fills blanks. Sighting fields are kept per capture day. Written
    atomically under a lock, with the previous version kept as
    details.json.bak. A failure here costs only these extras."""
    ads_in = (found or {}).get("ads") or {}
    if not ads_in:
        return 0
    day = str(day or date.today().isoformat())[:10]
    path = store_path(db_path)
    from .photos import _locked
    from .phonecfg import _replace
    with _locked(path):
        ads = _read(path, strict=True)
        changed = 0
        for ad, got in ads_in.items():
            ad = str(ad).upper()
            if not _AD.fullmatch(ad):
                continue
            car, sight = _clean_car(got.get("car")), _clean_day(got.get("day"))
            if not car and not sight:
                continue
            cur = ads.get(ad) or {"d": "", "f": {}, "s": {}}
            new = {"d": cur["d"], "f": dict(cur["f"]), "s": dict(cur["s"])}
            if day >= cur["d"]:
                new["f"].update(car)
                new["d"] = day
            else:
                for k, v in car.items():
                    new["f"].setdefault(k, v)
            if sight:
                new["s"][day] = {**new["s"].get(day, {}), **sight}
            if new != cur:
                ads[ad] = new
                changed += 1
        if changed:
            body = json.dumps({"format": FORMAT, "ads": dict(sorted(ads.items()))},
                              separators=(",", ":"))
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(body, encoding="utf-8")
            if path.exists():
                try:
                    path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
                except OSError:
                    pass
            _replace(tmp, path)
    _CACHE.pop(str(path), None)
    return changed


def remember_html(db_path, html: str, day=None) -> int:
    """extract + remember, for the import paths. Never raises."""
    try:
        return remember(db_path, extract(html), day)
    except Exception:
        return 0


def backfill(db_path, pages=None) -> dict:
    """Read every kept page (data/raw, the watch folder's archive) and
    remember its details. Writes only details.json."""
    from .photos import kept_pages
    from .schema import file_mtime_date
    from .adapters import parse_file
    pages = kept_pages(db_path) if pages is None else [Path(p) for p in pages]
    changed = ads = 0
    for p in pages:
        try:
            html = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = extract(html)
        ads += len(found["ads"])
        if not found["ads"]:
            continue
        # the capture day the import gave the page's cars, as photos do;
        # the file's own date when the page parses to nothing
        try:
            recs, _, _ = parse_file(p)
            day = max((str(r.capture_date) for r in recs), default=None)
        except Exception:
            day = None
        changed += remember(db_path, found, day or file_mtime_date(p).isoformat())
    return {"pages": len(pages), "ads_in_pages": ads, "changed": changed, **coverage(db_path)}


def coverage(db_path) -> dict:
    """How many stored cars have details. Read-only on the database."""
    from .photos import ad_id
    from .store import connect_readonly
    have = load(db_path)
    try:
        conn = connect_readonly(db_path)
    except Exception:
        return {"cars": 0, "with_details": 0, "stored_ads": len(have)}
    try:
        rows = conn.execute("SELECT listing_id, url FROM vehicles").fetchall()
    finally:
        conn.close()
    n = sum(1 for lid, url in rows if (a := ad_id(lid, url)) and a in have)
    return {"cars": len(rows), "with_details": n, "stored_ads": len(have)}


# ------------------------------------------------------------ the frame
#: the frame columns this adds, in order (store._frame_from)
FRAME_COLS = ("condition", "engine", "cylinders", "litres", "induction", "fuel",
              "gearbox_listed", "drive_listed", "colour", "seller_id",
              "price_type", "rating", "just_listed", "suggested", "promoted")


def frame_cols(df, db_path) -> None:
    """Add the details to an analysis frame, in place.

    New columns (FRAME_COLS) are blank where the pages said nothing. Of the
    STORED columns only blanks are touched: a car with no stored state or
    seller type takes the one its page stated, and `gearbox` and `drive`
    (worked out from the trim name, trims.py) take what the listing stated
    where the trim name said nothing. A stored or trim-read value always
    wins."""
    import pandas as pd
    have = load(db_path) if db_path else {}
    n = len(df)
    if not n:
        for c in FRAME_COLS:
            df[c] = pd.Series([], dtype=object)
        return
    from .photos import _AD as _PAD
    cols: dict[str, list] = {c: [None] * n for c in FRAME_COLS}
    fill_state, fill_seller = [None] * n, [None] * n
    if have:
        lids = df["listing_id"].tolist()
        urls = df["url"].tolist()
        days = df["capture_date"].dt.strftime("%Y-%m-%d").tolist()
        cache: dict = {}
        for i in range(n):
            lid, url = lids[i], urls[i]
            k = (lid, url)
            if k not in cache:
                ad = None
                for v in (lid, url):
                    if isinstance(v, str) and (m := _PAD.search(v)):
                        ad = m.group(1).upper()
                        break
                cache[k] = have.get(ad) if ad else None
            got = cache[k]
            if not got:
                continue
            f = got["f"]
            for c in ("condition", "engine", "cylinders", "litres", "induction",
                      "fuel", "colour", "seller_id"):
                cols[c][i] = f.get(c)
            cols["gearbox_listed"][i] = f.get("gearbox")
            cols["drive_listed"][i] = f["drive"].lower() if f.get("drive") else None
            fill_state[i], fill_seller[i] = f.get("state"), f.get("seller")
            s = got["s"].get(days[i])
            if s:
                cols["price_type"][i] = s.get("price_type")
                cols["rating"][i] = s.get("rating")
                for c in ("just_listed", "suggested", "promoted"):
                    cols[c][i] = True if s.get(c) else None
    for c, v in cols.items():
        df[c] = pd.Series(v, index=df.index, dtype=object)
    for c in ("just_listed", "suggested", "promoted"):
        df[c] = df[c].fillna(False).astype(bool)
    df["cylinders"] = pd.to_numeric(df["cylinders"], errors="coerce")
    df["litres"] = pd.to_numeric(df["litres"], errors="coerce")
    if have:
        fs = pd.Series(fill_state, index=df.index, dtype=object)
        df["state"] = df["state"].where(df["state"].notna(), fs)
        fl = pd.Series(fill_seller, index=df.index, dtype=object)
        df["seller_type"] = df["seller_type"].where(df["seller_type"].notna(), fl)
        for col, listed in (("gearbox", "gearbox_listed"), ("drive", "drive_listed")):
            if col in df.columns:
                df[col] = df[col].where(df[col].notna(), df[listed])
