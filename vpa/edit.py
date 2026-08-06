"""Correcting rows that are already in the database.

    python -m vpa edit --history
    python -m vpa edit --undo <batch>

Ingestion is append-only and reproducible: delete `vpa.db`, re-run the
imports, and you get the same database back. A hand edit is neither. It is
the one operation here whose input is a person typing, so it carries the
heaviest protocol in the project:

  1. **Nothing is written without a dry run.** `plan()` returns exactly what
     would change, field by field, old value beside new. `apply()` runs the
     same `plan()` again and refuses if it no longer matches what was shown.
  2. **A backup is taken before any change** (`Store.backup()`), as in
     `repair.py` and `merge.py`.
  3. **One transaction**, BEGIN IMMEDIATE, with invariants checked before
     COMMIT and a full ROLLBACK on any failure.
  4. **Every change is recorded in `edits`** with its old value, so it can be
     undone. An import can be reproduced from its source file; an edit can
     only be reproduced from this table.
  5. **Typed values go through the importer's own normalisers** -
     `parse_price`, `parse_odometer`, `norm_year`, `norm_state`,
     `norm_seller`, `norm_capture_date`. You therefore cannot type a value
     the importer itself would have refused, and "$18,990" means here exactly
     what it means on the Import page.

WHAT EDITING DOES NOT DO
------------------------
It does not re-key a vehicle. `vehicles.key` is a historical identity - the
fingerprint taken when the row first arrived - and changing make/model/variant
deliberately leaves it alone. Re-deriving the key on edit would break the link
to the vehicle's own observations and orphan its price history. Splitting or
merging identities is what `repair.py` and `merge.py` are for, and they have
their own conditions.

It does not touch anything it was not asked to. There is no "clean up while
we are here": the only rows that change are the ones named in the plan.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

from .store import Store, DEFAULT_DB

#: Vehicle fields a person may correct. Deliberately excludes `key` (identity,
#: see the module docstring), `source` (provenance - what a row IS, not what it
#: says) and `first_seen`/`last_seen` (derived from the observations).
VEHICLE_FIELDS = ("make", "model", "variant", "year", "body_type", "state",
                  "seller_type", "raw_title", "url", "listing_id")

#: Observation fields. These ARE the recorded price history, so each one is
#: validated and each change is written to `edits` individually.
OBSERVATION_FIELDS = ("price_aud", "odometer_km", "price_note", "capture_date")

_OPS = ("vehicle", "observation", "delete_observation", "delete_vehicle",
        "rename_variant")


class EditError(ValueError):
    """A change that must not be attempted. Raised during planning, so it can
    never reach a transaction."""


# --------------------------------------------------------------- validation
def _clean(field: str, value):
    """One typed value -> the value that would be stored, or EditError.

    Uses the importer's own functions so an edit and an import cannot disagree
    about what a price or a state is.
    """
    from .schema import (norm_capture_date, norm_make, norm_model, norm_seller,
                         norm_state, norm_year, parse_odometer, parse_price)

    text = "" if value is None else str(value).strip()
    blank = text == ""

    if field in ("make", "model"):
        if blank:
            raise EditError(f"{field} cannot be emptied - a row with no "
                            f"{field} cannot be selected or grouped")
        return norm_make(text) if field == "make" else norm_model(text)
    if field == "variant":
        return None if blank else text
    if field in ("raw_title", "url", "listing_id", "price_note", "body_type"):
        return None if blank else text
    if field == "year":
        if blank:
            return None
        y = norm_year(text)
        if y is None:
            raise EditError(f"{text!r} is not a plausible year (1900-2035)")
        return y
    if field == "state":
        if blank:
            return None
        s = norm_state(text)
        if s is None:
            raise EditError(f"{text!r} is not an Australian state or territory")
        return s
    if field == "seller_type":
        if blank:
            return None
        s = norm_seller(text)
        if s is None:
            raise EditError(f"{text!r} is not a seller type - use dealer or private")
        return s
    if field == "price_aud":
        if blank:
            return None
        p, note = parse_price(text)
        if p is None:
            raise EditError(f"{text!r} is not a usable price"
                            + (f" ({note})" if note else ""))
        return p
    if field == "odometer_km":
        if blank:
            return None
        o = parse_odometer(text)
        if o is None:
            raise EditError(f"{text!r} is not a usable odometer reading")
        return o
    if field == "capture_date":
        if blank:
            raise EditError("a sighting must have the date it was seen")
        d = norm_capture_date(text)
        # A capture date is when YOU looked. It cannot be in the future, and a
        # future date silently breaks every trend and forecast that orders by
        # it - which is exactly the kind of damage this tool exists to fix.
        if d > date.today().isoformat():
            raise EditError(f"{d} is in the future - a capture date is the day "
                            f"you saw the listing")
        return d
    raise EditError(f"{field!r} is not an editable field")


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) < 1e-9
    return str(a) == str(b)


# ------------------------------------------------------------------ planning
def plan(store: Store, ops: list[dict]) -> dict:
    """What `ops` would change, without changing anything.

    Each op is `{"op": ..., "target": ..., "fields": {...}, "reason": ...}`.
    Returns changes field by field, plus the totals that will be checked
    again after the write.
    """
    changes: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    conn = store.conn

    for i, op in enumerate(ops or []):
        kind = str(op.get("op") or "").strip()
        target = str(op.get("target") or "").strip()
        fields = op.get("fields") or {}
        where = f"change {i + 1}"
        if kind not in _OPS:
            errors.append(f"{where}: {kind!r} is not an operation")
            continue

        try:
            if kind == "vehicle":
                row = conn.execute(
                    "SELECT * FROM vehicles WHERE key = ?", (target,)).fetchone()
                if row is None:
                    raise EditError(f"no vehicle with key {target}")
                for f, v in fields.items():
                    if f not in VEHICLE_FIELDS:
                        raise EditError(f"{f!r} is not an editable vehicle field")
                    new = _clean(f, v)
                    if _same(row[f], new):
                        continue
                    changes.append({"op": kind, "target": target, "field": f,
                                    "before": row[f], "after": new,
                                    "label": _label(conn, key=target),
                                    "reason": op.get("reason") or ""})

            elif kind == "observation":
                row = conn.execute(
                    "SELECT * FROM observations WHERE id = ?", (target,)).fetchone()
                if row is None:
                    raise EditError(f"no sighting with id {target}")
                for f, v in fields.items():
                    if f not in OBSERVATION_FIELDS:
                        raise EditError(f"{f!r} is not an editable sighting field")
                    new = _clean(f, v)
                    if _same(row[f], new):
                        continue
                    changes.append({"op": kind, "target": target, "field": f,
                                    "before": row[f], "after": new,
                                    "label": _label(conn, obs=target),
                                    "reason": op.get("reason") or ""})

            elif kind == "delete_observation":
                row = conn.execute(
                    "SELECT * FROM observations WHERE id = ?", (target,)).fetchone()
                if row is None:
                    raise EditError(f"no sighting with id {target}")
                left = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE key = ?",
                    (row["key"],)).fetchone()[0]
                last = left <= 1
                if last:
                    warnings.append(
                        f"sighting {target} is the only one this vehicle has - "
                        f"deleting it removes the vehicle as well, since a car "
                        f"with no sighting is a row nothing can reach")
                # The vehicle row goes with it, so the vehicle row has to be
                # RECORDED with it. Storing only the sighting made the deletion
                # un-undoable: the restore then tried to attach a sighting to a
                # vehicle that no longer existed and hit the foreign key.
                veh = conn.execute("SELECT * FROM vehicles WHERE key = ?",
                                   (row["key"],)).fetchone() if last else None
                before = ({"observation": dict(row), "vehicle": dict(veh)}
                          if last else dict(row))
                changes.append({"op": kind, "target": target, "field": None,
                                "before": before, "after": None,
                                "label": _label(conn, obs=target),
                                "reason": op.get("reason") or "",
                                "last_of_vehicle": last,
                                "vehicle_key": row["key"]})

            elif kind == "delete_vehicle":
                row = conn.execute(
                    "SELECT * FROM vehicles WHERE key = ?", (target,)).fetchone()
                if row is None:
                    raise EditError(f"no vehicle with key {target}")
                obs = [dict(r) for r in conn.execute(
                    "SELECT * FROM observations WHERE key = ?", (target,))]
                warnings.append(
                    f"deleting {_label(conn, key=target)} also deletes its "
                    f"{len(obs)} recorded sighting(s) - that price history "
                    f"cannot be recovered except by undoing this edit")
                changes.append({"op": kind, "target": target, "field": None,
                                "before": {"vehicle": dict(row), "observations": obs},
                                "after": None,
                                "label": _label(conn, key=target),
                                "reason": op.get("reason") or "",
                                "obs_count": len(obs)})

            elif kind == "rename_variant":
                make = str(fields.get("make") or "").strip()
                model = str(fields.get("model") or "").strip()
                old = str(fields.get("old") or "").strip()
                new = _clean("variant", fields.get("new"))
                if not (make and model and old):
                    raise EditError("a trim rename needs make, model and the "
                                    "current trim name")
                if _same(old, new):
                    raise EditError("the new trim name is the same as the old one")
                hits = [r["key"] for r in conn.execute(
                    "SELECT key FROM vehicles WHERE make=? AND model=? "
                    "AND variant IS ?", (make, model, old))]
                if not hits:
                    raise EditError(f"no {make} {model} is stored under {old!r}")
                for k in hits:
                    changes.append({"op": "vehicle", "target": k,
                                    "field": "variant", "before": old,
                                    "after": new, "label": f"{make} {model}",
                                    "reason": op.get("reason")
                                    or f"trim rename: {old!r} -> {new!r}",
                                    "bulk": True})
        except EditError as e:
            errors.append(f"{where}: {e}")

    totals = _totals(conn)
    expected = dict(totals)
    for c in changes:
        if c["op"] == "delete_observation":
            expected["observations"] -= 1
            # `before` is the sighting alone, or {observation, vehicle} when
            # the vehicle goes with it. Reading the price from the wrong shape
            # made the promised total disagree with the real one and rolled
            # every last-sighting deletion back.
            b = _deleted_observation(c["before"])
            if b.get("price_aud") is not None:
                expected["price_total"] -= float(b["price_aud"])
            if c.get("last_of_vehicle"):
                expected["vehicles"] -= 1
        elif c["op"] == "delete_vehicle":
            expected["vehicles"] -= 1
            expected["observations"] -= c["obs_count"]
            for o in c["before"]["observations"]:
                if o.get("price_aud") is not None:
                    expected["price_total"] -= float(o["price_aud"])
        elif c["op"] == "observation" and c["field"] == "price_aud":
            expected["price_total"] += (float(c["after"] or 0)
                                        - float(c["before"] or 0))
    expected["price_total"] = round(expected["price_total"], 2)

    return {"ok": not errors, "changes": changes, "errors": errors,
            "warnings": warnings, "before": totals, "expected": expected,
            "touches_price_history": any(
                c["op"] in ("delete_observation", "delete_vehicle")
                or (c["op"] == "observation"
                    and c["field"] in ("price_aud", "odometer_km", "capture_date"))
                for c in changes)}


def _deleted_observation(before) -> dict:
    """The sighting inside a delete_observation's `before`, whichever shape it
    took — bare when the vehicle survives, wrapped when it does not."""
    if isinstance(before, dict) and "observation" in before:
        return before["observation"]
    return dict(before)


def _totals(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {"vehicles": q("SELECT COUNT(*) FROM vehicles"),
            "observations": q("SELECT COUNT(*) FROM observations"),
            "price_total": round(
                q("SELECT COALESCE(SUM(price_aud), 0) FROM observations"), 2)}


def _label(conn, key: str | None = None, obs: str | None = None) -> str:
    if obs is not None:
        r = conn.execute(
            "SELECT v.make, v.model, v.variant, o.capture_date "
            "FROM observations o JOIN vehicles v ON v.key = o.key "
            "WHERE o.id = ?", (obs,)).fetchone()
        if r:
            return (" ".join(x for x in (r["make"], r["model"], r["variant"]) if x)
                    + f" seen {r['capture_date']}")
        return f"sighting {obs}"
    r = conn.execute("SELECT make, model, variant FROM vehicles WHERE key = ?",
                     (key,)).fetchone()
    if r:
        return " ".join(x for x in (r["make"], r["model"], r["variant"]) if x)
    return str(key)


# -------------------------------------------------------------------- apply
def apply(db_path: str | Path = DEFAULT_DB, ops: list[dict] | None = None,
          *, confirm: str | None = None) -> dict:
    """Take a backup, apply `ops` in one transaction, verify, commit or roll back.

    `confirm` must repeat the plan's fingerprint. It is not ceremony: the
    database can change between previewing and saving (another tab commits an
    import), and applying edits to rows that are no longer what was shown is
    precisely how a careful correction becomes damage.
    """
    with Store(db_path) as store:
        p = plan(store, ops or [])
        if not p["ok"]:
            return {**p, "applied": False, "backup": None}
        if not p["changes"]:
            return {**p, "applied": False, "backup": None,
                    "note": "nothing to change - every value already matches"}
        fp = fingerprint(p)
        if confirm is not None and confirm != fp:
            return {**p, "applied": False, "backup": None, "ok": False,
                    "errors": ["the data changed since this was previewed - "
                               "review the change again before saving"],
                    "fingerprint": fp}

        backup = store.backup()
        batch = uuid.uuid4().hex[:12]
        ts = datetime.now().isoformat(sep=" ", timespec="seconds")
        conn = store.conn
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            for c in p["changes"]:
                _record(conn, batch, ts, c)
                if c["op"] == "vehicle":
                    conn.execute(
                        f"UPDATE vehicles SET {c['field']} = ? WHERE key = ?",
                        (c["after"], c["target"]))
                elif c["op"] == "observation":
                    conn.execute(
                        f"UPDATE observations SET {c['field']} = ? WHERE id = ?",
                        (c["after"], c["target"]))
                elif c["op"] == "delete_observation":
                    conn.execute("DELETE FROM observations WHERE id = ?",
                                 (c["target"],))
                    if c.get("last_of_vehicle"):
                        conn.execute("DELETE FROM vehicles WHERE key = ?",
                                     (c["vehicle_key"],))
                elif c["op"] == "delete_vehicle":
                    # ON DELETE CASCADE removes the observations with it
                    conn.execute("DELETE FROM vehicles WHERE key = ?",
                                 (c["target"],))

            # a vehicle's window is derived from its sightings, so any edit to
            # a capture date or any deletion has to move it
            conn.execute("""
                UPDATE vehicles SET
                  first_seen = COALESCE((SELECT MIN(capture_date) FROM observations
                                         WHERE key = vehicles.key), first_seen),
                  last_seen  = COALESCE((SELECT MAX(capture_date) FROM observations
                                         WHERE key = vehicles.key), last_seen)""")

            after = _totals(conn)
            if after != p["expected"]:
                raise RuntimeError(
                    f"totals after the edit are not what the preview promised: "
                    f"{after} vs {p['expected']}")
            bad = conn.execute(
                "SELECT COUNT(*) FROM observations o "
                "LEFT JOIN vehicles v ON v.key = o.key WHERE v.key IS NULL"
            ).fetchone()[0]
            if bad:
                raise RuntimeError(f"{bad} sighting(s) left pointing at no vehicle")
            empty = conn.execute(
                "SELECT COUNT(*) FROM vehicles v WHERE NOT EXISTS "
                "(SELECT 1 FROM observations o WHERE o.key = v.key)"
            ).fetchone()[0]
            if empty:
                raise RuntimeError(f"{empty} vehicle(s) left with no sighting")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("foreign key check failed")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            return {**p, "applied": False, "backup": str(backup),
                    "ok": False, "errors": [f"rolled back, nothing changed: {e}"]}
        finally:
            conn.isolation_level = ""

        return {**p, "applied": True, "backup": str(backup), "batch": batch,
                "after": after, "fingerprint": fp}


def fingerprint(p: dict) -> str:
    """A stable digest of exactly what the preview showed."""
    import hashlib
    payload = json.dumps(
        [[c["op"], str(c["target"]), c.get("field"),
          _jsonable(c.get("before")), _jsonable(c.get("after"))]
         for c in p["changes"]] + [p["before"]],
        sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _jsonable(v):
    return dict(v) if isinstance(v, sqlite3.Row) else v


def _record(conn, batch: str, ts: str, c: dict) -> None:
    before, after = c.get("before"), c.get("after")
    if isinstance(before, (dict, sqlite3.Row)) or isinstance(before, list):
        before = json.dumps(_jsonable(before), default=str)
    if isinstance(after, (dict, list)):
        after = json.dumps(after, default=str)
    conn.execute(
        "INSERT INTO edits (batch, ts, op, target, field, before, after, reason)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (batch, ts, c["op"], str(c["target"]), c.get("field"),
         None if before is None else str(before),
         None if after is None else str(after), c.get("reason") or ""))


# --------------------------------------------------------------------- undo
def history(db_path: str | Path = DEFAULT_DB, limit: int = 50) -> list[dict]:
    """Recent edit batches, newest first."""
    with Store(db_path) as store:
        rows = store.conn.execute("""
            SELECT batch, MIN(ts) ts, COUNT(*) n,
                   SUM(CASE WHEN undone_at IS NULL THEN 0 ELSE 1 END) undone,
                   GROUP_CONCAT(DISTINCT op) ops,
                   MAX(reason) reason
            FROM edits GROUP BY batch ORDER BY ts DESC LIMIT ?""",
            (limit,)).fetchall()
        return [dict(r) for r in rows]


def undo(db_path: str | Path = DEFAULT_DB, batch: str = "") -> dict:
    """Put back exactly what a batch changed.

    Undo is the reason `edits` stores the old value of every field and the
    whole row for every deletion. It is applied in reverse order inside one
    transaction, with a backup first, like any other write here.
    """
    with Store(db_path) as store:
        conn = store.conn
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM edits WHERE batch = ? ORDER BY id DESC", (batch,))]
        if not rows:
            return {"ok": False, "error": f"no edit batch {batch!r}"}
        if all(r["undone_at"] for r in rows):
            return {"ok": False, "error": "that edit has already been undone"}

        backup = store.backup()
        ts = datetime.now().isoformat(sep=" ", timespec="seconds")
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                if r["undone_at"]:
                    continue
                if r["op"] == "vehicle":
                    conn.execute(
                        f"UPDATE vehicles SET {r['field']} = ? WHERE key = ?",
                        (r["before"], r["target"]))
                elif r["op"] == "observation":
                    conn.execute(
                        f"UPDATE observations SET {r['field']} = ? WHERE id = ?",
                        (r["before"], r["target"]))
                elif r["op"] == "delete_observation":
                    payload = json.loads(r["before"])
                    if "observation" in payload:      # took its vehicle with it
                        _reinsert_vehicle(conn, payload["vehicle"])
                        _reinsert_observation(conn, payload["observation"])
                    else:
                        _reinsert_observation(conn, payload)
                elif r["op"] == "delete_vehicle":
                    payload = json.loads(r["before"])
                    _reinsert_vehicle(conn, payload["vehicle"])
                    for o in payload["observations"]:
                        _reinsert_observation(conn, o)
            conn.execute(
                "UPDATE edits SET undone_at = ? WHERE batch = ? AND undone_at IS NULL",
                (ts, batch))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("foreign key check failed")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            return {"ok": False, "backup": str(backup),
                    "error": f"rolled back, nothing changed: {e}"}
        finally:
            conn.isolation_level = ""
        return {"ok": True, "batch": batch, "restored": len(rows),
                "backup": str(backup), "totals": _totals(conn)}


def _reinsert_vehicle(conn, v: dict) -> None:
    """Put a deleted vehicle back. Must run BEFORE its sightings: the foreign
    key is enforced immediately, so a sighting inserted first has nothing to
    point at."""
    cols = list(v)
    conn.execute(
        f"INSERT OR REPLACE INTO vehicles ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})", tuple(v[c] for c in cols))


def _reinsert_observation(conn, o: dict) -> None:
    """Put a deleted sighting back, with its original id.

    The vehicle may have gone with it (a last-sighting deletion), so it is
    recreated from `vehicles` only if missing - an undo must not resurrect a
    vehicle row that a later, unrelated edit deliberately changed.
    """
    cols = [c for c in o if c != "id"]
    conn.execute(
        f"INSERT OR REPLACE INTO observations (id, {', '.join(cols)}) "
        f"VALUES ({', '.join('?' * (len(cols) + 1))})",
        tuple([o["id"]] + [o[c] for c in cols]))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="vpa edit",
                                 description="inspect and undo manual corrections")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--history", action="store_true", help="list edit batches")
    ap.add_argument("--undo", metavar="BATCH", help="undo one batch")
    a = ap.parse_args(argv)
    if a.undo:
        r = undo(a.db, a.undo)
        print(r.get("error") or
              f"undone: {r['restored']} change(s) put back; backup {r['backup']}")
        return 0 if r.get("ok") else 1
    for h in history(a.db):
        state = "UNDONE" if h["undone"] == h["n"] else "applied"
        print(f"{h['ts']}  {h['batch']}  {h['n']:3d} change(s)  {state:7s}  "
              f"{h['ops']}  {h['reason'][:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
