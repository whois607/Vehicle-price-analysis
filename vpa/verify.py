"""Verify — prove the numbers, rather than asking you to trust them.

    python -m vpa verify            (CLI)
    GET /api/verify                 (the import page's "Data check" panel)

Everything else in this project computes; this module *cross-checks*. A stat
that agrees with itself proves nothing, so every check here compares two
independently produced answers, or checks a property that no correct database
can violate:

    sqlite      SQLite's own integrity_check and foreign_key_check
    orphans     observations whose vehicle row is missing (impossible while
                the FK holds - so a non-zero here means the FK was off)
    engine      the same six totals computed twice: once in SQL, once through
                store.frame() + pandas. Catches a dtype, filter or join bug in
                the path every page and report actually uses.
    identities  vehicle keys holding two prices on ONE capture day - two cars
                merged into one identity (see repair.py)
    workbook    output/viewer.xlsx's _Data sheet vs the live database. This is
                the only artefact that cannot recompute itself, so it is the
                only one that can silently show yesterday's numbers.
    values      prices, odometers and capture dates outside what a real used
                listing can be
    duplicates  the same car counted twice: one price, one day, two identities

Read-only: it opens the database and the workbook, and writes to neither.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .store import DEFAULT_DB, Store

#: A used vehicle listed outside this range is a typo or a parse error, not a
#: price. Deliberately wide - the point is to catch $1 and $1,200,000, not to
#: second-guess the market.
PRICE_MIN, PRICE_MAX = 100, 2_000_000
ODO_MAX_KM = 2_000_000
#: capture dates are stamped locally, so a little clock skew is not an error
FUTURE_TOLERANCE_DAYS = 1

_PASS, _WARN, _FAIL, _SKIP = "pass", "warn", "fail", "skip"


def _check(cid, name, status, detail, why="", fix="") -> dict:
    return {"id": cid, "name": name, "status": status, "detail": detail,
            "why": why, "fix": fix}


# --------------------------------------------------------------- the checks
def _check_sqlite(store: Store) -> dict:
    integrity = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk = store.conn.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok":
        return _check("sqlite", "database file integrity", _FAIL,
                      f"SQLite reports: {integrity}",
                      "the file itself is damaged",
                      "restore the newest file from data/backups/")
    if fk:
        return _check("sqlite", "database file integrity", _FAIL,
                      f"{len(fk)} foreign-key violation(s)",
                      "an observation points at a vehicle that is not there")
    return _check("sqlite", "database file integrity", _PASS,
                  "SQLite integrity_check and foreign_key_check both clean")


def _check_orphans(store: Store) -> dict:
    n = store.conn.execute(
        "SELECT COUNT(*) FROM observations o "
        "LEFT JOIN vehicles v ON v.key = o.key WHERE v.key IS NULL").fetchone()[0]
    if n:
        return _check("orphans", "every price belongs to a vehicle", _FAIL,
                      f"{n} observation(s) have no vehicle row",
                      "those prices can never be found by any view")
    return _check("orphans", "every price belongs to a vehicle", _PASS,
                  "no orphaned observations")


def _check_engine(store: Store, df: pd.DataFrame) -> dict:
    """SQL and pandas must agree. They read the same rows by completely
    different paths, so a mismatch means the path the app uses is losing or
    duplicating something."""
    sql = store.conn.execute(
        "SELECT COUNT(*), COUNT(price_aud), "
        "       COALESCE(ROUND(SUM(price_aud), 2), 0), COUNT(DISTINCT key), "
        "       MIN(capture_date), MAX(capture_date) FROM observations").fetchone()
    priced = df["price_aud"].dropna()
    frame = (len(df), int(priced.count()), round(float(priced.sum()), 2),
             int(df["key"].nunique()),
             None if df.empty else str(pd.Timestamp(df["capture_date"].min()).date()),
             None if df.empty else str(pd.Timestamp(df["capture_date"].max()).date()))
    sqlv = (sql[0], sql[1], round(float(sql[2]), 2), sql[3],
            None if not sql[4] else str(sql[4])[:10],
            None if not sql[5] else str(sql[5])[:10])
    names = ("observations", "priced", "price total", "vehicles",
             "first capture", "last capture")
    bad = [f"{nm}: SQL {a} vs frame {b}"
           for nm, a, b in zip(names, sqlv, frame) if a != b]
    if bad:
        return _check("engine", "SQL and the analysis frame agree", _FAIL,
                      "; ".join(bad),
                      "the frame every page and report reads is not the "
                      "database")
    return _check("engine", "SQL and the analysis frame agree", _PASS,
                  f"{sqlv[0]:,} observations, {sqlv[1]:,} priced, "
                  f"total ${sqlv[2]:,.0f}, {sqlv[3]:,} vehicles, "
                  f"{sqlv[4]} to {sqlv[5]} - identical both ways")


def _check_identities(df: pd.DataFrame) -> dict:
    from . import analyze
    bad = analyze.collided_keys(df)
    if bad:
        return _check("identities", "one identity = one vehicle", _WARN,
                      f"{len(bad)} identity(ies) hold two prices on a single "
                      f"capture day",
                      "two different cars were merged into one identity, so "
                      "their price difference is not a price movement",
                      "python -m vpa repair-identities --apply")
    return _check("identities", "one identity = one vehicle", _PASS,
                  "no identity holds two prices on one capture day")


def _read_workbook_data(path: Path) -> dict | None:
    """Totals straight out of the workbook's _Data sheet. Only literal
    columns are read (date, price, identity) - the formula columns would come
    back as formula text without Excel to evaluate them."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if "_Data" not in wb.sheetnames:
            return None
        ws = wb["_Data"]
        rows = n = 0
        total = 0.0
        keys: set = set()
        lo = hi = None
        for r in ws.iter_rows(min_row=3, values_only=True):
            if r is None or all(v is None for v in r):
                continue
            date, price, sel = r[2], r[8], r[18]
            rows += 1
            if isinstance(price, (int, float)):
                n += 1
                total += float(price)
            if isinstance(date, datetime):
                d = date.date().isoformat()
            elif date:
                d = str(date)[:10]
            else:
                d = None
            if d:
                lo = d if lo is None or d < lo else lo
                hi = d if hi is None or d > hi else hi
            keys.add(sel)
        return {"rows": rows, "priced": n, "total": round(total, 2),
                "first": lo, "last": hi, "selections": len(keys)}
    finally:
        wb.close()


