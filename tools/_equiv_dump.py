"""Dump what one code tree answers on one database - for tools/equiv.py.

    python tools/_equiv_dump.py <code root> <db copy> <out.json> [--all]

Run once with the committed code and once with the working tree, on two
copies of the same data; equiv.py compares the two dumps. Each section is
wrapped, so a function one tree lacks is recorded as an error, not a crash.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

root, db, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
ALL = "--all" in sys.argv
sys.path.insert(0, str(root))
REAL = Path(__file__).resolve().parent.parent          # the live project: its kept pages
res: dict = {}


def section(name):
    def wrap(fn):
        try:
            res[name] = fn()
        except Exception:
            res[name] = {"error": traceback.format_exc(limit=3)}
        print(f"  {name}", flush=True)
        return fn
    return wrap


def jsonable(o):
    return json.loads(json.dumps(o, sort_keys=True, default=lambda v: v.item() if hasattr(v, "item") else str(v)))


def kept_pages():
    folders = [REAL / "data" / "raw"]
    try:
        folders.append(Path(json.loads((REAL / "data" / "inbox.json").read_text(encoding="utf-8"))["folder"]))
    except (OSError, ValueError, KeyError):
        pass
    return sorted({p for f in folders if f.is_dir() for p in f.rglob("*")
                   if p.suffix.lower() in (".htm", ".html") and p.is_file()})


@section("pages: records and keys from every kept page")
def _():
    from vpa.adapters import parse_file
    out = {}
    for p in kept_pages():
        try:
            recs, fmt, n = parse_file(p)
            out[p.name + "|" + str(p.stat().st_size)] = [fmt, n, [[r.dedupe_key(), r.to_row()] for r in recs]]
        except Exception as e:
            out[p.name] = repr(e)
    return jsonable(out)


@section("photos: addresses read from every kept page")
def _():
    from vpa import photos
    return jsonable({p.name + "|" + str(p.stat().st_size):
                     photos.extract(p.read_text(encoding="utf-8", errors="replace")) for p in kept_pages()})


import sqlite3  # noqa: E402
con = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)
VEH = con.execute("SELECT key, source, listing_id, url FROM vehicles ORDER BY key").fetchall()
con.close()


@section("links: advert and archive address of every car")
def _():
    from vpa import links
    return [[k, links.ad_url(u, s), links.archive_url(links.ad_url(u, s))] for k, s, l, u in VEH]


@section("site ids: which listing ids count as the site's own")
def _():
    from vpa import merge
    return [[k, merge._is_site_id(l)] for k, s, l, u in VEH]


@section("duplicates: what the Duplicates page offers")
def _():
    from vpa import dupes
    from vpa.store import Store
    with Store(db) as st:
        g = dupes._groups(st)
    # what the page offers: each group's label and cars, and the refusal counts
    return {k: (sorted(f"{x.get('label')}: {','.join(sorted(map(str, x.get('keys', []))))}" for x in g[k])
                if isinstance(g.get(k), list) else jsonable(g.get(k)))
            for k in ("proven", "review", "dismissed", "refused")}


FRAME = {}


def frame():
    if not FRAME:
        from vpa import compare, explore
        f = explore._frame(db)
        explore._frame = lambda p: f.copy()           # read once, a copy per call
        compare._frame = explore._frame
        FRAME["df"] = f
    return FRAME["df"]


@section("frame: the analysis frame every page reads")
def _():
    df = frame()
    cols = sorted(df.columns)
    return {"cols": cols, "rows": len(df),
            "hash": __import__("hashlib").sha1(df[cols].astype(str).sort_values(cols).to_csv(index=False)
                                               .encode()).hexdigest()}


@section("selections: Explore's answer for each make, model line and trim")
def _():
    from vpa import compare, explore
    df = frame()
    index = compare.selection_index(df)
    items = list(index.items())
    if not ALL:                      # every make and line, and the 40 largest trims
        size = df.groupby(["make", "model", "variant"])["key"].nunique()
        trims = sorted((x for x in items if x[1][3] == "trim"),
                       key=lambda x: -size.get(tuple(x[1][:3]), 0))[:40]
        items = [x for x in items if x[1][3] != "trim"] + trims
    return {label: jsonable(explore.detail_payload(db, mk, mo, va)) for label, (mk, mo, va, lv) in items}


@section("car cards: the car check for cars of the largest trims")
def _():
    from vpa import explore
    df = frame()
    size = df.dropna(subset=["variant"]).groupby(["make", "model", "variant"])["key"].nunique()
    out = {}
    for (mk, mo, va), _n in size.sort_values(ascending=False).head(8 if not ALL else 40).items():
        keys = sorted(df[(df.make == mk) & (df.model == mo) & (df.variant == va)]["key"].unique())
        for k in keys[:15 if not ALL else None]:
            out[k] = jsonable(explore.car_payload(db, mk, mo, va, key=k))
    return out


@section("compare: the Compare catalogue")
def _():
    from vpa import compare
    return jsonable(compare.catalogue_payload(db))


out.write_text(json.dumps(res, sort_keys=True), encoding="utf-8")
