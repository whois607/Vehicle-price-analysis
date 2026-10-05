"""SQLite persistence: append + dedupe, never lose history.

    vehicles      one row per physical listing (dedupe key)
    observations  one price/odometer sighting per capture date — the history
    ingest_log    what was ingested when, and what it added

Re-ingesting the same file is idempotent (observation identity is
key + capture_date + price + odometer, NULL-safe). A new capture of a known
vehicle at a new price appends an observation — that's the price history.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .schema import Record

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL, listing_id TEXT,
    vehicle_type TEXT NOT NULL DEFAULT 'car',
    make TEXT NOT NULL, model TEXT NOT NULL, variant TEXT,
    year INTEGER, body_type TEXT, state TEXT, seller_type TEXT,
    raw_title TEXT, url TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL REFERENCES vehicles(key) ON DELETE CASCADE,
    capture_date TEXT NOT NULL,
    price_aud REAL, price_note TEXT, odometer_km REAL,
    batch_id INTEGER);

CREATE INDEX IF NOT EXISTS idx_obs_key ON observations(key);
CREATE INDEX IF NOT EXISTS idx_obs_date ON observations(capture_date);

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL, source_file TEXT, adapter TEXT,
    rows_parsed INTEGER, vehicles_new INTEGER,
    obs_new INTEGER, obs_duplicate INTEGER);

-- Every manual correction made after a row was committed. Ingestion is
-- append-only and reproducible from the source files; a hand edit is neither,
-- so it is the one kind of change that must leave a record of itself.
-- `before` holds enough to put the row back exactly as it was, which is what
-- makes an edit undoable rather than merely regrettable.
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY,
    batch TEXT NOT NULL,          -- one submission; the unit of undo
    ts TEXT NOT NULL,
    op TEXT NOT NULL,             -- vehicle | observation | delete_* | rename_variant
    target TEXT NOT NULL,         -- vehicles.key, or observations.id as text
    field TEXT,                   -- NULL for whole-row operations
    before TEXT, after TEXT,      -- text form; JSON for whole-row operations
    reason TEXT,
    undone_at TEXT);

CREATE INDEX IF NOT EXISTS idx_edits_batch ON edits(batch);

-- Cars you are actually interested in. This is YOUR annotation, not something
-- a capture recorded, so it lives in its own table rather than as a column on
-- `vehicles`: no statistic may ever group by it, and re-importing must never
-- touch it. Deliberately NO foreign key — a star whose vehicle has gone is
-- not an error to clean up, it is the single most interesting thing the
-- watchlist can tell you, so it survives to be reported.
CREATE TABLE IF NOT EXISTS starred (
    key TEXT PRIMARY KEY,
    starred_at TEXT NOT NULL,
    note TEXT);

-- One direct (no-preview) upload run. The Import page shows you every row
-- before it lands, so a mistake is caught by eye; the Bulk page deliberately
-- does not, which moves the whole safety net to AFTER the write. This table
-- is that net: it names the ingest batches the run created, so undo can
-- delete exactly those observations and nothing else, and carries the prior
-- state of every vehicle the run touched, so a vehicle that already existed
-- is restored rather than deleted.
--   batches   JSON list of ingest_log.id
--   before    JSON {key: {column: value}} for keys that ALREADY existed
--   files     JSON list of {name, records, obs_new, error}
-- No foreign key: the point of a run log is to outlive what it describes.
CREATE TABLE IF NOT EXISTS bulk_runs (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    label TEXT,
    backup_path TEXT,
    batches TEXT NOT NULL,
    before TEXT NOT NULL,
    files TEXT NOT NULL,
    rows_parsed INTEGER, vehicles_new INTEGER,
    obs_new INTEGER, obs_duplicate INTEGER,
    undone_at TEXT);

-- One identity folded into another from the Duplicates page, kept so the
-- merge can be undone exactly. A merge only re-points observations and
-- removes the emptied vehicle row, so undoing it needs: the vehicle row as
-- it was, which observations moved, and whether its star moved with it.
--   vehicle   JSON {column: value} of the absorbed vehicle row
--   obs_ids   JSON list of observations.id re-pointed to `winner`
--   star      JSON {starred_at, note} of the absorbed key's star, or NULL
--   star_new  1 if the winner was starred only BECAUSE of this merge
--   winner_first/last  the survivor's first/last_seen before this merge
-- No foreign key: the log must outlive the row it describes.
CREATE TABLE IF NOT EXISTS merge_log (
    id INTEGER PRIMARY KEY,
    batch TEXT NOT NULL,
    ts TEXT NOT NULL,
    winner TEXT NOT NULL,
    absorbed TEXT NOT NULL,
    vehicle TEXT NOT NULL,
    obs_ids TEXT NOT NULL,
    star TEXT, star_new INTEGER,
    winner_first TEXT, winner_last TEXT,   -- the survivor's window BEFORE
    basis TEXT,
    backup_path TEXT,
    undone_at TEXT);
CREATE INDEX IF NOT EXISTS idx_merge_log_batch ON merge_log(batch);

-- Groups you looked at on the Duplicates page and said are DIFFERENT cars.
-- `keys` is the group's vehicle keys, sorted and comma-joined: if a new
-- identity later joins the group, it is a new question and is asked again.
CREATE TABLE IF NOT EXISTS not_duplicates (
    keys TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    note TEXT);
"""

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "vpa.db"

