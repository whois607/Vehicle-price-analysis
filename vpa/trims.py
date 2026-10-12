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
    generation      the generation code (G20 LCI II, FG MkII), from the brand
                    rules in trim_rules.json; None where they cannot tell
    trim_base       the trim with the generation taken out ("330i M Sport")
    body_type       the stored body, or - where none was stored - the body
                    the generation code decides (BMW F36 = hatch); the
                    stored value stays in body_type_stored

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


# ------------------------------------------------------------ brand rules
RULES_PATH = Path(__file__).resolve().parent / "trim_rules.json"
_RULES: dict = {"sig": None, "rules": {}}


def rules() -> dict:
    """trim_rules.json, re-read whenever the file changes."""
    try:
        st = RULES_PATH.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"makes": {}, "shorthand": []}
    if _RULES["sig"] != sig:
        _RULES["rules"] = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        _RULES["sig"] = sig
    return _RULES["rules"]


def make_rules(make) -> dict:
    """One make's rules, following 'same_as'."""
    makes = rules().get("makes", {})
    r, seen = makes.get(make) or {}, set()
    while r.get("same_as") and r["same_as"] not in seen:
        seen.add(r["same_as"])
        r = {**(makes.get(r["same_as"]) or {}), **{k: v for k, v in r.items() if k != "same_as"}}
    return r


def _fits(span, year, slack=1) -> bool:
    """A car with no year cannot rule a code out (the LAST code in a name
    is taken, which is how carsales writes them)."""
    if year is None:
        return True
    lo, hi = span[0], span[1] if span[1] is not None else 9999
    return lo - slack <= year <= hi + slack


def generation_of(make, model, name, year) -> tuple:
    """(generation, trim_base) for one grouped trim name. The generation is
    the LAST code in the name the make's rules recognise - carsales writes
    the trim first ("XT BF MkII") - with its qualifier; a series code
    counts only when the car's year fits its years (Ford's XT and XL are
    trims too) or the car has no year. Nothing recognised: (None, name)."""
    if not isinstance(name, str) or not name:
        return None, None
    R = make_rules(make)
    if not R:
        return None, name
    toks = name.split()
    quals = sorted(R.get("qualifiers", []), key=lambda q: -len(q.split()))
    found = None                                    # (start, end) token span
    i = 0
    while i < len(toks):
        hit = None
        if R.get("series"):
            for code, span in sorted(R["series"].items(), key=lambda kv: -len(kv[0].split())):
                n = len(code.split())
                if " ".join(toks[i:i + n]) == code and _fits(span, year):
                    hit = (i, i + n)
                    break
        if hit is None and R.get("codes") and re.fullmatch(R["codes"], toks[i]):
            hit = (i, i + 1)
        if hit:
            s, e = hit
            for q in quals:
                n = len(q.split())
                if " ".join(toks[e:e + n]) == q:
                    e += n
                    break
            found = (s, e)
            i = e
        else:
            i += 1
    if not found:
        return None, name
    s, e = found
    rest = " ".join(toks[:s] + toks[e:]).strip()
    return " ".join(toks[s:e]), rest or None


def generation_by_year(make, model, name, year) -> str | None:
    """A generation from the make's 'badges' (a pattern in the trim decides
    it: LS460 is always XF40), else its 'years' table. A year two
    generations share - both were on sale - is not guessed: it is named
    with both ("B8/B9"), so those few cars are a group of their own and
    say why."""
    R = make_rules(make)
    for pattern, gen in (R.get("badges") or {}).get(model, []):
        if name and re.search(pattern, name):
            return gen
    if year is None:
        return None
    hits = [g for g, lo, hi in (R.get("years") or {}).get(model, [])
            if lo <= year <= (hi if hi is not None else 9999)]
    return "/".join(hits) if hits else None


def _drop_words(name, words) -> str | None:
    if not name:
        return name
    out = " " + name + " "
    for w in sorted(words, key=len, reverse=True):
        out = re.sub(rf"(?i)\s{re.escape(w)}\s", " ", out)
    return " ".join(out.split()) or None


