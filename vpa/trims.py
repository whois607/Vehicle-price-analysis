"""Trim names as the analysis groups them - worked out when the data is READ.

The database keeps every trim exactly as it was imported. This module only
decides which of those stored names are the SAME trim for counting, charts,
the car check and the expected price. Nothing here writes anywhere: a rule
that turns out wrong is fixed by changing this file, and every past car
regroups at once, with nothing to undo in the data.

`add_trim_cols(df)` is applied to the analysis frame in `store._frame_from`,
so Explore, Compare, the car check, the starred list, the workbook and the
phone copy all see one set of names and cannot disagree. Columns:

    variant         the trim the analysis groups by (derived)
    variant_stored  the trim exactly as the database holds it

Anything that WRITES a trim (editing a car, renaming a trim, quick add)
reads and writes the stored name through SQL, never through this frame.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd


def clean(variant) -> str | None:
    """One stored trim name -> the name it is grouped under. No rules yet:
    the derived name is the stored name."""
    if variant is None or (isinstance(variant, float) and pd.isna(variant)):
        return None
    s = str(variant)
    return s if s.strip() else None


def add_trim_cols(df: pd.DataFrame) -> pd.DataFrame:
    """The analysis frame with `variant` derived and `variant_stored` kept.
    Rows, keys, prices and every other column are untouched."""
    df["variant_stored"] = df["variant"] if "variant" in df else None
    if df.empty:
        return df
    stored = df["variant"].astype(object)
    names = {v: clean(v) for v in stored.dropna().unique()}
    df["variant"] = stored.map(lambda v: None if v is None or
                               (isinstance(v, float) and pd.isna(v)) else names[v])
    return df


def resolve(df: pd.DataFrame, make, model, variant):
    """The derived name for a trim someone asked for by name - a derived
    name already, or a stored one from an old link or a saved page. Unknown
    names come back unchanged, so they still match nothing."""
    if not variant or df.empty or "variant_stored" not in df:
        return variant
    line = df
    if make:
        line = line[line["make"] == make]
    if model:
        line = line[line["model"] == model]
    if (line["variant"] == variant).any():
        return variant
    hit = line.loc[line["variant_stored"] == variant, "variant"].dropna()
    if not hit.empty:
        return str(hit.iloc[0])
    return variant


# ------------------------------------------------------------ the record
# A saved record of how every car is grouped, so a change to the rules can be
# checked car by car: which groups merged, which cars moved, and what each
# group's typical asking price was before and after.

def record(df: pd.DataFrame) -> dict:
    """Every car (key) -> [make, model, stored trim, grouped trim], and every
    group -> its number of cars and the median of each car's latest price."""
    cars, groups = {}, {}
    if df.empty:
        return {"made": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cars": cars, "groups": groups}
    last = (df.sort_values(["capture_date", "key"], kind="stable")
            .drop_duplicates("key", keep="last"))
    txt = lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
    for r in last.itertuples(index=False):
        cars[str(r.key)] = [txt(r.make), txt(r.model), txt(r.variant_stored), txt(r.variant)]
    priced = (df.dropna(subset=["price_aud"])
              .sort_values(["capture_date", "key"], kind="stable")
              .drop_duplicates("key", keep="last"))
    for (mk, mo, va), g in priced.groupby(
            [priced["make"], priced["model"], priced["variant"].fillna("(no trim)")]):
        groups[f"{mk} | {mo} | {va}"] = {"cars": int(len(g)),
                                         "median": float(g["price_aud"].median())}
    return {"made": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cars": cars, "groups": groups}


def compare_records(old: dict, new: dict) -> dict:
    """What changed between two records: cars whose make/model/stored trim
    changed (must be none - only the grouping may move), cars that moved
    group (old group -> new group, counted), and groups gone or new."""
    oc, nc = old["cars"], new["cars"]
    data_changed = sorted(k for k in set(oc) & set(nc) if oc[k][:3] != nc[k][:3])
    moved: dict[tuple, int] = {}
    for k in set(oc) & set(nc):
        a, b = oc[k], nc[k]
        if a[3] != b[3]:
            pair = (f"{a[0]} | {a[1]} | {a[3] or '(no trim)'}",
                    f"{b[0]} | {b[1]} | {b[3] or '(no trim)'}")
            moved[pair] = moved.get(pair, 0) + 1
    og, ng = old["groups"], new["groups"]
    return {"cars_old": len(oc), "cars_new": len(nc),
            "cars_missing": sorted(set(oc) - set(nc)),
            "cars_added": sorted(set(nc) - set(oc)),
            "data_changed": data_changed,
            "moved": sorted(([a, b, n] for (a, b), n in moved.items()),
                            key=lambda x: (-x[2], x[0])),
            "groups_gone": sorted(set(og) - set(ng)),
            "groups_new": {g: ng[g] for g in sorted(set(ng) - set(og))},
            "groups_old": len(og), "groups_now": len(ng)}


def cli(db_path, save=None, against=None) -> int:
    """`python -m vpa trims`: how stored trims are grouped, per make; save a
    record, or compare the current grouping with a saved one. Read-only on
    the database."""
    from .store import read_frame
    df = read_frame(db_path)
    rec = record(df)
    if not df.empty:
        print("make: stored trim names -> groups (groups with 8+ cars)")
        for mk, g in df.drop_duplicates("key").groupby("make"):
            st = g.dropna(subset=["variant_stored"])[["model", "variant_stored"]].drop_duplicates()
            gr = g.dropna(subset=["variant"]).groupby(["model", "variant"])["key"].nunique()
            print(f"  {mk}: {len(st)} -> {len(gr)} ({int((gr >= 8).sum())})")
    if save:
        Path(save).write_text(json.dumps(rec, indent=0, sort_keys=True), encoding="utf-8")
        print(f"\nrecord of {len(rec['cars'])} cars in {len(rec['groups'])} groups saved to {save}")
    if against:
        old = json.loads(Path(against).read_text(encoding="utf-8"))
        d = compare_records(old, rec)
        print(f"\nagainst {against} ({old.get('made')}):")
        print(f"  cars {d['cars_old']} -> {d['cars_new']}; groups {d['groups_old']} -> {d['groups_now']}")
        print(f"  cars whose stored make/model/trim differ: {len(d['data_changed'])}")
        print(f"  cars missing: {len(d['cars_missing'])}; new since: {len(d['cars_added'])}")
        print(f"  cars that moved group: {sum(n for _, _, n in d['moved'])}")
        for a, b, n in d["moved"]:
            print(f"    {n:4d}  {a}  ->  {b}")
    return 0
