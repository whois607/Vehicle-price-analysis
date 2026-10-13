"""Listing details (vpa/details.py): what saved pages state beyond the price,
and the filters built on them.

    python -m tests.check_details      (from vehicle-price-analysis/)

Everything against a throwaway database in tests/synthetic/details_work -
never data/details.json, which is checked to be untouched:

  * a results page: each car's condition, engine, gearbox, seller, state,
    price type, rating and flags come from ITS OWN tile and analytics; a
    tile part that names two ads attributes nothing, and analytics copies
    that disagree give no value;
  * a details page: drive, doors, seats, engine size, colour;
  * the file: newer pages overwrite, older ones only fill blanks; sightings
    kept per day; .bak kept; a damaged file costs only the extras;
  * the frame: new columns, and only BLANK stored state / seller / gearbox
    filled - a stored value always wins; no file = blank columns;
  * Explore's filters: condition groups, "not stated", every filter in a
    fixed order, and the options offered with their counts;
  * every import path records them, and vpa.db is byte-identical across
    every read, backfill and frame of them;
  * searches: the page's own result count when its text states none, and
    the sort order each search was saved in.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "tests" / "synthetic" / "details_work"
DB = WORK / "data" / "vpa.db"
REAL = ROOT / "data" / "details.json"
RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tile(ad, engine, gear, seller, price_info, badge=None, gallery=None, other_ad=None):
    """One listing tile as the page's own data draws it."""
    t = {"type": "ListingCard", "id": f"tile-{ad}",
         "action": {"type": "NavigateAction", "data": {"url": f"/cars/details/x/{ad}/"}},
         "keyDetails": {"type": "Stack", "children": [
             {"type": "Stack", "children": [{"type": "Icon", "source": "body-type", "title": "Sedan"},
                                             {"type": "Text", "value": "Sedan"}]},
             {"type": "Stack", "children": [{"type": "Icon", "source": "engine", "title": engine},
                                             {"type": "Text", "value": engine}]},
             {"type": "Stack", "children": [{"type": "Icon", "source": "transmission", "title": gear},
                                             {"type": "Text", "value": gear}]}]},
         "keyDetailsTexts": ["Sedan", engine, gear, "150,000 km"],
         "priceInfo": {"type": "Link", "value": price_info,
                       "action": {"data": {"url": f"/price-guide/{other_ad or ad}"}}},
         "sellerTitle": {"type": "Stack", "children": [{"type": "Text", "value": seller}]},
         "galleryBadge": {"type": "GalleryBadge", "label": gallery or ""}}
    if badge:
        t["priceBadge"] = {"type": "Tooltip", "child": {"type": "Badge", "label": badge},
                           "content": {"children": [{"type": "Text", "value": "Read more"}]}}
    return t


def analytics(ad, **kv):
    body = {"eventCategory": "listing", "networkId": ad, **kv}
    return json.dumps(body, separators=(",", ":"))      # as pages write it


