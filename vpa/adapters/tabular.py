"""CSV / JSON exports -> raw field dicts, via column-synonym mapping.

Columns are matched case-insensitively against synonym lists, so a
spreadsheet with 'Price ($)', 'KMs' and 'Rego State' still maps cleanly.
Rows with a title but no make/model get make/model mined from the title.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .pastetext import split_title, _TITLE

_SYNONYMS: dict[str, list[str]] = {
    "make": ["make", "brand", "manufacturer"],
    "model": ["model"],
    "variant": ["variant", "badge", "trim", "series", "grade"],
    "year": ["year", "build year", "model year", "yr", "build"],
    "price": ["price aud", "price", "asking price", "advertised price", "cost",
              "amount", "price ($)", "price$", "listed price"],
    "odometer": ["odometer km", "odometer", "km", "kms", "mileage", "odo",
                 "kilometres", "kilometers"],
    "state": ["state or region", "state", "region", "location", "suburb",
              "rego state", "state/territory"],
    "seller_type": ["seller type", "seller", "listing type", "dealer/private"],
    "capture_date": ["capture date", "date captured", "snapshot date", "date",
                     "captured", "seen", "date seen"],
    "listing_id": ["listing id", "ad id", "id", "listing", "sku", "stock no",
                   "stock number"],
    "url": ["url", "link", "href"],
    "raw_title": ["raw title", "title", "heading", "name", "description"],
    "body_type": ["body or bike type", "body type", "body", "bike type",
                  "category", "type"],
    "vehicle_type": ["vehicle type", "kind", "vehicle"],
    "source": ["source", "site", "marketplace"],
    "price_note": ["price note", "note"],
}


def _canon(col: str) -> str:
    return re.sub(r"[\s_\-]+", " ", col.strip().casefold())


def map_columns(columns: list[str]) -> dict[str, str]:
    """actual column name -> schema field, first synonym match wins."""
    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    for field, syns in _SYNONYMS.items():
        for col in columns:
            if col in claimed:
                continue
            if _canon(col) in syns:
                mapping[col] = field
                claimed.add(col)
                break
    return mapping


def _finish_row(raw: dict) -> dict:
    """Backfill make/model/year from a title like '2014 Ford Falcon XR6'."""
    title = raw.get("raw_title")
    if title and (not raw.get("make") or not raw.get("model")):
        m = _TITLE.match(str(title).strip())
        if m and len(m.group(2).split()) >= 2:
            make, model, variant = split_title(m.group(1), m.group(2))
            raw.setdefault("year", m.group(1))
            if not raw.get("make"):
                raw["make"] = make
            if not raw.get("model"):
                raw["model"] = model
            if not raw.get("variant") and variant:
                raw["variant"] = variant
    return raw


def parse_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        mapping = map_columns(list(reader.fieldnames))
        out = []
        for row in reader:
            raw = {field: row[col] for col, field in mapping.items()
                   if row.get(col) not in (None, "")}
            if raw:
                out.append(_finish_row(raw))
        return out


def parse_json(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    if isinstance(data, dict):
        for k in ("listings", "records", "results", "data", "items"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    rows = [r for r in data if isinstance(r, dict)]
    if not rows:
        return []
    # union of keys across rows, mapped once
    all_keys: list[str] = sorted({k for r in rows for k in r})
    mapping = map_columns(all_keys)
    out = []
    for row in rows:
        raw = {field: row[col] for col, field in mapping.items()
               if row.get(col) not in (None, "")}
        if raw:
            out.append(_finish_row(raw))
    return out