def _check_workbook(df: pd.DataFrame, path: Path, stale_hint: bool) -> dict:
    if not path.exists():
        return _check("workbook", "Excel workbook matches the database", _SKIP,
                      "viewer.xlsx has not been built yet",
                      "the workbook is the one view that cannot refresh "
                      "itself",
                      "click 'Excel viewer', or run: python -m vpa viewer")
    try:
        got = _read_workbook_data(path)
    except Exception as e:                       # locked by Excel, corrupt, ...
        return _check("workbook", "Excel workbook matches the database", _SKIP,
                      f"could not read viewer.xlsx ({type(e).__name__})",
                      "it may be open in Excel or mid-sync")
    if got is None:
        return _check("workbook", "Excel workbook matches the database", _WARN,
                      "viewer.xlsx has no _Data sheet",
                      "it was not built by this tool, or was built by a much "
                      "older version",
                      "python -m vpa viewer")

    priced = df["price_aud"].dropna()
    want = {"rows": len(df), "priced": int(priced.count()),
            "total": round(float(priced.sum()), 2),
            "first": None if df.empty else str(pd.Timestamp(df["capture_date"].min()).date()),
            "last": None if df.empty else str(pd.Timestamp(df["capture_date"].max()).date())}
    diffs = [f"{k}: workbook {got[k]} vs database {want[k]}"
             for k in want if got.get(k) != want[k]]
    built = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    if diffs:
        return _check("workbook", "Excel workbook matches the database", _WARN,
                      f"built {built}; " + "; ".join(diffs[:3]),
                      "the workbook was built from different data - its "
                      "charts are showing an older dataset",
                      "click 'Excel viewer', or run: python -m vpa viewer")
    return _check("workbook", "Excel workbook matches the database", _PASS,
                  f"built {built}; all {got['rows']:,} rows, "
                  f"${got['total']:,.0f} of prices and the "
                  f"{got['first']} to {got['last']} range are identical"
                  + (" (rebuild pending by timestamp, but the numbers agree)"
                     if stale_hint else ""))


