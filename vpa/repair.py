"""Repair merged vehicle identities, without ever risking the price data.

    python -m vpa repair-identities            # dry run, shows the plan
    python -m vpa repair-identities --apply    # backs up, then migrates

THE PROBLEM
A key holding two different prices on the SAME capture day cannot be one
car - one listing has one asking price on one day. Two causes:

  * the id-less fingerprint bucketed odometer at 5,000 km, so two cars of
    the same year/variant/state with similar mileage collapsed together
    (318,200 km and 315,000 km both landed in bucket 63);
  * v2-era `IMG-` ids are derived from image URLs and are not unique per
    listing, so two different ads can carry the same id.

THE FIX
`Record.ODO_BUCKET_KM` is now 500 km, which on the real data splits every
merged pair while preserving all 41 existing cross-day links. This module
migrates identities ALREADY STORED to agree with that rule, and re-keys
observations whose ad id has been proven non-unique onto the fingerprint
instead (an id that yields two same-day prices has disproved itself).

SAFETY - the whole point of this module
  * `apply()` takes a `Store.backup()` snapshot BEFORE touching anything.
  * All work happens in ONE transaction. Every invariant below is checked
    before commit; any failure raises and the transaction is rolled back,
    leaving the database byte-for-byte as it was.
  * No observation is ever deleted, edited, or created. Only the `key`
    column that points an observation at its vehicle is rewritten, and
    vehicle rows are added. The full multiset of
    (capture_date, price_aud, price_note, odometer_km) is compared before
    and after and must be identical.
  * Re-keying can only ever SPLIT, never merge: 500 km is a strict
    refinement of 5,000 km, so two rows that get the same new key must
    already have shared the old one. Vehicle count can only go up.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .schema import Record
from .store import Store, DEFAULT_DB

# vehicles columns copied onto a split-off vehicle row
_CARRY = ("source", "listing_id", "vehicle_type", "make", "model", "variant",
          "year", "body_type", "state", "seller_type", "raw_title", "url")


def _rows(store: Store) -> list[dict]:
    return [dict(r) for r in store.conn.execute("""
        SELECT o.id AS obs_id, o.key, o.capture_date, o.price_aud,
               o.price_note, o.odometer_km,
               v.source, v.listing_id, v.vehicle_type, v.make, v.model,
               v.variant, v.year, v.body_type, v.state, v.seller_type,
               v.raw_title, v.url
        FROM observations o JOIN vehicles v ON v.key = o.key""").fetchall()]


def merged_keys(store: Store) -> dict[str, list[dict]]:
    """Keys holding 2+ distinct prices on one capture day -> their rows."""
    by_key_day = defaultdict(set)
    rows = _rows(store)
    for r in rows:
        if r["price_aud"] is not None:
            by_key_day[(r["key"], r["capture_date"])].add(r["price_aud"])
    bad = {k for (k, _d), prices in by_key_day.items() if len(prices) > 1}
    out = defaultdict(list)
    for r in rows:
        if r["key"] in bad:
            out[r["key"]].append(r)
    return dict(out)


def _key_for(r: dict, *, trust_id: bool) -> str:
    """Identity this observation should have under the CURRENT rule.

    Built through the real `Record.dedupe_key()` rather than a copy of it,
    so a migrated key and a key minted by a future import cannot disagree.
    """
    rec = Record(
        source=r["source"], capture_date=r["capture_date"],
        make=r["make"], model=r["model"],
        listing_id=(r["listing_id"] if trust_id else None),
        vehicle_type=r["vehicle_type"] or "car", variant=r["variant"],
        year=r["year"], body_type=r["body_type"],
        odometer_km=r["odometer_km"], price_aud=r["price_aud"],
        price_note=r["price_note"], state=r["state"],
        seller_type=r["seller_type"], raw_title=r["raw_title"], url=r["url"])
    return rec.dedupe_key()


def _target_key(r: dict, bad: set) -> str:
    """The key this observation should end up under.

    Only recompute where there is a reason to; otherwise keep what is
    stored. That is what makes the migration idempotent. An earlier version
    always recomputed, which oscillated: a row split off a disproved ad id
    keeps that id on its new vehicle row, so the next run saw a healthy id
    and pulled it straight back.
    """
    if r["key"] in bad:
        # merged identity: an ad id that yields two same-day prices has
        # disproved itself, so fall back to the fingerprint for these rows
        return _key_for(r, trust_id=False)
    if not r["listing_id"]:
        # fingerprint identity: adopt the current ODO_BUCKET_KM
        return _key_for(r, trust_id=False)
    return r["key"]          # id-based and healthy - leave it completely alone


def plan(store: Store) -> dict:
    """Work out every observation's correct key. No writes."""
    rows = _rows(store)
    bad = set(merged_keys(store))

    moves: list[tuple[int, str, str]] = []      # (obs_id, old_key, new_key)
    by_new: dict[str, dict] = {}
    for r in rows:
        new = _target_key(r, bad)
        by_new.setdefault(new, r)
        if new != r["key"]:
            moves.append((r["obs_id"], r["key"], new))

    existing = {k[0] for k in store.conn.execute("SELECT key FROM vehicles")}
    return {"rows": len(rows), "moves": moves,
            "merged_before": len(bad),
            "new_vehicles": sorted(set(by_new) - existing),
            "attrs": by_new}