_FRAME_SQL = """
    SELECT o.key, o.capture_date, o.price_aud, o.price_note, o.odometer_km,
           v.source, v.listing_id, v.vehicle_type, v.make, v.model,
           v.variant, v.year, v.body_type, v.state, v.seller_type,
           v.raw_title, v.url, v.first_seen, v.last_seen
    FROM observations o JOIN vehicles v ON v.key = o.key
    ORDER BY o.capture_date, o.key
"""


def _frame_from(conn) -> pd.DataFrame:
    df = pd.read_sql_query(_FRAME_SQL, conn)
    if not df.empty:
        df["capture_date"] = pd.to_datetime(df["capture_date"])
    # Per-row detail marker (backfill rule confirmed 04/07/2026): quick_add
    # rows carry only model+price+date -> 'brief'; every other source
    # (imports, v2 history, manual adds) -> 'full'. Stats that need only
    # model+price+date accept both; richer stats restrict to 'full'.
    df["detail_level"] = (df["source"] == "quick_add").map(
        {True: "brief", False: "full"})
    return df


def connect_readonly(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """A connection that CANNOT write: SQLite itself refuses every change.
    For code that only ever reads (the phone copy), so a bug there can never
    reach the data. Unlike Store(), it never creates the file or the schema;
    a missing database is an error, not an empty one."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no database at {p}")
    conn = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def read_frame(path: str | Path = DEFAULT_DB) -> pd.DataFrame:
    """Store(path).frame(), through a read-only connection."""
    conn = connect_readonly(path)
    try:
        return _frame_from(conn)
    finally:
        conn.close()


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- writes -------------------------------------------------------------
    def ingest(self, records: list[Record], source_file: str = "",
               adapter: str = "") -> dict:
        """Append records, deduping vehicles by key and observations by
        (key, capture_date, price, odometer). Returns counts."""
        cur = self.conn.execute(
            "INSERT INTO ingest_log (ts, source_file, adapter, rows_parsed,"
            " vehicles_new, obs_new, obs_duplicate) VALUES (?,?,?,?,0,0,0)",
            (datetime.now().isoformat(sep=" ", timespec="seconds"),
             source_file, adapter, len(records)))
        batch_id = cur.lastrowid
        v_new = o_new = o_dupe = 0
        for r in records:
            key = r.dedupe_key()
            row = self.conn.execute("SELECT key FROM vehicles WHERE key=?", (key,)).fetchone()
            if row:
                # refresh identity fields, filling gaps but never blanking
                self.conn.execute(
                    """UPDATE vehicles SET last_seen=MAX(last_seen, ?),
                       variant=COALESCE(variant, ?), year=COALESCE(year, ?),
                       body_type=COALESCE(body_type, ?), state=COALESCE(state, ?),
                       seller_type=COALESCE(seller_type, ?),
                       raw_title=COALESCE(raw_title, ?), url=COALESCE(url, ?)
                       WHERE key=?""",
                    (r.capture_date, r.variant, r.year, r.body_type, r.state,
                     r.seller_type, r.raw_title, r.url, key))
            else:
                self.conn.execute(
                    """INSERT INTO vehicles (key, source, listing_id, vehicle_type,
                       make, model, variant, year, body_type, state, seller_type,
                       raw_title, url, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, r.source, r.listing_id, r.vehicle_type, r.make, r.model,
                     r.variant, r.year, r.body_type, r.state, r.seller_type,
                     r.raw_title, r.url, r.capture_date, r.capture_date))
                v_new += 1
            dupe = self.conn.execute(
                """SELECT 1 FROM observations WHERE key=? AND capture_date=?
                   AND price_aud IS ? AND odometer_km IS ?""",
                (key, r.capture_date, r.price_aud, r.odometer_km)).fetchone()
            if dupe:
                o_dupe += 1
            else:
                self.conn.execute(
                    """INSERT INTO observations (key, capture_date, price_aud,
                       price_note, odometer_km, batch_id) VALUES (?,?,?,?,?,?)""",
                    (key, r.capture_date, r.price_aud, r.price_note,
                     r.odometer_km, batch_id))
                o_new += 1
        self.conn.execute(
            "UPDATE ingest_log SET vehicles_new=?, obs_new=?, obs_duplicate=? WHERE id=?",
            (v_new, o_new, o_dupe, batch_id))
        self.conn.commit()
        return {"rows_parsed": len(records), "vehicles_new": v_new,
                "obs_new": o_new, "obs_duplicate": o_dupe}

    def preview(self, records: list[Record]) -> list[dict]:
        """Dry-run classification of records against the store — NO writes.

        Per record: {'key', 'status': 'new' | 'new_observation' | 'duplicate',
        'prior_price': float|None}. Uses exactly the same identity and
        observation-duplicate rules as ingest(), so what the preview says is
        what ingest() will do."""
        out = []
        for r in records:
            key = r.dedupe_key()
            row = self.conn.execute(
                "SELECT key FROM vehicles WHERE key=?", (key,)).fetchone()
            if not row:
                out.append({"key": key, "status": "new", "prior_price": None})
                continue
            dupe = self.conn.execute(
                """SELECT 1 FROM observations WHERE key=? AND capture_date=?
                   AND price_aud IS ? AND odometer_km IS ?""",
                (key, r.capture_date, r.price_aud, r.odometer_km)).fetchone()
            prior = self.conn.execute(
                """SELECT price_aud FROM observations WHERE key=?
                   AND price_aud IS NOT NULL
                   ORDER BY capture_date DESC, id DESC LIMIT 1""", (key,)).fetchone()
            out.append({"key": key,
                        "status": "duplicate" if dupe else "new_observation",
                        "prior_price": prior["price_aud"] if prior else None})
        return out

    # --- reads --------------------------------------------------------------
    def frame(self) -> pd.DataFrame:
        """Every observation joined to its vehicle — the analysis dataset.
        One row = one sighting of one vehicle on one date."""
        return _frame_from(self.conn)

    def sightings(self, key: str) -> list[dict]:
        """Every stored sighting of ONE vehicle, with the import that recorded it.

        `frame()` is the analysis dataset and deliberately carries no batch
        columns — no statistic should ever depend on which file a row arrived
        in. This is the separate AUDIT view, for one key at a time: it answers
        "did this car really appear in my earlier captures, or has something
        merged two cars onto one identity?" by naming the ingest behind each
        observation — when it ran, what it read, and how many rows it carried.

        LEFT JOIN, not JOIN: rows written before the batch_id column existed,
        and rows whose ingest_log entry was pruned, still have to be listed.
        An observation that cannot name its import is itself the finding.

        Read-only.
        """
        rows = self.conn.execute("""
            SELECT o.id, o.capture_date, o.price_aud, o.price_note,
                   o.odometer_km, o.batch_id,
                   l.ts AS imported_at, l.source_file, l.adapter,
                   l.rows_parsed, l.vehicles_new
            FROM observations o
            LEFT JOIN ingest_log l ON l.id = o.batch_id
            WHERE o.key = ?
            ORDER BY o.capture_date, o.id
        """, (key,)).fetchall()
        return [dict(r) for r in rows]

    def status(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "db_path": str(self.path),
            "vehicles": q("SELECT COUNT(*) FROM vehicles"),
            "observations": q("SELECT COUNT(*) FROM observations"),
            "priced_observations": q("SELECT COUNT(*) FROM observations WHERE price_aud IS NOT NULL"),
            "date_range": tuple(self.conn.execute(
                "SELECT MIN(capture_date), MAX(capture_date) FROM observations").fetchone()),
            "sources": dict(self.conn.execute(
                "SELECT source, COUNT(*) FROM vehicles GROUP BY source").fetchall()),
            "ingests": q("SELECT COUNT(*) FROM ingest_log"),
        }

    def backup(self, dest_dir: str | Path | None = None) -> Path:
        """Consistent snapshot via SQLite's backup API (safe while the app
        is running). Default destination: data/backups/."""
        dest_dir = Path(dest_dir) if dest_dir else self.path.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{self.path.stem}_backup_{stamp}.db"
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return dest

    def price_changes(self) -> pd.DataFrame:
        """Vehicles whose price moved between captures — re-list/haggle signal."""
        df = self.frame()
        if df.empty:
            return df
        priced = df.dropna(subset=["price_aud"]).sort_values("capture_date", kind="stable")
        g = priced.groupby("key")["price_aud"]
        changed = priced[g.transform("nunique") > 1]
        return changed[["key", "make", "model", "variant", "year",
                        "capture_date", "price_aud", "source", "raw_title"]]