def _check_values(df: pd.DataFrame) -> dict:
    if df.empty:
        return _check("values", "prices and odometers are plausible", _SKIP,
                      "no data yet")
    p = df["price_aud"].dropna()
    odd_price = int(((p < PRICE_MIN) | (p > PRICE_MAX)).sum())
    o = df["odometer_km"].dropna()
    odd_odo = int(((o < 0) | (o > ODO_MAX_KM)).sum())
    cutoff = pd.Timestamp(datetime.now() + timedelta(days=FUTURE_TOLERANCE_DAYS))
    future = int((df["capture_date"] > cutoff).sum())
    bits = []
    if odd_price:
        bits.append(f"{odd_price} price(s) outside ${PRICE_MIN:,}-${PRICE_MAX:,}")
    if odd_odo:
        bits.append(f"{odd_odo} odometer(s) outside 0-{ODO_MAX_KM:,} km")
    if future:
        bits.append(f"{future} capture date(s) in the future")
    if bits:
        return _check("values", "prices and odometers are plausible", _WARN,
                      "; ".join(bits),
                      "usually a typo or a mis-parsed paste; find them on "
                      "Explore by sorting the price column")
    return _check("values", "prices and odometers are plausible", _PASS,
                  f"all {len(p):,} prices, {len(o):,} odometers and "
                  f"{len(df):,} capture dates are inside sane bounds")


def _check_duplicates(store: Store) -> dict:
    """One car listed once should be counted once.

    NOT the same thing as "one ad id, one identity": v2-era `IMG-` ids are
    derived from image URLs and genuinely repeat across different ads, which
    is why repair.py re-keys them onto the fingerprint. Splitting those is
    correct, so counting them here would flag the fix as the fault.

    The signature of a real double-count is a full duplicate: two different
    identities carrying the SAME price on the SAME day for the same car."""
    same_ad = store.conn.execute("""
        SELECT v.source, v.listing_id, o.capture_date, o.price_aud,
               COUNT(DISTINCT o.key) c
        FROM observations o JOIN vehicles v ON v.key = o.key
        WHERE v.listing_id IS NOT NULL AND v.listing_id <> ''
          AND o.price_aud IS NOT NULL
        GROUP BY v.source, v.listing_id, o.capture_date, o.price_aud
        HAVING c > 1""").fetchall()
    # NOT grouped by capture_date. The first version of this check was, and
    # it under-reported by 6.5x on the real data (26 against 170), because
    # this failure mode arises precisely when the two copies come from
    # DIFFERENT capture sessions - one pasted as text (no ad id, state
    # present), one saved as HTML a week later (ad id present, state absent).
    # Requiring one capture day excluded exactly the case being looked for.
    #
    # state and year are deliberately NOT grouped on either: one copy
    # routinely records what the other does not, so grouping on them splits
    # a real pair. Identity is make+model+variant+price+odometer, and an
    # odometer equal TO THE KILOMETRE a week apart is the discriminator -
    # a car actually being driven would not match.
    same_car = store.conn.execute("""
        SELECT COUNT(DISTINCT o.key) c,
               SUM(CASE WHEN v.listing_id IS NULL OR v.listing_id = ''
                        THEN 1 ELSE 0 END) idless
        FROM observations o JOIN vehicles v ON v.key = o.key
        WHERE o.price_aud IS NOT NULL AND o.odometer_km IS NOT NULL
        GROUP BY v.make, v.model, v.variant, o.odometer_km, o.price_aud
        HAVING c > 1""").fetchall()
    # Splitting by cause, because the two have different fixes: a pair where
    # one copy has no ad id is the dedupe rule's blind spot (an id-bearing and
    # an id-less capture of one car can never collide), while a pair where
    # both carry ids means the site issued two ids for one car.
    mixed = sum(1 for r in same_car if r[1])
    both_ids = len(same_car) - mixed
    # extra COPIES, not groups: three identities of one car is two too many,
    # and the copy count is what actually inflates a median or a supply count
    extra = sum((r[0] or 1) - 1 for r in same_car)
    bits = []
    if same_ad:
        bits.append(f"{len(same_ad)} ad(s) recorded twice on one day at one "
                    f"price under two identities")
    if mixed:
        bits.append(f"{mixed} car(s) captured both with and without an ad id, "
                    f"so the two copies could not be matched")
    if both_ids:
        bits.append(f"{both_ids} car(s) carrying two different ad ids")
    if extra:
        bits.append(f"{extra} surplus identit(ies) in total")
    if bits:
        return _check("duplicates", "no listing is counted twice", _WARN,
                      "; ".join(bits),
                      "a duplicate inflates supply and pulls the median "
                      "towards whichever price was doubled",
                      "run `python -m vpa merge-duplicates` for a dry run - it "
                      "folds together only the groups it can prove are one "
                      "car and reports the rest with the reason. What is left "
                      "after that is deliberate: mostly rounded odometers "
                      "like '150,000 km', where two different cars genuinely "
                      "could share the reading and a price. Capturing the "
                      "same way every time is what prevents new ones.")
    return _check("duplicates", "no listing is counted twice", _PASS,
                  "no two identities share a car, a price and an odometer")


