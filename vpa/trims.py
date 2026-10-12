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
    gearbox         'manual' / 'auto', from the stored trim or the title
    drive           'awd' / '4wd' / '2wd' / 'rwd' / 'fwd', likewise
    model_year      the MY code as a year (MY13.5 -> 2013.5), likewise

The wording fixes (`clean`) only ever remove or respell words that do not
name a different car: gearbox, model-year and trailing drive/body words,
and the spelling of "Mk II". Same-trim names that differ only in capitals
or a hyphen ("SportLine"/"Sportline") are then grouped together, shown
under their most common spelling. Spaces are NOT ignored: the only real
case was Corvette "Sting Ray" (1963-67) vs "Stingray" (1968-76) - two eras,
not one spelling.

Anything that WRITES a trim (editing a car, renaming a trim, quick add)
reads and writes the stored name through SQL, never through this frame.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from .adapters.titles import strip_tail

#: Gearbox words removed from ANYWHERE in a trim, not just its end - carsales
#: writes "edition one Auto quattro" with the gearbox before a badge word.
#: Deliberately a narrower list than titles._TRANSMISSION: "man" and "seq"
#: are left alone mid-name.
GEARBOX = {"auto": "auto", "automatic": "auto", "manual": "manual", "cvt": "auto",
           "dct": "auto", "dsg": "auto", "tiptronic": "auto", "steptronic": "auto",
           "multitronic": "auto", "powershift": "auto", "smg": "auto"}
#: Drive words, read into the `drive` column. Only REMOVED from a trim when
#: they trail a gearbox word (strip_tail's rule) - "quattro" and "xDrive"
#: stay in the name, since an A4 and an A4 quattro are different cars.
DRIVE = {"awd": "awd", "quattro": "awd", "xdrive": "awd", "4matic": "awd",
         "4motion": "awd", "allgrip": "awd", "4wd": "4wd", "4x4": "4wd",
         "2wd": "2wd", "4x2": "2wd", "rwd": "rwd", "fwd": "fwd"}
_SPEED = re.compile(r"^\d{1,2}sp$", re.I)
#: MY18, MY13.5, MY22.25, MY24.50
_MY = re.compile(r"^my(\d{2})(?:\.(\d{1,2}))?$", re.I)
_MK = re.compile(r"\bmk\.?\s*(iv|v|i{1,3})\b", re.I)


def clean(variant) -> str | None:
    """One stored trim name -> the name it is grouped under.

        'Auto quattro MY18'               -> 'quattro'
        'edition one Auto quattro MY25'   -> 'edition one quattro'
        '320i F30 Auto MY13.5'            -> '320i F30'
        'XR6 Turbo BA Mk II'              -> 'XR6 Turbo BA MkII'
        'XR8 FG Manual Super Cab'         -> 'XR8 FG'
        'GT FN Auto RWD MY22.25'          -> 'GT FN'
        'Stingray 3LT R Auto MY26'        -> 'Stingray 3LT R'
        'Auto MY86'                       -> None   (no trim: a base car)

    The same steps the importer takes (titles.strip_tail), plus what older
    imports kept: model-year codes anywhere, gearbox words anywhere.
    Applying it twice changes nothing."""
    if variant is None or (isinstance(variant, float) and pd.isna(variant)):
        return None
    s = " ".join(str(variant).split())
    if not s:
        return None
    s = _MK.sub(lambda m: "Mk" + m.group(1).upper(), s)
    toks = [t for t in s.split() if not _MY.match(t)]
    toks = strip_tail(" ".join(toks)).split()
    out: list[str] = []
    after_gear = False
    for t in toks:
        if t.casefold() in GEARBOX or (after_gear and _SPEED.match(t)):
            after_gear = True
            continue
        after_gear = False
        out.append(t)
    s = strip_tail(" ".join(out)) if out else ""
    return s or None


def fold(name: str) -> str:
    """Names that differ only in capitals or a hyphen are one trim."""
    return " ".join(name.casefold().replace("-", "").split())


def _words(*texts) -> list[str]:
    out = []
    for t in texts:
        if isinstance(t, str):
            out.extend(w.strip(",()").casefold() for w in t.split())
    return out


def details(variant, title) -> tuple:
    """(gearbox, drive, model_year) read from the stored trim, then the
    title. None where neither says."""
    words = _words(variant, title)
    gear = next((GEARBOX[w] for w in words if w in GEARBOX), None)
    drive = next((DRIVE[w] for w in words if w in DRIVE), None)
    my = None
    for w in words:
        m = _MY.match(w)
        if m:
            yy = int(m.group(1))
            year = (2000 + yy) if yy <= (datetime.now().year % 100) + 2 else (1900 + yy)
            frac = m.group(2)
            my = year + (int(frac) / (10 ** len(frac)) if frac else 0)
            break
    return gear, drive, my


def add_trim_cols(df: pd.DataFrame) -> pd.DataFrame:
    """The analysis frame with `variant` derived and `variant_stored` kept,
    plus gearbox / drive / model_year. Rows, keys, prices and every other
    column are untouched."""
    df["variant_stored"] = df["variant"] if "variant" in df else None
    for c in ("gearbox", "drive", "model_year"):
        df[c] = None
    if df.empty:
        return df
    stored = df["variant"].astype(object)
    nan = lambda v: v is None or (isinstance(v, float) and pd.isna(v))
    # clean each distinct stored name once; then, within one make + model,
    # names equal but for capitals/hyphens take their most common spelling
    # (by cars; ties alphabetically) so the choice is the same every load
    combos = (df.assign(_v=stored.map(lambda v: None if nan(v) else str(v)))
              .dropna(subset=["_v"]).groupby(["make", "model", "_v"])["key"].nunique())
    spelt: dict[tuple, dict[str, int]] = {}
    cleaned: dict[tuple, str | None] = {}
    for (mk, mo, v), n in combos.items():
        c = clean(v)
        cleaned[(mk, mo, v)] = c
        if c:
            d = spelt.setdefault((mk, mo, fold(c)), {})
            d[c] = d.get(c, 0) + int(n)
    shown = {k: sorted(d.items(), key=lambda x: (-x[1], x[0]))[0][0] for k, d in spelt.items()}
    df["variant"] = [None if nan(v) or not cleaned.get((mk, mo, str(v)))
                     else shown[(mk, mo, fold(cleaned[(mk, mo, str(v))]))]
                     for mk, mo, v in zip(df["make"], df["model"], stored)]
    titles = df["raw_title"] if "raw_title" in df else pd.Series([None] * len(df))
    got = {}
    trip = [got.setdefault((v, t), details(None if nan(v) else v, None if nan(t) else t))
            for v, t in zip(stored, titles)]
    df["gearbox"] = [x[0] for x in trip]
    df["drive"] = [x[1] for x in trip]
    df["model_year"] = [x[2] for x in trip]
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
    # a name never stored, e.g. read from a page just saved: its clean form
    c = clean(variant)
    if c:
        hit = [n for n in line["variant"].dropna().unique() if fold(str(n)) == fold(c)]
        if hit:
            return str(hit[0])
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