def results_page(cars, tiles, extra_js="", title="Ford Falcon cars for sale - examplecars.example",
                 visible_total=True, total=4):
    items = [{"@type": "ListItem", "item": {
        "@type": "Vehicle", "name": name, "model": "Falcon",
        "brand": {"@type": "Brand", "name": "Ford"},
        "url": f"/cars/details/x/{ad}/?gts={ad}",
        "mileageFromOdometer": {"@type": "QuantitativeValue", "value": "150000"},
        "offers": {"@type": "Offer", "price": str(price), "priceCurrency": "AUD"}}}
        for ad, name, price in cars]
    ld = json.dumps({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": items})
    data = json.dumps({"props": {"pageProps": {"root": {"type": "Stack", "children": tiles}}}})
    shown = f"<h1>{total} Ford Falcon cars for sale</h1>" if visible_total else "<h1>Ford Falcon</h1>"
    return (f"<html><head><title>{title}</title></head><body>{shown}"
            f'<script type="application/ld+json">{ld}</script>'
            f'<script id="__NEXT_DATA__" type="application/json">{data}</script>'
            f"<script>window.track=[{extra_js}];</script></body></html>")


A1, A2, A3, A4, A5 = "EXA-AD-500001", "EXA-AD-500002", "EXA-AD-500003", "EXA-AD-500004", "EXA-AD-500005"
CARS = [(A1, "2016 Ford Falcon XR6 FG X", 25000), (A2, "2014 Ford Falcon XR8 FG Auto", 31990),
        (A3, "2026 Ford Falcon GT", 89000), (A4, "2015 Ford Falcon XR6 FG X", 22000)]
TILES = [tile(A1, "6cyl 4.0L T Petrol", "Automatic", "Private • QLD", "Excl. Govt. Charges",
              "GREAT PRICE", "Just listed"),
         tile(A2, "8cyl 5.0L S Petrol", "Manual", "Dealer demo • NSW", "Drive away",
              "Below market price"),
         tile(A3, "8cyl 5.0L S Petrol", "Automatic", "New car to order", "Indicative drive away"),
         # a tile part naming a SECOND ad: nothing in it may be attributed
         tile(A4, "4cyl 2.0L T Diesel", "Manual", "Private • WA", "Drive away", other_ad=A5)]
JS = ",".join([
    analytics(A1, adtype="Private", type="Used", sellerId="S-11", state="VIC",
              sortby="Price (Low to High)", listingresultcount="4", result_type="organic"),
    # a second copy disagreeing on the seller id: the id is left out
    analytics(A1, adtype="Private", type="Used", sellerId="S-99", sortby="Price (Low to High)",
              listingresultcount="4"),
    analytics(A2, adtype="Dealer Demo", type="Demo", sellerId="S-22", sortby="Price (Low to High)",
              listingresultcount="4"),
    analytics(A3, adtype="New car to order", type="Generic", sellerId="S-33", result_type="suggested",
              suggestedresult=True, sortby="Price (Low to High)", listingresultcount="4"),
    analytics(A4, adtype="Private", type="Used", state="WA", sortby="Price (Low to High)",
              listingresultcount="4")])


def detail_page(ad):
    ld = json.dumps({"@type": "Car", "name": "2015 Ford Falcon XR6 Turbo FG X Auto",
                     "fuelType": "Petrol - Unleaded ULP", "vehicleTransmission": "Automatic",
                     "driveWheelConfiguration": "RWD", "numberOfDoors": "4", "seatingCapacity": "5",
                     "vehicleEngine": {"engineDisplacement": {"value": 3984, "unitCode": "cc"}},
                     "itemCondition": "https://schema.org/UsedCondition"})
    meta = ('"metaData":{"networkId":"%s","make":"Ford","model":"Falcon","badge":"XR6 Turbo",'
            '"state":"NSW","sellertype":"Private","year":"2015","odometermin":"161843",'
            '"price":"28000","bodystyle":"Sedan"}' % ad)
    return (f'<html><head><title>x</title></head><body><script>{{{meta}}}</script>'
            f'<script type="application/ld+json">{ld}</script>'
            f'<div><span>Exterior colour</span></div><div><span>Winter White</span></div>'
            f"</body></html>")


def main() -> int:
    from vpa import details, explore, sweeps
    real_before = REAL.stat().st_mtime_ns if REAL.exists() else None
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "data").mkdir(parents=True)

    print("\n== reading a results page ==")
    page = results_page(CARS, TILES, JS)
    got = details.extract(page)
    a = got["ads"]
    c1, c2, c3 = a[A1]["car"], a[A2]["car"], a[A3]["car"]
    check("engine, gearbox and fuel from each car's own tile",
          (c1.get("cylinders"), c1.get("litres"), c1.get("induction"), c1.get("fuel"), c1.get("gearbox"))
          == (6, 4.0, "turbo", "petrol", "auto")
          and (c2.get("induction"), c2.get("gearbox"), c2.get("cylinders")) == ("supercharged", "manual", 8),
          f"{c1.get('engine')} / {c2.get('engine')}")
    check("condition: used, demo, new to order (tile and analytics agree)",
          (c1.get("condition"), c2.get("condition"), c3.get("condition")) == ("used", "demo", "new to order"),
          "")
    check("seller and state from the tile's seller line; a dealer's own name is never kept",
          (c1.get("seller"), c1.get("state"), c2.get("seller"), c2.get("state"), c3.get("seller"))
          == ("private", "QLD", "dealer", "NSW", "dealer"), "")
    d1, d2, d3 = a[A1]["day"], a[A2]["day"], a[A3]["day"]
    check("price type, the site's rating (either wording) and the flags",
          (d1.get("price_type"), d1.get("rating"), d1.get("just_listed"))
          == ("excl. charges", "great", True)
          and (d2.get("price_type"), d2.get("rating")) == ("drive away", "good")
          and d3.get("price_type") == "drive away" and d3.get("suggested") is True
          and "suggested" not in d1, json.dumps([d1, d2, d3]))
    c4 = a.get(A4, {}).get("car", {})
    check("a tile part naming two ads attributes NOTHING from the tile",
          "gearbox" not in c4 and "engine" not in c4 and "price_type" not in a.get(A4, {}).get("day", {})
          and c4.get("condition") == "used", json.dumps(a.get(A4)))
    check("analytics copies that disagree give no value (seller id)",
          "seller_id" not in c1 and c2.get("seller_id") == "S-22", "")
    check("the page's own sort and result count",
          got["page"] == {"sort": "Price (Low to High)", "total": 4}, json.dumps(got["page"]))
    check("engine wording: '#cyl #L [T|S] fuel', and anything else kept as shown only",
          details.parse_engine("6cyl 4.0L Gas Only") == {"engine": "6cyl 4.0L Gas Only", "cylinders": 6,
                                                         "litres": 4.0, "induction": "natural", "fuel": "lpg"}
          and details.parse_engine("Electric motor") == {"engine": "Electric motor"}
          and details.parse_engine("8cyl 289L Petrol")["litres"] == 4.7
          and details.parse_engine("8cyl 351L Petrol")["cylinders"] == 8
          and details.parse_engine("") == {}, "")

    print("\n== reading a details page ==")
    dp = details.extract(detail_page("EXA-AD-20672993"))["ads"].get("EXA-AD-20672993", {}).get("car", {})
    check("drive, doors, seats, engine size, colour, condition",
          (dp.get("drive"), dp.get("doors"), dp.get("seats"), dp.get("cc"), dp.get("litres"),
           dp.get("colour"), dp.get("gearbox"), dp.get("fuel"), dp.get("condition"), dp.get("state"))
          == ("RWD", 4, 5, 3984, 4.0, "Winter White", "auto", "petrol", "used", "NSW"), json.dumps(dp))

    print("\n== the details file ==")
    n1 = details.remember(DB, got, "2026-09-10")
    n2 = details.remember(DB, got, "2026-09-10")
    details.remember(DB, {"ads": {A1: {"car": {"gearbox": "manual", "colour": "Red"}, "day": {"rating": "fair"}}}},
                     "2026-09-01")
    old = details.load(DB)[A1]
    details.remember(DB, {"ads": {A1: {"car": {"condition": "demo"}, "day": {"price_type": "drive away"}}}},
                     "2026-09-20")
    new = details.load(DB)[A1]
    check("the same page twice changes nothing",
          n1 == 4 and n2 == 0, f"{n1} then {n2}")
    check("an older page only fills blanks; a newer one overwrites what it states",
          old["f"]["gearbox"] == "auto" and old["f"]["colour"] == "Red"
          and new["f"]["condition"] == "demo" and new["f"]["gearbox"] == "auto" and new["d"] == "2026-09-20", "")
    check("sightings kept per capture day",
          new["s"]["2026-09-01"] == {"rating": "fair"}
          and new["s"]["2026-09-10"]["rating"] == "great"
          and new["s"]["2026-09-20"] == {"price_type": "drive away"}, json.dumps(new["s"]))
    details.remember(DB, {"ads": {"not an ad": {"car": {"condition": "used"}},
                                  A5: {"car": {"condition": "<script>" * 20}, "day": {"rating": 5}}}}, "2026-09-10")
    check("nothing but plain short values under real ad ids is stored",
          "NOT AN AD" not in details.load(DB) and A5 not in details.load(DB), "")
    check("written beside its own database, previous version kept as .bak",
          details.store_path(DB) == WORK / "data" / "details.json"
          and (WORK / "data" / "details.json.bak").exists()
          and not (WORK / "data" / "details.json.lock").exists(), "")
    f = WORK / "data" / "details.json"
    good = f.read_bytes()
    f.write_text("{not json", encoding="utf-8")
    try:
        details.remember(DB, got, "2026-09-11")
        refused = False
    except ValueError:
        refused = True
    check("a damaged file is never rewritten from nothing (the save is refused)",
          refused and f.read_text(encoding="utf-8") == "{not json", "")
    f.write_bytes(good)
    details._CACHE.clear()

    print("\n== importing: every path, and the database ==")
    from vpa import bulk, importui
    from vpa.store import Store, read_frame
    f.unlink()
    (WORK / "data" / "details.json.bak").unlink()
    with Store(DB) as st:
        st.conn.execute("SELECT 1")
    bulk.run([{"name": "page1.htm", "text": page, "capture_date": "2026-09-20"}], DB)
    have = details.load(DB)
    check("Upload (and the watch folder, which runs the same import)",
          have.get(A1, {}).get("f", {}).get("engine") == "6cyl 4.0L T Petrol"
          and have[A2]["s"]["2026-09-20"]["price_type"] == "drive away", f"{len(have)} ads")
    p2 = results_page([("EXA-AD-600001", "2012 Ford Falcon XR6 FG", 15000),
                       ("EXA-AD-600002", "2012 Ford Falcon XR6 FG", 16000)],
                      [tile("EXA-AD-600001", "6cyl 4.0L Petrol", "Manual", "Private • SA", "Excl. Govt. Charges"),
                       tile("EXA-AD-600002", "6cyl 4.0L Petrol", "Manual", "Private • SA", "Excl. Govt. Charges")],
                      title="page two", total=2)
    res = importui.parse_payload({"name": "p2.htm", "text": p2}, DB)
    check("Import with preview: previewing stores nothing",
          "EXA-AD-600001" not in details.load(DB) and res.get("ok"), f"{len(res.get('rows', []))} rows")
    rows = [r for r in res["rows"] if "EXA-AD-600001" in str(r.get("listing_id"))]
    importui.commit_payload({"rows": rows}, DB)
    have = details.load(DB)
    check("...and committing stores details for the committed rows only",
          "EXA-AD-600001" in have and "EXA-AD-600002" not in have, "")
    p3 = WORK / "page3.htm"
    p3.write_text(results_page([("EXA-AD-700001", "2013 Ford Falcon XR6 FG", 14000)],
                               [tile("EXA-AD-700001", "6cyl 4.0L Petrol", "Automatic", "Private • TAS",
                                     "Drive away")], title="page three", total=1), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "vpa", "--db", str(DB), "ingest", str(p3)],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    check("the command line", r.returncode == 0 and "EXA-AD-700001" in details.load(DB),
          (r.stdout.strip().splitlines() or [r.stderr[-200:]])[0])

    db_hash = sha(DB)
    (WORK / "data" / "raw").mkdir()
    # the kept pages, dated as the imports dated them (the file's own date)
    import os
    from datetime import datetime
    for name, text, day in (("kept.htm", page, "2026-09-20"), ("kept2.htm", p2, None),
                            ("kept3.htm", p3.read_text(encoding="utf-8"), None)):
        k = WORK / "data" / "raw" / name
        k.write_text(text, encoding="utf-8")
        if day:
            t = datetime.fromisoformat(day + "T12:00:00").timestamp()
            os.utime(k, (t, t))
    f.unlink()
    b = details.backfill(DB)
    df = read_frame(DB)
    r = subprocess.run([sys.executable, "-m", "vpa", "--db", str(DB), "details"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    check("the backfill rebuilds the file from kept pages",
          b["pages"] == 3 and b["ads_in_pages"] == 7 and A1 in details.load(DB), json.dumps(b))
    check("vpa.db is byte-identical across backfill, frame reads and the status command",
          sha(DB) == db_hash and r.returncode == 0, r.stdout.strip()[:120])

    print("\n== the analysis frame ==")
    one = df.sort_values("capture_date").drop_duplicates("key", keep="last").set_index("listing_id")
    with Store(DB) as st:
        stored = {k: (s, se) for k, s, se in st.conn.execute(
            "SELECT listing_id, state, seller_type FROM vehicles")}
    check("new columns, read from the file",
          all(c in df.columns for c in details.FRAME_COLS)
          and one.loc[A1, "engine"] == "6cyl 4.0L T Petrol" and one.loc[A3, "condition"] == "new to order"
          and one.loc[A2, "price_type"] == "drive away" and bool(one.loc[A3, "suggested"]),
          json.dumps({k: str(one.loc[A2, k]) for k in ("engine", "condition", "price_type", "suggested")}))
    check("a stored state always wins; only a blank one is filled",
          stored[A1][0] == "VIC" and one.loc[A1, "state"] == "VIC"
          and stored[A2][0] is None and one.loc[A2, "state"] == "NSW", f"stored {stored[A2]}")
    check("gearbox: the trim name's wins, the listing's fills only where it said none",
          one.loc[A2, "gearbox"] == "auto" and one.loc[A2, "gearbox_listed"] == "manual"
          and one.loc[A1, "gearbox"] == "auto", f"{one.loc[A2, 'gearbox']} / {one.loc[A1, 'gearbox']}")
    f.rename(f.with_suffix(".off"))
    df0 = read_frame(DB)
    f.with_suffix(".off").write_text("{damaged", encoding="utf-8")
    f.with_suffix(".off").rename(f)
    details._CACHE.clear()
    dfx = read_frame(DB)
    check("no file, or a damaged one: blank columns, every row still there",
          len(df0) == len(df) == len(dfx) and df0["condition"].isna().all() and dfx["condition"].isna().all()
          and df0["state"].tolist() == [s for s in df0["state"].tolist()], f"{len(df0)} rows")
    f.unlink()
    details.backfill(DB)

    print("\n== Explore's filters ==")
    p = explore.detail_payload(DB, "Ford", "Falcon")
    opts = p["facets"]
    check("each filter offers what the selection holds, with counts",
          {o["v"] for o in opts["cond"]} >= {"used", "demo", "new to order"}
          and {o["v"] for o in opts["gb"]} >= {"auto", "manual"},
          json.dumps(opts["cond"]))
    q = explore.detail_payload(DB, "Ford", "Falcon", facets={"cond": "notnew"})
    keys = {v["listing_id"] for v in q["vehicles"]}
    check("'not new or demo' drops new and demo, and keeps cars that did not say",
          A1 in keys and A2 not in keys and A3 not in keys and "EXA-AD-600001" in keys
          and [x["text"] for x in q["filters"]] == ["not new or demo"], f"{sorted(keys)}")
    q = explore.detail_payload(DB, "Ford", "Falcon", facets={"pt": "drive away", "st": "NSW"})
    check("filters run in one fixed order, whatever order they come in",
          [x["text"] for x in q["filters"]] == ["state: NSW", "price: drive away"]
          and {v["listing_id"] for v in q["vehicles"]} == {A2}, json.dumps(q["filters"]))
    q = explore.detail_payload(DB, "Ford", "Falcon", facets={"cond": "~"})
    check("'not stated' picks the cars whose pages said nothing",
          q.get("empty") or all(v["condition"] is None for v in q["vehicles"]), "")
    q = explore.detail_payload(DB, "Ford", "Falcon", facets={"nosug": "1"})
    check("leaving out listings the site suggested",
          A3 not in {v["listing_id"] for v in q["vehicles"]}, "")
    q = explore.detail_payload(DB, "Ford", "Falcon", facets={"gb": "nonsense"})
    check("a value nobody has empties the view and says so",
          q.get("empty") and "match the filters" in q["reason"], q.get("reason", ""))
    p = explore.detail_payload(DB, "Ford", "Falcon")
    check("splits by condition, price type and rating",
          {r["name"] for r in p["splits"]["condition"]} >= {"used", "demo"}
          and {r["name"] for r in p["splits"]["price_type"]} >= {"drive away", "excl. charges"}, "")
    v1 = next(v for v in p["vehicles"] if v["listing_id"] == A1)
    check("the cars table carries each car's spec",
          (v1["engine"], v1["gearbox"], v1["rating"], v1["just_listed"]) == ("6cyl 4.0L T Petrol", "auto", "great", True),
          json.dumps({k: v1[k] for k in ("engine", "gearbox", "rating", "condition")}))

    print("\n== searches ==")
    quiet = results_page(CARS, TILES, JS, title="quiet page", visible_total=False)
    check("the page's own result count when its text states none",
          sweeps.stated_total(quiet) is None and sweeps.page_total(quiet) == 4, "")
    from vpa.adapters import parse_file
    qp = WORK / "quiet.htm"
    qp.write_text(quiet, encoding="utf-8")
    recs, _, _ = parse_file(qp)
    s = sweeps.note(DB, quiet, recs)
    check("the sort order each search was saved in",
          s and s["sorts"] == ["Price (Low to High)"] and s["total"] == 4, json.dumps(s and s.get("sorts")))

    check("data/details.json was never touched",
          (REAL.stat().st_mtime_ns if REAL.exists() else None) == real_before, "")
    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