def _check_trims(store: Store) -> dict:
    """One real-world trim should have ONE name.

    A pool is only meaningful if every car that belongs in it is actually in
    it. The way that quietly fails here is not a bad price, it is a bad name:
    the same trim captured two ways lands under two variant strings and
    becomes two smaller, wronger pools that no chart can tell apart.

    Measured on the real database on 04/08/2026, before the parsers shared a
    splitter:

        19 rows  BMW 4 Series  '420i M Sport'
         6 rows  BMW 4 Series  '420i M Sport Auto R'
         3 rows  BMW 4 Series  '420i M Sport F32'

    Two families are reported, and they are different problems:

      * **tail** - names that become identical once the gearbox/drive
        descriptor is removed ('420i M Sport Auto R' vs '420i M Sport').
        These are provably the same trim: `titles.strip_tail` is the same
        function ingestion now uses, so a difference here is a row captured
        before the parsers agreed.
      * **case** - names differing only in capitalisation or spacing.

    Series codes are NOT reported. 'XR6 FG X' and 'XR6 FG MkII' are genuinely
    different cars, and so are '420i M Sport' and '420i M Sport F32' as far as
    this check can prove - F32 is a real chassis generation, not noise. Only
    differences this can PROVE are cosmetic get named.

    Read-only, like every check here.
    """
    from .adapters.titles import strip_tail

    rows = store.conn.execute("""
        SELECT make, model, variant, COUNT(*) n
        FROM vehicles
        WHERE variant IS NOT NULL AND TRIM(variant) <> ''
        GROUP BY make, model, variant""").fetchall()

    tail: dict[tuple, dict] = {}
    case: dict[tuple, dict] = {}
    for make, model, variant, n in rows:
        tail.setdefault((make, model, strip_tail(variant)), {})[variant] = n
        case.setdefault((make, model, " ".join(variant.casefold().split())),
                        {})[variant] = n

    def offenders(groups):
        return [(k, v) for k, v in groups.items() if len(v) > 1]

    tail_bad, case_bad = offenders(tail), offenders(case)
    # a group caught by BOTH is the same finding twice
    case_bad = [g for g in case_bad if g[0][:2] + (g[0][2],) not in
                {k[:2] + (k[2],) for k, _ in tail_bad}]
    if not tail_bad and not case_bad:
        return _check("trims", "one trim, one name", _PASS,
                      f"{len(rows)} named trim(s), none of them two spellings "
                      f"of the same thing")

    bits, examples = [], []
    surplus = 0
    for (mk, mo, base), names in tail_bad + case_bad:
        surplus += sum(names.values()) - max(names.values())
        if len(examples) < 3:
            examples.append(f"{mk} {mo}: " + " / ".join(
                f"{v!r} x{c}" for v, c in sorted(names.items(),
                                                 key=lambda x: -x[1])))
    if tail_bad:
        bits.append(f"{len(tail_bad)} trim(s) stored under names that differ "
                    f"only by a gearbox or drive descriptor")
    if case_bad:
        bits.append(f"{len(case_bad)} differing only in capitalisation")
    bits.append(f"{surplus} vehicle(s) sitting in the smaller spelling")
    return _check("trims", "one trim, one name", _WARN,
                  "; ".join(bits) + " - e.g. " + "; ".join(examples),
                  "a split trim halves both pools, so every median, rank and "
                  "forecast for that car is computed on part of the evidence",
                  "captures made from now on are consistent - both the paste "
                  "and the saved-page parsers share one splitter. Rows already "
                  "stored keep the name they were given; nothing is renamed "
                  "automatically because a rename is a data change. Re-import "
                  "the affected capture, or fix the variant on the Import "
                  "page before committing.")


