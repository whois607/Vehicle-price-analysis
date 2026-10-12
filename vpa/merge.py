"""Merge vehicle identities that are provably the same car.

    python -m vpa merge-duplicates            # dry run, shows the plan
    python -m vpa merge-duplicates --apply    # backs up, then merges

THE PROBLEM
`Record.dedupe_key()` uses the site's ad id when there is one and a
fingerprint otherwise. So one car captured twice by different methods - once
pasted as text (no ad id) and once saved as HTML (ad id present) - produces
TWO identities that can never collide. Capture eras made it worse: the same
car carries a `BASE-` id in May, an `IMG-` id in June, an `SSE-AD-` id in
July and none at all from a paste. Measured on the real database: 835
vehicles of which 106 were surplus copies of a car already stored.

MERGING IS THE DANGEROUS DIRECTION
`repair.py` only ever SPLITS, and says why: a wrong split costs a link, a
wrong merge welds two cars' prices into one history and cannot be undone by
inspection. So the bar here is not "probably the same car" - it is a set of
conditions under which two different cars essentially cannot qualify.

THE RULE - every condition must hold, or the group is reported and LEFT ALONE
  1. Same source, make, model and trim AS THE ANALYSIS GROUPS IT (trims.py:
     "quattro" and "Auto quattro MY18" of one model year are one trim -
     since 12/10/2026; before, the stored text had to match exactly), and a
     trim must be recorded: a name the rules built from the year alone
     (a trimless Corvette is "C4") does not count, so a match never rests
     on make, model and year.
  2. Some observation of each shares an EXACT price and an EXACT odometer.
  3. That odometer is not a whole thousand. 150,000 km is what people type
     when they mean "about 150k"; 267,999 km is a reading. Only readings
     count, because two different cars can share a rounded odometer and a
     price, but not a 6-digit one.
  4. Year and state do not conflict (either equal, or one is not recorded -
     one capture method routinely records what the other does not).
  5. Exactly ONE identity in the group carries a real site ad id
     (`SSE-AD-`/`OAG-AD-`). That one survives and the others fold into it,
     so the surviving key stays the stable, site-derived one. `IMG-`/`BASE-`
     ids are image- and era-derived and are not evidence of anything.
  6. Merging must not put two different prices on one capture day - that is
     the very corruption `repair.py` exists to undo.
  7. The combined odometer history must never go BACKWARDS through time. A
     car cannot un-drive kilometres, so this is a physical check that no
     amount of matching metadata can fake. On the real data it held back
     nothing, which is independent corroboration that these are single cars.

SAFETY - the same protocol as repair.py
  * `apply()` takes a `Store.backup()` snapshot BEFORE touching anything.
  * All work happens in ONE transaction. Every invariant is checked before
    commit; any failure raises and rolls back, leaving the file untouched.
  * NO observation is ever deleted, edited or created. Only the `key` column
    that points an observation at its vehicle is rewritten, and surplus
    vehicle rows are removed AFTER their observations have been re-pointed.
    The full multiset of (capture_date, price_aud, price_note, odometer_km)
    is compared before and after and must be identical.
  * Vehicle count must fall by exactly the number of identities folded in.
  * The set of colliding keys must not grow.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .store import DEFAULT_DB, Store

#: an odometer that is a whole multiple of this is treated as approximate
ROUND_KM = 1000
#: ad-id prefixes the sites actually issue; everything else is derived
SITE_ID_PREFIXES = ("SSE-AD-", "OAG-AD-")


def _rows(store: Store) -> list[dict]:
    """Every observation with its vehicle, plus how the analysis groups the
    trim: `group` (the grouped name, generation included) and `named`
    (a real trim is recorded - see rule 1). `variant` stays the stored text."""
    from . import trims
    rows = [dict(r) for r in store.conn.execute("""
        SELECT o.id AS obs_id, o.key, o.capture_date, o.price_aud,
               o.price_note, o.odometer_km,
               v.source, v.listing_id, v.make, v.model, v.variant,
               v.year, v.state, v.first_seen, v.last_seen
        FROM observations o JOIN vehicles v ON v.key = o.key""").fetchall()]
    df = store.frame()
    grouped = {}
    if not df.empty:
        one = df.drop_duplicates("key")
        txt = lambda v: v if isinstance(v, str) and v else None
        for k, mk, mo, st, va, tb, gen in zip(one["key"], one["make"], one["model"],
                                               one["variant_stored"], one["variant"],
                                               one["trim_base"], one["generation"]):
            va, tb, gen = txt(va), txt(tb), txt(gen)
            # a real trim: words beyond the generation, or a generation the
            # stored name itself wrote ("FG X") - not one the year supplied
            written = trims.clean(trims.expand(mk, mo, st)) or ""
            named = bool(va and (tb or (gen and gen in written)))
            grouped[str(k)] = (va, named)
    for r in rows:
        r["group"], r["named"] = grouped.get(str(r["key"]), (r["variant"], bool(r["variant"])))
    return rows


def _is_site_id(listing_id) -> bool:
    return bool(listing_id) and str(listing_id).startswith(SITE_ID_PREFIXES)


def _fingerprint(store: Store) -> list[tuple]:
    """The price data itself, as a sortable multiset. Nothing here may change.

    Every field is stringified before sorting: price_note is NULL on most
    rows and a word on some, and Python refuses to order None against str.
    Comparing the multisets is what matters, not the ordering, so a total
    order that merely EXISTS is enough."""
    return sorted(
        tuple("" if v is None else str(v) for v in row)
        for row in store.conn.execute(
            "SELECT capture_date, price_aud, price_note, odometer_km "
            "FROM observations").fetchall())


def _components(rows: list[dict]) -> tuple[list[list[str]], dict]:
    """Group keys linked by a shared exact (price, odometer) reading.

    Union-find over STRICT edges only, so a chain A-B-C can only form when
    every link in it independently qualifies."""
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["key"]].append(r)

    # Linking is deliberately LOOSE - any shared exact (price, odometer).
    # Every rejection then happens at group level in plan(), so a group that
    # is refused is REPORTED with its reason instead of silently never
    # forming. An earlier version filtered rounded odometers here and the 55
    # groups it dropped were invisible to the user.
    sigs = defaultdict(set)
    for r in rows:
        if r["price_aud"] is None or r["odometer_km"] is None:
            continue
        sigs[(r["source"], r["make"], r["model"], r["group"],
              r["price_aud"], r["odometer_km"])].add(r["key"])

    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # remember WHICH shared reading linked each key, so the plan can show the
    # evidence rather than an unrelated observation of the surviving car
    evidence: dict[str, tuple] = {}
    for sig, keys in sigs.items():
        ks = sorted(keys)
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                union(ks[i], ks[j])
                evidence.setdefault(ks[i], sig)
                evidence.setdefault(ks[j], sig)

    comps = defaultdict(list)
    for k in parent:
        comps[find(k)].append(k)
    return [sorted(v) for v in comps.values() if len(v) > 1], evidence


def _failures(comp: list[str], by_key: dict, evidence: dict) -> list[tuple[str, str]]:
    """Every rule the group breaks, as (code, reason), in the order the rules
    are listed at the top of this file. Empty = provably one car.

    `plan()` reports only the first; the Duplicates page (dupes.py) needs
    them all, because a group is offered for YOUR judgement only when the
    sole thing missing is proof - never when the data contradicts it."""
    out: list[tuple[str, str]] = []
    obs = [r for k in comp for r in by_key[k]]
    sig = evidence.get(comp[0]) or evidence.get(comp[-1])

    # -- 1. a trim must be recorded, or "same car" rests on make+model (and
    # perhaps the generation the year gave it)
    if not all(by_key[k][0]["named"] for k in comp):
        out.append(("no variant", "no trim recorded, so make, model and year "
                    "alone would have to carry the match"))

    # -- 2. the linking odometer must be a reading, not an approximation
    if sig and sig[5] % ROUND_KM == 0:
        out.append(("rounded odometer",
                    f"odometer {sig[5]:,.0f} km is a whole thousand - people "
                    f"type that when they mean 'about', so two different cars "
                    f"could share it along with a price"))

    # -- 3. year and state must not actually conflict
    conflict = ""
    for f in ("year", "state"):
        vals = {by_key[k][0][f] for k in comp if by_key[k][0][f] is not None}
        if len(vals) > 1:
            conflict = f"{f} conflicts: {sorted(str(v) for v in vals)}"
    if conflict:
        out.append((conflict.split(" conflicts")[0] + " conflicts", conflict))

    # -- 4. merging must not put two prices on one capture day
    by_day = defaultdict(set)
    for r in obs:
        if r["price_aud"] is not None:
            by_day[r["capture_date"]].add(r["price_aud"])
    clash = sorted(d for d, p in by_day.items() if len(p) > 1)
    if clash:
        out.append(("two prices on one day",
                    f"merging would put two prices on {clash[0]}"))

    # -- 5. exactly one site ad id, which becomes the surviving key
    winners = [k for k in comp if _is_site_id(by_key[k][0]["listing_id"])]
    if len(winners) != 1:
        out.append(("no single site ad id",
                    f"{len(winners)} identities carry a real site ad id "
                    f"(exactly 1 is required, so the surviving key is the "
                    f"site's own)"))

    # -- 6. the odometer cannot run backwards through time
    seq = sorted((r["capture_date"], r["odometer_km"]) for r in obs
                 if r["odometer_km"] is not None)
    back = [(seq[i - 1], seq[i]) for i in range(1, len(seq))
            if seq[i][1] < seq[i - 1][1]]
    if back:
        (d0, o0), (d1, o1) = back[0]
        out.append(("odometer goes backwards",
                    f"odometer would go backwards: {o0:,.0f} km on {d0} then "
                    f"{o1:,.0f} km on {d1}"))
    return out


def _label(rows: list[dict]) -> str:
    v = rows[0]
    return " ".join(str(x) for x in (v["make"], v["model"], v.get("group") or v["variant"])
                    if x)


def plan(store: Store) -> dict:
    """Pure dry run. Returns the merges that pass EVERY condition, and the
    groups held back with the reason. Touches nothing."""
    rows = _rows(store)
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["key"]].append(r)

    comps, evidence = _components(rows)
    merges, held = [], []
    for comp in comps:
        fails = _failures(comp, by_key, evidence)
        if fails:
            # `code` groups the report; `reason` carries the specifics. An
            # earlier version grouped on the message and printed 55 nearly
            # identical "round odometer NNN,NNN km" lines.
            code, reason = fails[0]
            held.append({"keys": comp, "code": code, "reason": reason,
                         "label": _label(by_key[comp[0]])})
            continue
        obs = [r for k in comp for r in by_key[k]]
        winner = [k for k in comp if _is_site_id(by_key[k][0]["listing_id"])][0]
        v = by_key[winner][0]
        sig = evidence.get(comp[0]) or evidence.get(winner)
        merges.append({
            "winner": winner,
            "absorbed": [k for k in comp if k != winner],
            "listing_id": v["listing_id"],
            "label": _label(by_key[winner]),
            # the shared reading that linked them - the actual evidence
            "price": sig[4] if sig else v["price_aud"],
            "odometer_km": sig[5] if sig else v["odometer_km"],
            "days": sorted({r["capture_date"] for r in obs}),
            "observations": len(obs),
        })

    merges.sort(key=lambda m: (-len(m["absorbed"]), m["label"]))
    return {
        "merges": merges, "held": held,
        "groups": len(merges),
        "absorbed": sum(len(m["absorbed"]) for m in merges),
        "vehicles_before": len(by_key),
        "vehicles_after": len(by_key) - sum(len(m["absorbed"]) for m in merges),
    }


def _apply_merges(store: Store, merges: list[dict], log: dict | None = None) -> dict:
    """Fold each merge's `absorbed` keys into its `winner`, in ONE
    transaction, verifying every invariant before commit. Shared by the
    command-line merge and the Duplicates page so both obey the same
    checks. With `log` ({batch, ts, basis, backup}) each absorbed identity
    is written to merge_log so the merge can be undone exactly."""
    import json
    from . import analyze

    n_absorbed = sum(len(m["absorbed"]) for m in merges)
    before_obs = store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    before_veh = store.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
    before_fp = _fingerprint(store)
    before_bad = analyze.collided_keys(store.frame())
    cols = [r[1] for r in store.conn.execute("PRAGMA table_info(vehicles)")]

    store.conn.isolation_level = None
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        for m in merges:
            for k in m["absorbed"]:
                row = store.conn.execute("SELECT * FROM vehicles WHERE key = ?",
                                         (k,)).fetchone()
                if row is None:
                    raise RuntimeError(f"identity {k} no longer exists")
                ids = [r[0] for r in store.conn.execute(
                    "SELECT id FROM observations WHERE key = ? ORDER BY id", (k,))]
                star = store.conn.execute(
                    "SELECT starred_at, note FROM starred WHERE key = ?", (k,)).fetchone()
                winner_starred = store.conn.execute(
                    "SELECT 1 FROM starred WHERE key = ?", (m["winner"],)).fetchone()
                wwin = store.conn.execute(
                    "SELECT first_seen, last_seen FROM vehicles WHERE key = ?",
                    (m["winner"],)).fetchone()
                if wwin is None:
                    raise RuntimeError(f"identity {m['winner']} no longer exists")
                # ORDER MATTERS: re-point the observations FIRST. The FK
                # is ON DELETE CASCADE, so deleting the vehicle while it
                # still owned rows would delete the price history with it.
                store.conn.execute(
                    "UPDATE observations SET key = ? WHERE key = ?",
                    (m["winner"], k))
                left = store.conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE key = ?",
                    (k,)).fetchone()[0]
                if left:
                    raise RuntimeError(
                        f"{left} observation(s) still point at {k}")
                # A star follows its car. Merging two identities into one
                # must not silently drop the fact that you were watching
                # it; INSERT OR IGNORE because the survivor may already
                # be starred, and the note on the survivor wins.
                store.conn.execute(
                    "INSERT OR IGNORE INTO starred (key, starred_at, note) "
                    "SELECT ?, starred_at, note FROM starred WHERE key = ?",
                    (m["winner"], k))
                store.conn.execute("DELETE FROM starred WHERE key = ?", (k,))
                store.conn.execute("DELETE FROM vehicles WHERE key = ?", (k,))
                if log is not None:
                    store.conn.execute(
                        "INSERT INTO merge_log (batch, ts, winner, absorbed, "
                        "vehicle, obs_ids, star, star_new, winner_first, "
                        "winner_last, basis, backup_path) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (log["batch"], log["ts"], m["winner"], k,
                         json.dumps(dict(zip(cols, tuple(row)))), json.dumps(ids),
                         json.dumps(dict(star)) if star else None,
                         1 if (star and not winner_starred) else 0,
                         wwin[0], wwin[1],
                         m.get("basis") or log.get("basis"), log.get("backup")))
            # the survivor inherits the widest window
            store.conn.execute("""
                UPDATE vehicles SET
                  first_seen = (SELECT MIN(capture_date) FROM observations
                                WHERE key = vehicles.key),
                  last_seen  = (SELECT MAX(capture_date) FROM observations
                                WHERE key = vehicles.key)
                WHERE key = ?""", (m["winner"],))

        after_obs = store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        after_veh = store.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        if after_obs != before_obs:
            raise RuntimeError(
                f"observation count changed: {before_obs} -> {after_obs}")
        if _fingerprint(store) != before_fp:
            raise RuntimeError("the price data itself changed")
        if after_veh != before_veh - n_absorbed:
            raise RuntimeError(
                f"vehicle count wrong: {before_veh} -> {after_veh}, "
                f"expected {before_veh - n_absorbed}")
        orphans = store.conn.execute(
            "SELECT COUNT(*) FROM observations o LEFT JOIN vehicles v "
            "ON v.key = o.key WHERE v.key IS NULL").fetchone()[0]
        if orphans:
            raise RuntimeError(f"{orphans} orphaned observation(s)")
        after_bad = analyze.collided_keys(store.frame())
        if after_bad - before_bad:
            raise RuntimeError(
                f"{len(after_bad - before_bad)} identity(ies) now hold two "
                f"prices on one day - merging created the corruption "
                f"repair.py exists to undo")
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    finally:
        store.conn.isolation_level = ""
    return {"observations": after_obs, "vehicles": after_veh}


def apply(db_path: str | Path = DEFAULT_DB) -> dict:
    """Take a backup, merge in one transaction, verify, commit or roll back."""
    from datetime import datetime
    import uuid

    with Store(db_path) as store:
        backup = store.backup()
        p = plan(store)
        if not p["merges"]:
            return {**p, "applied": False, "backup": str(backup),
                    "note": "nothing met every condition - nothing was touched"}
        done = _apply_merges(store, p["merges"], log={
            "batch": uuid.uuid4().hex[:12],
            "ts": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "basis": "proven", "backup": str(backup)})
        return {**p, "applied": True, "backup": str(backup), **done}


def print_plan(p: dict, applied: bool = False) -> None:
    """ASCII console form."""
    verb = "Merged" if applied else "Would merge"
    print(f"{verb} {p['groups']} group(s), folding in {p['absorbed']} surplus "
          f"identity(ies)")
    print(f"  vehicles {p['vehicles_before']} -> {p['vehicles_after']}"
          f"   (no observation is added, edited or deleted)")
    if p.get("backup"):
        print(f"  backup: {p['backup']}")
    sizes: dict[int, int] = {}
    for m in p["merges"]:
        n = len(m["absorbed"]) + 1
        sizes[n] = sizes.get(n, 0) + 1
    if sizes:
        print("  group sizes: " + ", ".join(
            f"{c} x {n} identities" for n, c in sorted(sizes.items())))
    for m in p["merges"][:8]:
        print(f"\n  {m['label']}  - all seen at ${m['price']:,.0f} and exactly "
              f"{m['odometer_km']:,.0f} km")
        print(f"    keep    {m['winner'][:12]}  {m['listing_id']}")
        for k in m["absorbed"]:
            print(f"    fold in {k[:12]}")
        print(f"    -> one car, {m['observations']} sightings over "
              f"{len(m['days'])} capture day(s): {m['days'][0]} to {m['days'][-1]}")
    if len(p["merges"]) > 8:
        print(f"\n  ... and {len(p['merges']) - 8} more")
    if p["held"]:
        print(f"\n  HELD BACK - not confident, left exactly as they are "
              f"({len(p['held'])}):")
        counts: dict[str, int] = {}
        example: dict[str, str] = {}
        for h in p["held"]:
            code = h.get("code", "other")
            counts[code] = counts.get(code, 0) + 1
            example.setdefault(code, h["reason"])
        for code, n in sorted(counts.items(), key=lambda t: -t[1]):
            print(f"    {n:>3}  {code}")
            print(f"         e.g. {example[code]}")
    if not applied:
        print("\n  This was a DRY RUN. Nothing was changed. Re-run with "
              "--apply to perform it.")
