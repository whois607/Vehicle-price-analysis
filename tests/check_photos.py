"""Listing photos (vpa/photos.py) and the import paths that record them.

    python -m tests.check_photos      (from vehicle-price-analysis/)

Everything against a throwaway database in tests/synthetic/photos_work -
never data/photos.json, which is checked to be untouched:

  * a search page: each listing's photos come from its OWN schema.org
    object, so a neighbour's photo can never land on it;
  * a details page: its main photo and preloaded gallery only - never the
    "similar listings" further down;
  * only the site's own photo server, in one exact shape, is ever stored
    (another host, a look-alike, a script address: all refused);
  * merging: a newer page's photos first, an older page only adds; the same
    page twice changes nothing; the previous file is kept as .bak;
  * every import path records them - Upload (bulk), the watch folder (the
    same bulk.run), Import with preview (only the rows committed) and the
    command line - and none of them, nor the backfill, changes vpa.db beyond
    the import itself;
  * the laptop's car table, car card and starred list carry them.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "tests" / "synthetic" / "photos_work"
DB = WORK / "data" / "vpa.db"
REAL = ROOT / "data" / "photos.json"
RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def px(kind, pid, size="720,480"):
    return (f"https://examplecars.photos.example/examplecars/cars/{kind}/{pid}.jpg"
            f"?fit=pad&amp;size={size}")


def search_page(cars) -> str:
    """A saved results page as examplecars writes it: one schema.org object per
    listing, the ad id in its url and its photos in `image`."""
    items = []
    for ad, pics, price, odo in cars:
        items.append({"@type": "ListItem", "item": {
            "@type": "Vehicle", "name": "2016 Ford Falcon XR6 FG X Auto", "model": "Falcon",
            "brand": {"@type": "Brand", "name": "Ford"},
            "url": f"/cars/details/2016-ford-falcon-xr6-fg-x-auto/{ad}/?gts={ad}",
            "mileageFromOdometer": {"@type": "QuantitativeValue", "value": str(odo)},
            "offers": {"@type": "Offer", "price": str(price), "priceCurrency": "AUD"},
            "image": [{"@type": "ImageObject", "url": u} for u in pics]}})
    ld = json.dumps({"@context": "https://schema.org", "@type": "ItemList",
                     "itemListElement": items}).replace("&amp;", "&")
    # a photo in the page body next to the wrong listing must not matter
    stray = f'<img src="{px("private", "zzzzstrayneighbour01")}">'
    return (f'<html><head><title>Ford Falcon cars for sale - examplecars.example</title></head><body>'
            f'{stray}<script type="application/ld+json">{ld}</script></body></html>')


def detail_page(ad, main, gallery, similar) -> str:
    ld = json.dumps({"@type": "Car", "name": "2015 Ford Falcon XR6 Turbo FG X Auto",
                     "image": f"https://examplecars.photos.example/examplecars/cars/private/{main}.jpg"})
    pre = "".join(f'<link rel="preload" as="image" href="{px("private", g, "450,300")}"/>'
                  for g in gallery)
    pre += f'<link rel="preload" as="image" href="{px("dealer", "dddddealerpreload01")}"/>'
    sim = "".join(f'<img src="{px("private", s)}">' for s in similar)
    meta = ('"metaData":{"networkId":"%s","make":"Ford","model":"Falcon","badge":"XR6 Turbo",'
            '"state":"NSW","sellertype":"Private","year":"2015","odometermin":"161843",'
            '"price":"28000","bodystyle":"Sedan"}' % ad)
    return (f'<html><head><title>x</title></head><body>{pre}<script>{{{meta}}}</script>'
            f'<script type="application/ld+json">{ld}</script>'
            f'<section>Similar cars {sim}</section></body></html>')


def main() -> int:
    from vpa import photos
    real_before = REAL.stat().st_mtime_ns if REAL.exists() else None
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "data").mkdir(parents=True)

    print("\n== reading pages ==")
    A = ["aaaaaaaaaaaaaaaaaaaa1", "aaaaaaaaaaaaaaaaaaaa2", "aaaaaaaaaaaaaaaaaaaa3"]
    B = ["bbbbbbbbbbbbbbbbbbbb1", "bbbbbbbbbbbbbbbbbbbb2"]
    page = search_page([("EXA-AD-100001", [px("private", a) for a in A], 25000, 150000),
                        ("EXB-AD-200002", [px("dealer", b) for b in B], 31990, 98000),
                        ("EXA-AD-100003", [], 22000, 180000)])
    got = photos.extract(page)
    check("a search page: each listing gets exactly its own photos, in order",
          got == {"EXA-AD-100001": [f"examplecars/cars/private/{a}.jpg" for a in A],
                  "EXB-AD-200002": [f"examplecars/cars/dealer/{b}.jpg" for b in B]},
          json.dumps({k: len(v) for k, v in got.items()}))
    d = photos.extract(detail_page("EXA-AD-20672993", "mmmmmmmmmmmmmmmmmain",
                                   ["ggggggggggggggggggg1", "ggggggggggggggggggg2"],
                                   ["ssssssssssssssssssim1", "ssssssssssssssssssim2"]))
    pics = d.get("EXA-AD-20672993", [])
    check("a details page: main photo and its preloaded gallery, never 'similar listings'",
          pics == ["examplecars/cars/private/mmmmmmmmmmmmmmmmmain.jpg",
                   "examplecars/cars/private/ggggggggggggggggggg1.jpg",
                   "examplecars/cars/private/ggggggggggggggggggg2.jpg"], f"{len(pics)} photos")
    bad = ["https://evil.example.com/examplecars/cars/private/abcdefghijkl.jpg",
           "https://examplecars.photos.example.evil.com/examplecars/cars/private/abcdefghijkl.jpg",
           "https://examplecars.photos.example/othersite/cars/private/abcdefghijkl.jpg",
           "javascript:alert(1)//examplecars.photos.example/examplecars/cars/private/abcdefghijkl.jpg",
           "https://examplecars.photos.example/examplecars/cars/private/ab\"onerror=x.jpg",
           "https://examplecars.photos.example/examplecars/cars/private/short.jpg"]
    good = photos.clean("https://examplecars.photos.example/cars/private/7zwwbadhk89otj6l43lwr4y06.jpg?fit=crop")
    check("only the site's own photo server, in one exact shape",
          [photos.clean(u) for u in bad] == [None] * len(bad)
          and good == "examplecars/cars/private/7zwwbadhk89otj6l43lwr4y06.jpg",
          f"{len(bad)} refused; short form -> {good}")
    check("the address the app shows is built only from a valid stored path",
          photos.photo_url(good, 160, 107) == "https://examplecars.photos.example/examplecars/cars/private/"
          "7zwwbadhk89otj6l43lwr4y06.jpg?fit=pad&size=160,107"
          and photos.photo_url("../../x.jpg") is None, "")

    print("\n== the photo list ==")
    P = lambda *ids: [f"examplecars/cars/private/{i}.jpg" for i in ids]
    n1 = photos.remember(DB, {"EXA-AD-1": P("p" * 12 + "1", "p" * 12 + "2")}, "2026-09-01")
    n2 = photos.remember(DB, {"EXA-AD-1": P("p" * 12 + "1", "p" * 12 + "2")}, "2026-09-01")
    photos.remember(DB, {"exa-ad-1": P("n" * 12 + "1", "p" * 12 + "1")}, "2026-09-10")
    newer = photos.load(DB)["EXA-AD-1"]
    photos.remember(DB, {"EXA-AD-1": P("o" * 12 + "1")}, "2026-08-01")
    older = photos.load(DB)["EXA-AD-1"]
    check("newer page first, older pages only add; the same page twice changes nothing",
          n1 == 1 and n2 == 0
          and newer == P("n" * 12 + "1", "p" * 12 + "1", "p" * 12 + "2")
          and older == newer + P("o" * 12 + "1"), " -> ".join(x.split("/")[-1][:5] for x in older))
    check("written beside its own database, previous version kept as .bak",
          photos.store_path(DB) == WORK / "data" / "photos.json"
          and (WORK / "data" / "photos.json.bak").exists()
          and not (WORK / "data" / "photos.json.lock").exists(), "")
    photos.remember(DB, {"EXA-AD-9": ["https://evil.example.com/x.jpg", "../../etc.jpg"],
                         "not an ad": P("q" * 13)}, "2026-09-01")
    check("nothing but valid addresses under real ad ids is ever stored",
          set(photos.load(DB)) == {"EXA-AD-1"}, ", ".join(sorted(photos.load(DB))))
    # a file Windows will not let us read right now (a writer mid-replace,
    # OneDrive): reads keep the last good list, and a save is refused rather
    # than rewriting the list from nothing
    import msvcrt
    f = WORK / "data" / "photos.json"
    good = f.read_bytes()
    seen_before = photos.load(DB)
    fh = open(f, "r+b")
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, len(good))
    try:
        # a stale cache entry, so load() has to read the locked file
        photos._CACHE[str(f)] = ((0, 0), seen_before)
        during = photos.load(DB)
        try:
            photos.remember(DB, {"EXA-AD-77": P("z" * 13)}, "2026-09-30")
            refused = False
        except OSError:
            refused = True
    finally:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, len(good))
        fh.close()
    check("a moment's file lock hides no photos and loses none",
          during == seen_before and refused and f.read_bytes() == good, "")
    (WORK / "data" / "photos.json").write_text("{not json", encoding="utf-8")
    check("a damaged photo list reads as empty (pictures lost, nothing else)",
          photos.load(DB) == {}, "")
    (WORK / "data" / "photos.json").unlink()

    print("\n== every import path records them ==")
    from vpa import bulk, importui
    from vpa.store import Store
    with Store(DB) as st:
        st.conn.execute("SELECT 1")
    bulk.run([{"name": "page1.htm", "text": page, "capture_date": "2026-09-20"}], DB)
    have = photos.load(DB)
    check("Upload (and the watch folder, which runs the same import)",
          have.get("EXA-AD-100001") == [f"examplecars/cars/private/{a}.jpg" for a in A]
          and "EXB-AD-200002" in have, f"{len(have)} ads")
    page2 = search_page([("EXA-AD-300001", [px("private", "c" * 20)], 19000, 200000),
                         ("EXA-AD-300002", [px("private", "d" * 20)], 21000, 190000)])
    res = importui.parse_payload({"name": "p2.htm", "text": page2}, DB)
    check("Import with preview: previewing stores nothing",
          "EXA-AD-300001" not in photos.load(DB) and res.get("ok"), f"{len(res.get('rows', []))} rows")
    rows = [r for r in res["rows"] if "EXA-AD-300001" in str(r.get("listing_id"))]
    importui.commit_payload({"rows": rows}, DB)
    have = photos.load(DB)
    check("...and committing stores photos for the committed rows only",
          "EXA-AD-300001" in have and "EXA-AD-300002" not in have, "")
    page3 = WORK / "page3.htm"
    page3.write_text(search_page([("EXA-AD-400001", [px("private", "e" * 20)], 15000, 210000)]),
                     encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "vpa", "--db", str(DB), "ingest", str(page3)],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    check("the command line import", "EXA-AD-400001" in photos.load(DB),
          r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:])

    print("\n== the backfill reads kept pages and nothing else ==")
    raw = WORK / "data" / "raw"
    raw.mkdir()
    (raw / "old.htm").write_text(search_page([("EXA-AD-900009", [px("private", "f" * 20)], 22000, 180000)]),
                                 encoding="utf-8")
    inbox = WORK / "inbox" / "imported" / "2026-09-29"
    inbox.mkdir(parents=True)
    (inbox / "102424 x.htm").write_text(detail_page("EXB-AD-200002", "g" * 20, [], []), encoding="utf-8")
    db_hash = hashlib.sha256(DB.read_bytes()).hexdigest()
    rep = photos.backfill(DB)
    have = photos.load(DB)
    check("both kept folders are read; the database is byte-identical afterwards",
          rep["pages"] == 2 and have.get("EXA-AD-900009") == [f"examplecars/cars/private/{'f' * 20}.jpg"]
          and have["EXB-AD-200002"][0] == f"examplecars/cars/private/{'g' * 20}.jpg"
          and "EXA-AD-100003" not in have
          and hashlib.sha256(DB.read_bytes()).hexdigest() == db_hash,
          f"{rep['with_photo']} of {rep['cars']} cars have a photo")
    check("a second backfill changes nothing", photos.backfill(DB)["changed"] == 0, "")

    print("\n== the laptop pages carry them ==")
    from vpa import explore, starred
    d = explore.detail_payload(DB, "Ford", "Falcon", None)
    by = {v["listing_id"]: v.get("ph") for v in d.get("vehicles", [])}
    check("the car table: each car's first photo, none where none is stored",
          by.get("EXA-AD-100001") == f"examplecars/cars/private/{A[0]}.jpg"
          and "EXA-AD-100003" in by and by["EXA-AD-100003"] is None, f"{len(by)} cars")
    with sqlite3.connect(DB) as c:
        key, var = c.execute("SELECT key, variant FROM vehicles WHERE listing_id='EXA-AD-100001'").fetchone()
    card = explore.car_payload(DB, "Ford", "Falcon", var, key=key)
    check("the car card: all of the car's photos; comparables their first",
          card.get("subject", {}).get("photos") == [f"examplecars/cars/private/{a}.jpg" for a in A]
          and all("ph" in c for c in card.get("comparables", [])),
          f"{len(card.get('comparables', []))} comparables")
    starred.star(DB, key)
    w = starred.watchlist_payload(DB)
    check("the starred list", w["cars"] and w["cars"][0].get("ph") == f"examplecars/cars/private/{A[0]}.jpg", "")

    real_after = REAL.stat().st_mtime_ns if REAL.exists() else None
    check("the real data/photos.json was not touched by any of this",
          real_before == real_after, "untouched")

    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