def _coverage(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    return {
        "observations": int(len(df)),
        "priced_pct": round(float(df["price_aud"].notna().mean() * 100), 1),
        "odometer_pct": round(float(df["odometer_km"].notna().mean() * 100), 1),
        "full_pct": round(float((df["detail_level"] == "full").mean() * 100), 1),
        "capture_days": int(df["capture_date"].dt.strftime("%Y-%m-%d").nunique()),
        "reseen_pct": round(float(
            (df.groupby("key").size() > 1).mean() * 100), 1),
    }


# ------------------------------------------------------------------ payload
def verify_payload(db_path: str | Path = DEFAULT_DB,
                   workbook: Path | None = None) -> dict:
    """Run every check. `ok` is False only when something is actually wrong
    (a FAIL); warnings are surfaced but do not condemn the dataset."""
    from .viewer_xlsx import DEFAULT_OUT
    wb_path = Path(workbook) if workbook else DEFAULT_OUT

    with Store(db_path) as store:
        status = store.status()
        df = store.frame()
        last_ingest = store.conn.execute(
            "SELECT MAX(ts) FROM ingest_log").fetchone()[0]
        checks = [_check_sqlite(store), _check_orphans(store),
                  _check_engine(store, df)]
        checks.append(_check_identities(df))
        stale = False
        if wb_path.exists() and last_ingest:
            try:
                stale = (datetime.fromtimestamp(wb_path.stat().st_mtime)
                         < datetime.fromisoformat(str(last_ingest)))
            except ValueError:
                stale = False
        checks.append(_check_workbook(df, wb_path, stale))
        checks.append(_check_values(df))
        checks.append(_check_duplicates(store))
        checks.append(_check_trims(store))

    counts = {s: sum(1 for c in checks if c["status"] == s)
              for s in (_PASS, _WARN, _FAIL, _SKIP)}
    return {"ok": counts[_FAIL] == 0, "checks": checks, "counts": counts,
            "coverage": _coverage(df), "db": status["db_path"],
            "last_ingest": last_ingest,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def print_report(payload: dict) -> None:
    """ASCII console form, for `python -m vpa verify`."""
    mark = {_PASS: "OK  ", _WARN: "WARN", _FAIL: "FAIL", _SKIP: "--  "}
    print(f"Data check - {payload['checked_at']}")
    print(f"  database: {payload['db']}")
    print(f"  last ingest: {payload['last_ingest'] or 'never'}\n")
    for c in payload["checks"]:
        print(f"  [{mark[c['status']]}] {c['name']}")
        print(f"         {c['detail']}")
        if c["status"] not in (_PASS, _SKIP) and c["why"]:
            print(f"         why it matters: {c['why']}")
        if c["fix"]:
            print(f"         fix: {c['fix']}")
    cov = payload.get("coverage") or {}
    if cov:
        print(f"\n  coverage: {cov['observations']:,} observations over "
              f"{cov['capture_days']} capture days, {cov['priced_pct']}% priced, "
              f"{cov['odometer_pct']}% with an odometer, {cov['full_pct']}% full "
              f"detail, {cov['reseen_pct']}% of vehicles seen more than once")
    n = payload["counts"]
    print(f"\n  {n[_PASS]} passed, {n[_WARN]} warning(s), {n[_FAIL]} failure(s), "
          f"{n[_SKIP]} skipped")