def place(make, model, name, year) -> tuple:
    """(grouped name, generation, trim without it) for one cleaned name and
    the car's year - the one place the brand rules are applied, for the
    frame and for a single car alike.

    A generation written in the name (codes/series) is kept where it is. One
    that comes from a year table or a badge is ADDED to the name - "quattro"
    becomes "quattro B9", so a B8 and a B9 are no longer one trim - and a car
    with no trim takes it as its name ("C3"). Era words ("Sting Ray" on a C2)
    are dropped in the generations they belong to."""
    if not isinstance(name, str) or not name:
        name = None                                    # pandas hands over NaN
    gen, tb = generation_of(make, model, name, year)
    if gen is None:
        g = generation_by_year(make, model, name, year)
        if g:
            era = ((make_rules(make).get("era_words") or {}).get(model) or {}).get(g, [])
            base = _drop_words(name, era) if era else name
            if base and g in base.split():                 # already written in it
                base = _drop_words(base, [g])
            gen, tb = g, base
            name = f"{base} {g}" if base else g
    return name, gen, tb


def body_of(make, generation) -> str | None:
    """The body a generation code decides, if the make's rules say."""
    if not isinstance(generation, str) or not generation:
        return None
    return (make_rules(make).get("body") or {}).get(generation.split()[0])


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
    for c in ("gearbox", "drive", "model_year", "generation", "trim_base"):
        df[c] = None
    if "body_type" in df:
        df["body_type_stored"] = df["body_type"]
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
    got: dict = {}
    for k in zip(stored, titles):
        if k not in got:
            got[k] = details(None if nan(k[0]) else k[0], None if nan(k[1]) else k[1])
    trip = [got[k] for k in zip(stored, titles)]
    df["gearbox"] = [x[0] for x in trip]
    df["drive"] = [x[1] for x in trip]
    df["model_year"] = [x[2] for x in trip]
    # generation and the trim without it, from the GROUPED name; the model
    # year (MY code) before the listing year, when the car has one
    yr = [None if nan(m) and nan(y) else int(m) if not nan(m) else int(y)
          for m, y in zip(df["model_year"], df["year"] if "year" in df else [None] * len(df))]
    gens: dict = {}
    for k in zip(df["make"], df["model"], df["variant"], yr):
        if k not in gens:
            gens[k] = place(*k)
    trio = [gens[k] for k in zip(df["make"], df["model"], df["variant"], yr)]
    df["variant"] = [t[0] for t in trio]
    df["generation"] = [t[1] for t in trio]
    df["trim_base"] = [t[2] for t in trio]
    if "body_type" in df:
        df["body_type_stored"] = df["body_type"]
        df["body_type"] = [b if not nan(b) else body_of(mk, g)
                           for b, mk, g in zip(df["body_type"], df["make"], df["generation"])]
    return df


def resolve_all(df: pd.DataFrame, make, model, variant) -> list:
    """Every grouped name a trim asked for by name stands for, most cars
    first. A grouped name is itself; a STORED name (an old link, a saved
    comparison) is every group its cars landed in - an old "Audi S5
    quattro" is now "quattro B8", "quattro B9" and "quattro B10"; a name
    never stored is matched by its clean form, generation or not. Unknown:
    [variant], so it still matches nothing."""
    if not variant or df.empty or "variant_stored" not in df:
        return [variant] if variant else []
    line = df
    if make:
        line = line[line["make"] == make]
    if model:
        line = line[line["model"] == model]
    one = line.drop_duplicates("key")
    # cars STORED under the name - exactly, or once cleaned the same way
    # ("Sting Ray Manual MY66" was the "Sting Ray" group before generations)
    c = clean(variant)
    f = fold(c) if c else None
    cf = {v: fold(clean(v)) if clean(v) else None for v in one["variant_stored"].dropna().unique()}
    # a raw stored-style name ("Auto quattro MY24") means exactly the cars
    # stored under it; a clean one ("Sting Ray", from a later link) means
    # every car that cleans to it
    raw = c != " ".join(str(variant).split())
    exact = [isinstance(v, str) and v == variant for v in one["variant_stored"]]
    mask = exact if (raw and any(exact)) else [
        isinstance(v, str) and (v == variant or (f is not None and cf.get(v) == f))
        for v in one["variant_stored"]]
    hit = one.loc[mask, "variant"].dropna()
    if (line["variant"] == variant).any():
        # a group by that name - plus wherever else cars stored under it went
        others = [n for n in hit.astype(str).value_counts().index if n != variant]
        return [variant] + others
    if hit.empty and f is not None:
        # a name never stored: its clean form, with or without a generation
        mask = [(isinstance(b, str) and fold(b) == f) or (isinstance(n, str) and fold(n) == f)
                for b, n in zip(one["trim_base"], one["variant"])]
        hit = one.loc[mask, "variant"].dropna()
    if hit.empty:
        return [variant]
    counts = hit.astype(str).value_counts()
    return sorted(counts.index, key=lambda n: (-counts[n], n))


