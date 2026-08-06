"""The one normalised listing schema, and the normalisers every adapter uses.

A `Record` is one *observation* of one vehicle listing on one capture date.
The same physical listing seen on three dates is three Records sharing a
dedupe key; the store keeps all three as price history.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path

FIELDS = [
    "source", "listing_id", "capture_date", "vehicle_type", "make", "model",
    "variant", "year", "body_type", "odometer_km", "price_aud", "price_note",
    "state", "seller_type", "raw_title", "url",
]

KNOWN_SOURCES = {"carsales", "bikesales", "facebook", "gumtree", "manual", "v2", "other"}

# Makes whose canonical casing isn't title-case.
_MAKE_CASING = {
    "bmw": "BMW", "ktm": "KTM", "mg": "MG", "gwm": "GWM", "ldv": "LDV",
    "hsv": "HSV", "fpv": "FPV", "vw": "Volkswagen", "volkswagen": "Volkswagen",
    "mercedes": "Mercedes-Benz", "mercedes-benz": "Mercedes-Benz",
    "mercedes benz": "Mercedes-Benz", "landrover": "Land Rover",
    "land rover": "Land Rover", "alfa": "Alfa Romeo", "alfa romeo": "Alfa Romeo",
    "holden": "Holden", "ford": "Ford", "toyota": "Toyota", "mazda": "Mazda",
    "hyundai": "Hyundai", "kia": "Kia", "nissan": "Nissan", "honda": "Honda",
    "yamaha": "Yamaha", "suzuki": "Suzuki", "kawasaki": "Kawasaki",
    "ducati": "Ducati", "triumph": "Triumph", "harley-davidson": "Harley-Davidson",
    "harley davidson": "Harley-Davidson", "royal enfield": "Royal Enfield",
    "cfmoto": "CFMoto", "cf moto": "CFMoto",
}

_BIKE_MAKES = {"yamaha", "kawasaki", "ducati", "ktm", "harley-davidson",
               "harley davidson", "royal enfield", "cfmoto", "cf moto",
               "aprilia", "husqvarna", "gasgas", "beta", "sherco"}

_STATES = {"nsw": "NSW", "vic": "VIC", "qld": "QLD", "sa": "SA", "wa": "WA",
           "tas": "TAS", "nt": "NT", "act": "ACT",
           "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
           "south australia": "SA", "western australia": "WA",
           "tasmania": "TAS", "northern territory": "NT",
           "australian capital territory": "ACT"}


@dataclass
class Record:
    source: str
    capture_date: str                 # YYYY-MM-DD
    make: str
    model: str
    listing_id: str | None = None
    vehicle_type: str = "car"         # car | bike
    variant: str | None = None
    year: int | None = None
    body_type: str | None = None      # sedan/ute/SUV... or dirt/road/naked...
    odometer_km: float | None = None
    price_aud: float | None = None
    price_note: str | None = None     # negotiable | poa | drive_away | ...
    state: str | None = None
    seller_type: str | None = None    # private | dealer
    raw_title: str | None = None
    url: str | None = None

    # Odometer granularity for the id-less fingerprint. Two failure modes
    # pull opposite ways: too coarse merges two different cars onto one key
    # (which corrupts a price series with another car's prices); too fine
    # gives the SAME car a new key when it is re-listed a few hundred km
    # later (loses the link, but never corrupts a price).
    #
    # Measured against the real data on 03/08/2026 (412 fingerprint-matched
    # observations, 41 keys spanning 2+ capture days):
    #     5,000 km -> 11 merged keys,  0 links broken   <- the old value
    #     1,000 km ->  3 merged keys,  0 links broken
    #       500 km ->  0 merged keys,  0 links broken   <- chosen
    #       exact  ->  0 merged keys,  1 link broken (a car driven 120 km
    #                  between captures: 161,760 -> 161,880)
    # 500 km is the finest value that still tolerates real between-capture
    # driving. See vpa/repair.py for migrating identities already stored.
    ODO_BUCKET_KM = 500

    def dedupe_key(self) -> str:
        """Stable identity of the physical listing across captures.

        Prefer the source's own listing id. Without one, fingerprint on the
        stable attributes (NOT price, which changes; odometer bucketed to
        ODO_BUCKET_KM so a re-list with a little more mileage still matches).
        """
        if self.listing_id:
            basis = f"id|{self.source}|{self.listing_id}"
        else:
            odo_bucket = ("" if self.odometer_km is None
                          else str(int(self.odometer_km // self.ODO_BUCKET_KM)))
            parts = [
                "fp", self.source, (self.make or "").casefold(),
                (self.model or "").casefold(), str(self.year or ""),
                (self.variant or "").casefold(), self.state or "", odo_bucket,
            ]
            # No odometer AND no id (e.g. Facebook Marketplace tiles): the
            # spec fields alone can't tell two same-model listings apart, so
            # fold in a $500 price bucket. Appended ONLY in this case so
            # every existing odometer-bearing fingerprint stays unchanged.
            if self.odometer_km is None and self.price_aud is not None:
                parts.append(f"p{int(self.price_aud // 500)}")
            basis = "|".join(parts)
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# normalisers
# --------------------------------------------------------------------------

_PRICE_WORDS_NONE = re.compile(
    r"negotiable|poa|p\.o\.a|price on application|contact|call|offers|swap|tba|free",
    re.I)
_PRICE_NUM = re.compile(r"(\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?)[ ]*([kK])?")


def parse_price(value) -> tuple[float | None, str | None]:
    """'$18,990' / '18990' / '18,990 Drive Away' / 'Negotiable' / '$12.5k'
    -> (price_aud, note). Unparseable or implausible -> (None, note)."""
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        p = float(value)
        return (p, None) if p >= 100 else (None, "implausible_low")
    s = str(value).strip()
    if not s:
        return None, None
    note = None
    low = s.casefold()
    if "drive away" in low or "driveaway" in low:
        note = "drive_away"
    elif "excl" in low and "gov" in low:
        note = "excl_gov_charges"
    m = _PRICE_NUM.search(s.replace("$", " "))
    if not m:
        if _PRICE_WORDS_NONE.search(low):
            return None, ("negotiable" if "negotia" in low else
                          "poa" if "po" in low or "application" in low else "no_price")
        return None, "unparseable"
    num = float(m.group(1).replace(",", "").replace(" ", ""))
    if m.group(2):  # '12.5k'
        num *= 1000
    if "negotia" in low or "ono" in re.findall(r"\b\w+\b", low):
        note = (note + ",negotiable") if note else "negotiable"
    if num < 100:
        return None, "implausible_low"
    if num > 2_000_000:
        return None, "implausible_high"
    return num, note


_ODO_NUM = re.compile(r"(\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?)[ ]*([kK]\b)?")


def parse_odometer(value) -> float | None:
    """'45,000 km' / '45000' / '45k' -> km. None if absent/unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if 0 <= v < 2_000_000 else None
    s = str(value).strip()
    if not s or s.casefold() in {"n/a", "na", "-", "unknown", ""}:
        return None
    m = _ODO_NUM.search(s)
    if not m:
        return None
    v = float(m.group(1).replace(",", "").replace(" ", ""))
    if m.group(2):
        v *= 1000
    return v if 0 <= v < 2_000_000 else None


