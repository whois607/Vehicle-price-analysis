"""The phone app's numbers are the laptop's numbers.

    python -m tests.check_phone_app      (from vehicle-price-analysis/)

The phone shows the laptop's own Explore / Compare / Overview results
(phonebuild runs the page code for every selection), and adds only counting
over the sightings it carries - car lists, ranks, comparables, the car card,
filtered stats, link matching (vpa/phone_app/stats.js). This suite builds a
deliberately awkward database - missing odometers, price-only quick adds,
Facebook cars with no link, a car with two prices on one day, duplicate
identities and a group marked "not duplicates", starred cars - builds the
phone copy from it in a separate process, then runs stats.js under node
against the Python it ports, on:

  * every make, model line and trim, unfiltered and with year / price /
    keyword filters: headline, splits, capture days, supply, monthly
    series, histogram, scatter, the car list, price movers, standouts;
  * the car card for every car of several trims, and for typed-in cars;
  * advert links (ids, addresses, Facebook, junk), typed values, Python's
    rounding, and compare ranks;
  * every screen of the app itself (app.js, against a stand-in page) for
    every selection, filtered and not, every car, price checks and compare:
    none may throw or print NaN / undefined.

Also checks the build itself: the real-database copy is taken read-only and
the fixture database is byte-identical afterwards.

    python -m tests.check_phone_app --db data/vpa.db

runs the same comparison on a COPY of another database (taken through a
read-only connection, so the original is never opened for writing) - the
real data instead of the fixture. Car cards are then sampled: every car of
the 20 largest trims, up to 60 a trim. Needs node; without it the
suite says so and fails (it cannot vouch for the phone without running it).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "phone_app_work"
DB = WORK / "app_test.db"
OUT = WORK / "phone_out"
STATS = ROOT.parent / "vpa" / "phone_app" / "stats.js"
RUNNER = ROOT / "phone_js_check.js"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


LINES = {("Ford", "Falcon"): ["XR6", "XR8", "G6E"], ("Holden", "Commodore"): ["SS", "SV6"],
         ("Toyota", "Hilux"): ["SR5"], ("Mazda", "CX-5"): ["Maxx"]}
DAYS = ["2026-05-14", "2026-06-02", "2026-06-20", "2026-07-11", "2026-08-01", "2026-08-22"]


def seed() -> None:
    rnd = random.Random(7)
    recs = []
    n = 0
    for (mk, mo), trims in LINES.items():
        for va in trims + [None]:
            base = rnd.randint(9000, 60000)
            for i in range(rnd.randint(6, 46)):
                n += 1
                lid = f"SSE-AD-{100000 + n}"
                year = rnd.choice([2008, 2010, 2012, 2014, 2016, None if i % 17 == 0 else 2018])
                odo = None if i % 9 == 0 else rnd.randint(30, 320) * 1000 + rnd.choice([0, 0, 137])
                state = rnd.choice(["NSW", "VIC", "QLD", None])
                seller = rnd.choice(["dealer", "private", None])
                price = base + rnd.randint(-8000, 8000) - (odo or 150000) // 40
                days = sorted(rnd.sample(DAYS, rnd.randint(1, 5)))
                for j, d in enumerate(days):
                    p = price - (j * rnd.choice([0, 0, 500, 1250]) if j else 0)
                    recs.append(make_record({
                        "listing_id": lid, "make": mk, "model": mo, "variant": va,
                        "year": year, "price": p, "odometer": odo, "state": state,
                        "seller_type": seller, "url": f"/cars/details/x-{mo.lower()}/{lid}/",
                        # a gearbox in two titles of three, so the car check's
                        # same-gearbox split is compared too (trims.py)
                        "title": (f"{year or ''} {mk} {mo} {va or ''}".strip()
                                  + ("", " Manual", " Auto")[i % 3]),
                        "capture_date": d}, source="carsales"))
    # thin trims: their cards walk the ladder - a 5-car Sprint to every FG X
    # Falcon (generation), a 3-car 428i F36 to every 428i of 2013-2017
    # (trim, any generation) - and a 5-car XR6 Sprint with no code to the
    # model's nearby years
    thin = [("Ford", "Falcon", "XR6 Sprint FG X", 2016, 5), ("Ford", "Falcon", "XR6 FG X", 2015, 6),
            ("Ford", "Falcon", "XR8 FG X", 2015, 5), ("BMW", "4 Series", "428i Sport Line F36", 2015, 3),
            ("BMW", "4 Series", "428i Sport Line F32", 2016, 7), ("Ford", "Falcon", "XR6 Sprint", 2013, 5)]
    m = 0
    for mk, mo, va, yr, count in thin:
        for i in range(count):
            m += 1
            recs.append(make_record({"listing_id": f"SSE-AD-998{m:03d}", "make": mk, "model": mo,
                                     "variant": va, "year": yr - (i % 2),
                                     "price": 32000 + m * 900, "odometer": 30000 + m * 4100,
                                     "url": f"/cars/details/z/SSE-AD-998{m:03d}/",
                                     "title": f"{yr} {mk} {mo} {va} Manual",
                                     "capture_date": DAYS[m % len(DAYS)]}, source="carsales"))
    # a car with two prices on one day (two cars merged into one identity)
    for p in (21000, 23500):
        recs.append(make_record({"listing_id": "SSE-AD-999001", "make": "Ford", "model": "Falcon",
                                 "variant": "XR6", "year": 2012, "price": p, "odometer": 150000,
                                 "url": "/cars/details/x/SSE-AD-999001/",
                                 "capture_date": DAYS[3]}, source="carsales"))
    # duplicate identities: same price and odometer under two ad ids
    for lid in ("SSE-AD-999101", "SSE-AD-999102", "SSE-AD-999103", "SSE-AD-999104"):
        recs.append(make_record({"listing_id": lid, "make": "Ford", "model": "Falcon",
                                 "variant": "XR8", "year": 2014,
                                 "price": 33990 if lid < "SSE-AD-999103" else 28500,
                                 "odometer": 98000 if lid < "SSE-AD-999103" else 120000,
                                 "url": f"/cars/details/y/{lid}/", "capture_date": DAYS[4]},
                                source="carsales"))
    # Facebook cars (no link, no id) and price-only quick adds
    for i in range(5):
        recs.append(make_record({"make": "Ford", "model": "Falcon", "variant": "XR6",
                                 "year": 2011, "price": 15000 + i * 900, "state": "NSW",
                                 "title": f"Ford Falcon XR6 fb {i}",
                                 "capture_date": DAYS[2]}, source="facebook"))
    for i in range(4):
        recs.append(make_record({"make": "Ford", "model": "Falcon", "price": 18000 + i * 1100,
                                 "capture_date": DAYS[5]}, source="quick_add"))
    with Store(DB) as st:
        st.ingest(recs, source_file="(fixture)", adapter="test")
        keys = [r[0] for r in st.conn.execute(
            "SELECT key FROM vehicles WHERE listing_id IN ('SSE-AD-999103','SSE-AD-999104') ORDER BY key")]
        st.conn.execute("INSERT INTO not_duplicates (keys, ts) VALUES (?, '2026-08-23 10:00:00')",
                        (",".join(sorted(keys)),))
        for k in [r[0] for r in st.conn.execute("SELECT key FROM vehicles ORDER BY key LIMIT 3")]:
            st.conn.execute("INSERT INTO starred (key, starred_at) VALUES (?, '2026-08-23 10:00:00')", (k,))
        st.conn.commit()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    source = Path(argv[argv.index("--db") + 1]) if "--db" in argv else None
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    if source:
        from vpa.store import connect_readonly
        src = connect_readonly(source)
        dst = sqlite3.connect(DB)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"  comparing on a read-only copy of {source}")
        ph = source.resolve().parent / "photos.json"
        if ph.exists():                     # the photo list is read, never written
            (WORK / "photos.json").write_bytes(ph.read_bytes())
    else:
        seed()
        # photos for every other car that has an ad id, one to four each
        from vpa import photos
        with sqlite3.connect(DB) as c:
            ads = sorted({photos.ad_id(l, u) for l, u in c.execute(
                "SELECT listing_id, url FROM vehicles")} - {None})
        photos.remember(DB, {ad: [f"carsales/cars/private/{ad.lower().replace('-', '')}{j}x0000.jpg"
                                  for j in range(1 + i % 4)] for i, ad in enumerate(ads) if i % 2 == 0},
                        "2026-08-20")
    before = hashlib.sha256(DB.read_bytes()).hexdigest()

    print("\n== building the phone copy (separate process) ==")
    r = subprocess.run([sys.executable, "-m", "vpa", "--db", str(DB), "phone-copy", "--out", str(OUT)],
                       cwd=str(ROOT.parent), capture_output=True, text=True, encoding="utf-8")
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8")) if (OUT / "meta.json").exists() else {}
    check("the copy builds, every selection through the laptop's page code",
          r.returncode == 0 and meta.get("counts", {}).get("selections", 0) > 10,
          (r.stdout.strip().splitlines() or [r.stderr.strip()[-200:]])[-1])
    check("the database it was built from is byte-identical afterwards",
          hashlib.sha256(DB.read_bytes()).hexdigest() == before, before[:16])
    check("no private work copy is left behind",
          not list(OUT.glob("work-*")) and not (OUT / "build.lock").exists(), "none")
    snap = json.loads(gzip.decompress((OUT / meta["file"]).read_bytes()))
    from vpa import photos
    cov = photos.coverage(DB)
    ph_i = snap["vehicles"]["cols"].index("ph")
    with_ph = sum(1 for r in snap["vehicles"]["rows"] if r[ph_i])
    check("the copy carries each car's photo addresses, and only valid ones",
          with_ph == cov["with_photo"] and all(
              photos.PATH.match(p) for r in snap["vehicles"]["rows"] for p in (r[ph_i] or [])),
          f"{with_ph} of {len(snap['vehicles']['rows'])} cars")
    (WORK / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")

    print("\n== the laptop's answers ==")
    from vpa import compare, explore
    df = explore._frame(DB)
    index = compare.selection_index(df)
    details = []
    for label, (mk, mo, va, lv) in index.items():
        details.append({"label": label, "mk": mk, "mo": mo, "va": va, "filters": None,
                        "payload": explore.detail_payload(DB, mk, mo, va)})
    for label in ("Ford Falcon", "Ford Falcon XR6", "Holden Commodore", "Ford"):
        if label not in index:
            continue
        mk, mo, va, lv = index[label]
        for f in ({"year_min": 2010, "year_max": 2014}, {"price_max": "30,000"},
                  {"price_min": 20000}, {"keyword": "xr"}, {"year_max": 2012, "price_min": 15000}):
            p = explore.detail_payload(DB, mk, mo, va, **f)
            if not p.get("empty"):
                details.append({"label": label, "mk": mk, "mo": mo, "va": va, "filters": f, "payload": p})
    cards = []
    trims = [(label, sel) for label, sel in index.items() if sel[3] == "trim"]
    if source:                      # real data: the 20 largest trims, sampled
        size = df.groupby(["make", "model", "variant"])["key"].nunique()
        trims = sorted(trims, key=lambda t: -size.get((t[1][0], t[1][1], t[1][2]), 0))[:20]
    for label, (mk, mo, va, lv) in trims:
        pool = df[(df["make"] == mk) & (df["model"] == mo) & (df["variant"] == va)]
        for key in sorted(pool["key"].unique())[:60 if source else None]:
            p = explore.car_payload(DB, mk, mo, va, key=key)
            cards.append({"mk": mk, "mo": mo, "va": va, "opts": {"key": key}, "payload": p})
        for typed in ({"price": "$18,990", "odometer": "98k", "year": "2012", "seller": "Private seller",
                       "state": "New South Wales"},
                      {"price": "25000", "odometer": "", "year": "2014"},
                      {"price": "12.5k", "odometer": "400,000 km"},
                      {"price": "31000 drive away", "odometer": "61000", "state": "Penrith NSW 2750",
                       "seller": "dealer"},
                      {"price": "negotiable"},
                      # no price: the expected range on its own, on each basis
                      {"odometer": "120000"}, {"odometer": "900,000 km"}, {"odometer": "3000"},
                      {"year": "2013", "seller": "dealer"}, {"odometer": "75k", "state": "VIC"}):
            p = explore.car_payload(DB, mk, mo, va, **typed)
            cards.append({"mk": mk, "mo": mo, "va": va, "opts": typed, "payload": p})
    # a model with no trim picked: stored cars captured without a trim, and
    # typed cars with and without a year (explore.CAR_NEAR_YEARS fallback)
    for label, (mk, mo, va, lv) in index.items():
        if lv != "line":
            continue
        line = df[(df["make"] == mk) & (df["model"] == mo)]
        for key in sorted(line[line["variant"].isna()]["key"].unique())[:12 if source else None]:
            cards.append({"mk": mk, "mo": mo, "va": None, "opts": {"key": key},
                          "payload": explore.car_payload(DB, mk, mo, None, key=key)})
        for typed in ({"price": "20000", "year": "2012"}, {"odometer": "100000", "year": "2014"},
                      {"price": "20000"}, {"year": "1990", "price": "5000"}):
            cards.append({"mk": mk, "mo": mo, "va": None, "opts": typed,
                          "payload": explore.car_payload(DB, mk, mo, None, **typed)})
    links = []
    with sqlite3.connect(DB) as c:
        ids = [r[0] for r in c.execute("SELECT listing_id FROM vehicles WHERE listing_id IS NOT NULL LIMIT 6")]
    for lid in ids[:3]:
        links.append(f"https://www.carsales.com.au/cars/details/whatever/{lid}/?gts={lid}&x=1")
    links += [f"carsales.com.au/cars/details/x-falcon/{ids[3]}", ids[4],
              "https://www.carsales.com.au/cars/details/new-car/SSE-AD-1234567/",
              "https://www.facebook.com/marketplace/item/123", "hello there", "https://example.com/x",
              "https://www.carsales.com.au/cars/details/x-hilux/SSE-AD-100200/"]
    link_exp = [{"link": l, "payload": explore.link_payload(DB, l)} for l in links]
    from vpa.schema import norm_seller, norm_state, norm_year, parse_odometer, parse_price
    strings = ["$18,990", "18990", "18,990 Drive Away", "Negotiable", "$12.5k", "POA", "45,000 km",
               "45k", "98K", "n/a", "2012", "1899", "New South Wales", "Penrith, NSW", "vic",
               "Dealer used", "Private seller", "$1,250,000", "50", "ono 9500", "excl. gov charges 22000",
               "12 345", ""]
    parse = [{"s": s, "want": [list(parse_price(s)), parse_odometer(s), norm_state(s),
                               norm_seller(s), norm_year(s)]} for s in strings]
    rounding = [[x, nd, round(x, nd)] for x, nd in
                [(2.5, 0), (3.5, 0), (-2.5, 0), (12.5, 0), (0.125, 2), (0.375, 2), (2.675, 2),
                 (1.005, 2), (1234.565, 2), (49.95, 1), (0.05, 1), (99.995, 2), (1e6 + 0.5, 0)]]
    cmp_sets = [["Ford Falcon", "Holden Commodore", "Toyota Hilux"],
                ["Ford Falcon XR6", "Ford Falcon XR8", "Ford Falcon G6E", "Mazda CX-5 Maxx"]]
    compare_exp = []
    for labels in cmp_sets:
        cp = compare.compare_payload(DB, labels)
        compare_exp.append({"labels": labels, "want": [
            {k: c.get(k) for k in ("label", "rank", "vs_cheapest", "median")} for c in cp["columns"]]})

    def plain(o):
        return json.loads(json.dumps(o, default=lambda v: v.item() if hasattr(v, "item") else str(v)))
    exp = {"now": datetime.now().isoformat(timespec="seconds"), "details": plain(details),
           "cards": plain(cards), "links": plain(link_exp), "parse": parse,
           "rounding": rounding, "compare": plain(compare_exp)}
    (WORK / "expect.json").write_text(json.dumps(exp), encoding="utf-8")
    print(f"  {len(details)} selection views, {len(cards)} car cards, {len(links)} links")

    print("\n== the phone's answers (stats.js under node) ==")
    node = shutil.which("node")
    if not node:
        check("node is installed to run the phone's code", False, "node not found on PATH")
        return 1
    r = subprocess.run([node, str(RUNNER), str(STATS), str(WORK / "snapshot.json"),
                        str(WORK / "expect.json")], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        check("stats.js runs", False, r.stderr.strip()[-400:])
        return 1
    res = json.loads(r.stdout.strip().splitlines()[-1])
    for cat, v in res.items():
        name = ("every screen renders cleanly (no errors, NaN or undefined)"
                if cat == "screens render" else f"{cat} match the laptop")
        check(name, v["bad_total"] == 0,
              f"{v['n'] - v['bad_total']} of {v['n']}" + (" | " + " | ".join(v["bad"][:3]) if v["bad"] else ""))

    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