def resolve(df: pd.DataFrame, make, model, variant):
    """The ONE grouped name for a trim asked for by name: the group most of
    its cars landed in (resolve_all)."""
    got = resolve_all(df, make, model, variant)
    return got[0] if got else variant


# ------------------------------------------------------------ one car
def spellings(df: pd.DataFrame, make, model) -> dict:
    """fold(name) -> the spelling the frame shows (capitals/hyphens) for one
    make + model, so a car not yet stored lands in the group it would join.
    Same choice as add_trim_cols: most cars, ties alphabetical."""
    if df.empty or "variant_stored" not in df:
        return {}
    line = df[(df["make"] == make) & (df["model"] == model)].drop_duplicates("key")
    counts: dict[str, dict[str, int]] = {}
    for v in line["variant_stored"].dropna():
        c = clean(v)
        if c:
            d = counts.setdefault(fold(c), {})
            d[c] = d.get(c, 0) + 1
    return {f: sorted(d.items(), key=lambda x: (-x[1], x[0]))[0][0] for f, d in counts.items()}


def derive_one(df: pd.DataFrame, make, model, variant, year=None, title=None,
               spelt: dict | None = None) -> dict:
    """How ONE car - typically just read from a page, not stored - would be
    grouped: the same steps as add_trim_cols. `spelt`: spellings() for its
    make + model, when checking many cars of one model."""
    c = clean(variant)
    if c:
        sp = spelt if spelt is not None else spellings(df, make, model)
        c = sp.get(fold(c), c)
    gear, drive, my = details(variant, title)
    y = int(my) if my else (int(year) if year not in (None, "") and year == year else None)
    name, gen, tb = place(make, model, c, y)
    return {"variant": name, "generation": gen, "trim_base": tb,
            "gearbox": gear, "drive": drive, "model_year": my}


# ------------------------------------------------------------ coverage
#: An upload is checked against a model only once that model holds this many
#: cars - a new brand or model has no wording to disagree with yet.
WORDING_MIN_MODEL = 20
#: ...and warned about when fewer than this share of its cars land on a trim
#: name already in the data (at least WORDING_MIN_CARS of them checked).
WORDING_SHARE = 0.70
WORDING_MIN_CARS = 5


def _rec(r) -> dict:
    get = (lambda k: r.get(k)) if isinstance(r, dict) else (lambda k: getattr(r, k, None))
    return {"make": get("make"), "model": get("model"), "variant": get("variant"),
            "year": get("year"), "title": get("raw_title") or get("title")}