def norm_make(s: str | None) -> str | None:
    if not s:
        return None
    key = re.sub(r"\s+", " ", s.strip()).casefold()
    return _MAKE_CASING.get(key, key.title())


def norm_model(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s.strip())
    # Model codes with digits keep manufacturer style: yz250f -> YZ250F,
    # cx-5 -> CX-5. Short letter codes stay caps: YZ -> YZ, GSX -> GSX.
    # Plain names title-case: falcon -> Falcon, 4 series -> 4 Series.
    def word(w: str) -> str:
        if any(ch.isdigit() for ch in w) and any(ch.isalpha() for ch in w):
            return w.upper()
        if w.isalpha() and len(w) <= 3 and (w.isupper() or len(w) <= 2):
            return w.upper()
        return w.title()
    return " ".join(word(w) for w in s.split(" "))


def norm_state(s: str | None) -> str | None:
    if not s:
        return None
    key = s.strip().casefold()
    if key in _STATES:
        return _STATES[key]
    # 'Penrith, NSW' or 'Penrith NSW 2750'
    for token in re.split(r"[,\s]+", key)[::-1]:
        if token in _STATES:
            return _STATES[token]
    return None


def norm_seller(s: str | None) -> str | None:
    if not s:
        return None
    low = s.casefold()
    if "dealer" in low or "demo" in low:
        return "dealer"
    if "private" in low:
        return "private"
    return None


def norm_year(value) -> int | None:
    if value is None:
        return None
    try:
        y = int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None
    return y if 1900 <= y <= 2035 else None


def year_from_title(title: str | None) -> int | None:
    if not title:
        return None
    m = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", title)
    return int(m.group(1)) if m else None


def guess_vehicle_type(make: str | None, explicit: str | None = None) -> str:
    if explicit:
        low = str(explicit).casefold()
        if low.startswith("bike") or "motorcycle" in low or "motorbike" in low:
            return "bike"
        if low.startswith("car"):
            return "car"
    if make and make.casefold() in _BIKE_MAKES:
        return "bike"
    return "car"


def norm_capture_date(value, fallback: date | None = None) -> str:
    """Anything date-ish -> 'YYYY-MM-DD'. Accepts DD/MM/YYYY (AU), ISO,
    datetime strings. Falls back to `fallback` (e.g. file mtime) or today."""
    if value is not None:
        s = str(value).strip()
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)   # AU DD/MM/YYYY
        if m:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)   # ISO
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return (fallback or date.today()).isoformat()


def file_mtime_date(path: str | Path) -> date:
    return datetime.fromtimestamp(Path(path).stat().st_mtime).date()


def make_record(raw: dict, source: str, capture_fallback: date | None = None) -> Record | None:
    """Normalise one raw field dict (adapter output) into a Record.
    Returns None when there's no usable make+model."""
    title = raw.get("raw_title") or raw.get("title")
    make = norm_make(raw.get("make"))
    model = norm_model(raw.get("model"))
    if not make or not model:
        return None
    price, pnote = parse_price(raw.get("price_aud", raw.get("price")))
    if raw.get("price_note") and not pnote:
        pnote = raw["price_note"]
    year = norm_year(raw.get("year")) or year_from_title(title)
    return Record(
        source=source,
        listing_id=(str(raw["listing_id"]).strip() or None) if raw.get("listing_id") else None,
        capture_date=norm_capture_date(raw.get("capture_date"), capture_fallback),
        vehicle_type=guess_vehicle_type(make, raw.get("vehicle_type")),
        make=make, model=model,
        variant=(str(raw["variant"]).strip() or None) if raw.get("variant") else None,
        year=year,
        body_type=(str(raw["body_type"]).strip().casefold() or None) if raw.get("body_type") else None,
        odometer_km=parse_odometer(raw.get("odometer_km", raw.get("odometer"))),
        price_aud=price, price_note=pnote,
        state=norm_state(raw.get("state") or raw.get("state_or_region") or raw.get("location")),
        seller_type=norm_seller(raw.get("seller_type")),
        raw_title=title, url=raw.get("url"),
    )
