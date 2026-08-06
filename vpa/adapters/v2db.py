"""Import history from the vehicle-price-tracker-v2 SQLite database.

v2's model: watches (a tracked category) -> listings (ad identity) ->
observations (dated price/odometer sightings). Each v2 observation becomes
one Record here; make/model/variant/vehicle_type come from the listing when
present, else from its watch. v2's source_listing_id keeps identity stable,
so re-importing is idempotent.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..schema import Record, make_record

DEFAULT_V2_DB = (Path(__file__).resolve().parents[2].parent
                 / "vehicle-price-tracker-v2" / "data" / "tracker.db")


def load(db_path: str | Path = DEFAULT_V2_DB) -> list[Record]:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"v2 database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT l.source, l.source_listing_id, l.url, l.title,
               COALESCE(NULLIF(l.make,''), w.make)   AS make,
               COALESCE(NULLIF(l.model,''), w.model) AS model,
               COALESCE(NULLIF(l.variant,''), w.variant) AS variant,
               l.year, l.body_type, l.state, l.seller_type,
               w.vehicle_type,
               o.price, o.odometer, o.collected_at
        FROM observations o
        JOIN listings l ON l.id = o.listing_id
        JOIN watches  w ON w.id = l.watch_id
        ORDER BY o.collected_at, o.id
    """).fetchall()
    conn.close()

    records: list[Record] = []
    for r in rows:
        raw = {
            "listing_id": r["source_listing_id"],
            "capture_date": (r["collected_at"] or "")[:10],
            "vehicle_type": r["vehicle_type"],
            "make": r["make"], "model": r["model"], "variant": r["variant"],
            "year": r["year"], "body_type": r["body_type"],
            "odometer": r["odometer"], "price": r["price"],
            "state": r["state"], "seller_type": r["seller_type"],
            "raw_title": r["title"], "url": r["url"],
        }
        rec = make_record(raw, source=(r["source"] or "v2").casefold())
        if rec:
            records.append(rec)
    return records