def wording_check(df: pd.DataFrame, records) -> dict:
    """Do the cars in one upload use trim names the data already knows?
    A new site that words trims differently would otherwise quietly start
    new, one-car groups. Never blocks an import - the names are stored as
    they come and can be grouped later - it only says so."""
    one = df.drop_duplicates("key") if not df.empty else df
    size = one.groupby(["make", "model"]).size() if not one.empty else pd.Series(dtype=int)
    known: dict[tuple, set] = {}
    vocab: dict[tuple, set] = {}
    spelt: dict[tuple, dict] = {}
    checked = recognised = 0
    new_words: dict[str, int] = {}
    unknown: list[str] = []
    for r in map(_rec, records):
        mk, mo = r["make"], r["model"]
        if not mk or not mo or int(size.get((mk, mo), 0)) < WORDING_MIN_MODEL:
            continue
        if (mk, mo) not in known:
            line = one[(one["make"] == mk) & (one["model"] == mo)]
            known[(mk, mo)] = set(line["variant"].dropna())
            vocab[(mk, mo)] = {w.casefold() for v in known[(mk, mo)] for w in str(v).split()}
            spelt[(mk, mo)] = spellings(df, mk, mo)
        d = derive_one(df, mk, mo, r["variant"], r["year"], r["title"], spelt[(mk, mo)])
        if not d["variant"]:
            continue                       # a car with no trim says nothing here
        checked += 1
        if d["variant"] in known[(mk, mo)]:
            recognised += 1
            continue
        unknown.append(f"{mk} {mo} {d['variant']}")
        for w in d["variant"].split():
            if w.casefold() not in vocab[(mk, mo)]:
                new_words[w] = new_words.get(w, 0) + 1
    share = recognised / checked if checked else None
    warn = bool(checked >= WORDING_MIN_CARS and share is not None and share < WORDING_SHARE)
    words = [w for w, _ in sorted(new_words.items(), key=lambda x: (-x[1], x[0]))][:8]
    msg = ""
    if warn:
        msg = (f"only {recognised} of {checked} cars here have a trim name already in "
               f"your data - this page may word trims differently from the pages before"
               + (f" (new words: {', '.join(words)})" if words else "")
               + ". They were stored as they are; the Trims page lists them.")
    return {"checked": checked, "recognised": recognised,
            "share": None if share is None else round(share, 3), "warn": warn,
            "new_words": words, "unknown": unknown[:12], "message": msg}


def coverage(df: pd.DataFrame) -> list[dict]:
    """Per make: cars, trim groups, and - for a make with generation rules -
    how many cars the rules place in a generation."""
    out = []
    if df.empty:
        return out
    one = df.drop_duplicates("key")
    for mk, g in one.groupby("make"):
        R = make_rules(mk)
        has_gen = bool(R.get("codes") or R.get("series") or R.get("years") or R.get("badges"))
        sizes = g.dropna(subset=["variant"]).groupby(["model", "variant"])["key"].nunique()
        out.append({"make": str(mk), "cars": int(len(g)),
                    "with_trim": int(g["variant"].notna().sum()),
                    "groups": int(len(sizes)), "groups_8": int((sizes >= 8).sum()),
                    "rules": bool(R), "generation_rules": has_gen,
                    "with_generation": int(g["generation"].notna().sum()) if has_gen else None})
    return sorted(out, key=lambda r: -r["cars"])


def unmatched(df: pd.DataFrame, limit: int = 200) -> list[dict]:
    """Trim names the rules leave without a generation, for makes that have
    generation rules - most cars first, with how they were stored and a
    title or two, so a rule can be written for them."""
    out = []
    if df.empty:
        return out
    one = df.drop_duplicates("key", keep="last")
    for (mk, mo, va), g in one.groupby(["make", "model", one["variant"].fillna("")]):
        R = make_rules(mk)
        if not (R.get("codes") or R.get("series") or R.get("years") or R.get("badges")):
            continue
        miss = g[g["generation"].isna()]
        if miss.empty:
            continue
        ys = miss["year"].dropna()
        out.append({"make": str(mk), "model": str(mo), "variant": va or None,
                    "cars": int(len(miss)),
                    "years": [int(ys.min()), int(ys.max())] if not ys.empty else None,
                    "stored": sorted({str(s) for s in miss["variant_stored"].dropna()})[:4],
                    "titles": [str(t) for t in miss["raw_title"].dropna().unique()[:2]]})
    return sorted(out, key=lambda r: (-r["cars"], r["make"], r["model"]))[:limit]


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


def cli(db_path, save=None, against=None, unmatched_list=False) -> int:
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
    if unmatched_list:
        um = unmatched(df)
        print(f"\nno generation found ({sum(u['cars'] for u in um)} cars, makes with rules):")
        for u in um[:40]:
            yrs = f"{u['years'][0]}-{u['years'][1]}" if u["years"] else "?"
            print(f"  {u['cars']:4d}  {u['make']} {u['model']} {u['variant'] or '(no trim)'}  [{yrs}]")
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