def _fingerprint(rows: list[dict]) -> list:
    """Order-independent signature of the price data. Must not change."""
    sig = [(r["capture_date"], r["price_aud"], r["price_note"], r["odometer_km"])
           for r in rows]
    return sorted(sig, key=lambda t: tuple("" if x is None else str(x) for x in t))


def apply(db_path: str | Path = DEFAULT_DB, *, dry_run: bool = True) -> dict:
    """Migrate identities. Returns a report. dry_run=True writes nothing."""
    with Store(db_path) as store:
        before_rows = _rows(store)
        p = plan(store)
        report = {
            "dry_run": dry_run,
            "observations": len(before_rows),
            "vehicles_before": store.conn.execute(
                "SELECT COUNT(*) FROM vehicles").fetchone()[0],
            "merged_before": p["merged_before"],
            "observations_rekeyed": len(p["moves"]),
            "vehicles_added": len(p["new_vehicles"]),
            "backup": None, "vehicles_after": None, "merged_after": None,
        }
        if dry_run or not p["moves"]:
            report["merged_after"] = p["merged_before"] if dry_run else 0
            return report

        # ---- 1. snapshot first, always
        report["backup"] = str(store.backup())

        before_sig = _fingerprint(before_rows)
        n_before = len(before_rows)
        v_before = report["vehicles_before"]

        # ---- 2. one transaction; any assertion failure rolls it all back.
        # Autocommit mode so BEGIN/COMMIT/ROLLBACK below are exactly the
        # transaction boundary, rather than sqlite3's implicit one.
        prev_isolation = store.conn.isolation_level
        store.conn.isolation_level = None
        try:
            store.conn.execute("BEGIN IMMEDIATE")
            for key in p["new_vehicles"]:
                src = p["attrs"][key]
                cols = ", ".join(("key",) + _CARRY + ("first_seen", "last_seen"))
                marks = ", ".join("?" * (len(_CARRY) + 3))
                store.conn.execute(
                    f"INSERT INTO vehicles ({cols}) VALUES ({marks})",
                    [key] + [src[c] for c in _CARRY]
                    + [src["capture_date"], src["capture_date"]])
            for obs_id, _old, new in p["moves"]:
                store.conn.execute(
                    "UPDATE observations SET key = ? WHERE id = ?", (new, obs_id))

            # vehicles left with no observations are now empty shells
            store.conn.execute("""
                DELETE FROM vehicles WHERE key NOT IN
                (SELECT DISTINCT key FROM observations)""")
            # first_seen/last_seen must describe the rows each key now holds
            store.conn.execute("""
                UPDATE vehicles SET
                  first_seen = (SELECT MIN(capture_date) FROM observations o
                                WHERE o.key = vehicles.key),
                  last_seen  = (SELECT MAX(capture_date) FROM observations o
                                WHERE o.key = vehicles.key)""")

            # ---- 3. invariants: price data untouched, nothing orphaned
            after_rows = _rows(store)
            if len(after_rows) != n_before:
                raise RuntimeError(
                    f"observation count changed {n_before} -> {len(after_rows)}")
            if _fingerprint(after_rows) != before_sig:
                raise RuntimeError("price data changed - refusing to commit")
            orphans = store.conn.execute("""
                SELECT COUNT(*) FROM observations o
                WHERE NOT EXISTS (SELECT 1 FROM vehicles v WHERE v.key = o.key)
                """).fetchone()[0]
            if orphans:
                raise RuntimeError(f"{orphans} observation(s) lost their vehicle")
            v_after = store.conn.execute(
                "SELECT COUNT(*) FROM vehicles").fetchone()[0]
            if v_after < v_before:
                raise RuntimeError(
                    f"vehicles fell {v_before} -> {v_after}; a split cannot merge")
            left = len(merged_keys(store))
            if left:
                raise RuntimeError(f"{left} merged identity(ies) still present")
            store.conn.execute("COMMIT")
        except Exception:
            store.conn.execute("ROLLBACK")
            raise
        finally:
            store.conn.isolation_level = prev_isolation

        report["vehicles_after"] = v_after
        report["merged_after"] = 0
        return report


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="vpa repair-identities",
        description="Split vehicle identities that merged two different cars.")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--apply", action="store_true",
                    help="actually migrate (a backup is taken first)")
    args = ap.parse_args(argv)

    r = apply(args.db, dry_run=not args.apply)
    print(f"observations            : {r['observations']}")
    print(f"vehicles before         : {r['vehicles_before']}")
    print(f"merged identities       : {r['merged_before']}")
    print(f"observations to re-key  : {r['observations_rekeyed']}")
    print(f"vehicle rows to add     : {r['vehicles_added']}")
    if r["dry_run"]:
        print("\nDRY RUN - nothing was written. Re-run with --apply to migrate;"
              "\na backup is taken automatically before any change.")
    else:
        print(f"\nbackup                  : {r['backup']}")
        print(f"vehicles after          : {r['vehicles_after']}")
        print(f"merged identities left  : {r['merged_after']}")
        print("\nPrice data verified identical before and after.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
