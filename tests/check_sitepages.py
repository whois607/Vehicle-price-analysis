"""Saved site pages: seller type, state and body type, in both formats - and
the advert links built from what they stored.

    python -m tests.check_sitepages

From 27/07/2026 every examplecars capture stored no seller type and no state.
The site had moved its per-listing analytics from prefixed keys with the
fields AFTER the ad id ("tracking/item/networkId":..., "tracking/item/adtype")
to flat objects with the state BEFORE it:
    {..."state":"VIC","networkId":"EXA-AD-1","adtype":"Private",...}
and the parser matched none of it. Body type was never stored at all: the
JSON-LD `bodyType` is blank, and the only per-car statement of it is each
tile's key-details list, which carries no ad id.

The pages here are synthetic, built in the shape of the real ones. Every
check that attributes a value is paired with one where attributing it would
be a GUESS, and the parser must then attribute nothing.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import sqlite3

from vpa import links
from vpa.adapters import htmlpage
from vpa.store import Store

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def ld(cars) -> str:
    items = [{"@type": "ListItem", "position": i + 1, "item": {
        "@type": "Vehicle", "bodyType": "",
        "mileageFromOdometer": {"@type": "QuantitativeValue", "value": str(c["km"])},
        "url": f"/cars/details/x/{c['id']}/", "name": f"2016 Ford Falcon XR6 Turbo FG X Auto",
        "model": "Falcon", "brand": {"@type": "Brand", "name": "Ford"},
        "offers": {"@type": "Offer", "priceCurrency": "AUD", "price": str(c["price"])}}}
        for i, c in enumerate(cars)]
    blob = {"@context": "https://schema.org", "@type": "SearchResultsPage",
            "mainEntity": {"@type": "OfferCatalog", "itemListElement": items}}
    return f'<script type="application/ld+json">{json.dumps(blob)}</script>'


def flat(c, state=None, adtype=None, extra_id=None) -> str:
    """One analytics object in the new flat shape, state BEFORE the id."""
    bits = ['"eventCategory":"vehicle_listing"', '"make":"Ford"']
    if state:
        bits.append(f'"state":"{state}"')
    bits.append(f'"networkId":"{c["id"]}"')
    if extra_id:
        bits.append(f'"relatedNetworkId":"x","networkId":"{extra_id}"')
    bits.append(f'"price":"{c["price"]}"')
    if adtype:
        bits.append(f'"adtype":"{adtype}"')
    return "{" + ",".join(bits) + "}"


def tiles(cars, body="Sedan", km_override=None) -> str:
    out = []
    for i, c in enumerate(cars):
        km = km_override[i] if km_override else c["km"]
        out.append('{"keyDetailsTexts":' + json.dumps(
            [body, "6cyl 4.0L T Petrol", "Automatic", f"{km:,} km"]) + "}")
    return "".join(out)


def page(*parts) -> Path:
    html = ("<html><head><title>Ford Falcon cars for sale - examplecars.example</title>"
            "</head><body>" + "".join(parts) + "</body></html>")
    f = tempfile.NamedTemporaryFile("w", suffix=".htm", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    return Path(f.name)


def parse(*parts) -> dict:
    p = page(*parts)
    try:
        raws, _ = htmlpage.parse(p)
    finally:
        p.unlink()
    return {r["listing_id"]: r for r in raws}


CARS = [{"id": "EXA-AD-101", "price": 31400, "km": 187000},
        {"id": "EXB-AD-202", "price": 41990, "km": 125583},
        {"id": "EXA-AD-303", "price": 46500, "km": 117600}]


PATH = "/cars/details/2016-ford-falcon-g6e-turbo-fg-x-auto/EXA-AD-20616552/"
FULL = "https://www.examplecars.example" + PATH


def link_checks() -> None:
    """Links from a stored car to its advert (vpa/links.py): a bare path
    gets its site's address in front, nothing is guessed for a path of
    unknown origin, and the stored value is never touched."""
    print("\n== the full address of an advert ==")
    check("a examplecars bare path gets the site in front",
          links.ad_url(PATH, "examplecars") == FULL, links.ad_url(PATH, "examplecars"))
    check("a full address is left exactly as it is",
          links.ad_url("https://example.com/ad/1", "exampleads") == "https://example.com/ad/1",
          "unchanged")
    check("a protocol-relative address gets https",
          links.ad_url("//www.examplecars.example" + PATH, None) == FULL, "https: added")
    check("a bare path from a site with no known address is not guessed",
          links.ad_url(PATH, "exampleads") is None and links.ad_url(PATH, None) is None,
          "no link rather than a wrong one")
    check("no link, an empty link and a NaN all give None",
          all(links.ad_url(v, "examplecars") is None for v in (None, "", "  ", float("nan"))),
          "nothing to open")
    check("the archive link lists copies of the full address",
          links.archive_url(FULL) == "https://web.archive.org/web/*/" + FULL
          and links.archive_url(None) is None, links.archive_url(FULL))

    print("\n== the stored value is never touched ==")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        store = Store(db)
        from vpa.schema import make_record
        rec = make_record({"make": "Ford", "model": "Falcon", "variant": "XR6 Turbo",
                           "year": 2016, "price": 31400, "odometer": 187000,
                           "listing_id": "EXA-AD-20616552", "url": PATH,
                           "capture_date": "2026-09-01"}, source="examplecars")
        store.ingest([rec], source_file="t", adapter="t")
        links.ad_url(store.frame()["url"].iloc[0], "examplecars")
        store.close()
        con = sqlite3.connect(db)
        stored = con.execute("SELECT url FROM vehicles").fetchone()[0]
        con.close()
        check("the database still holds the bare path it was given",
              stored == PATH, stored)


def main() -> int:
    print("\n== the flat format examplecars moved to ==")
    # each listing's object repeated, as the real page repeats it per event
    objs = "".join(flat(c, st, ad) * 3 for c, st, ad in
                   zip(CARS, ["VIC", "WA", "NSW"], ["Private", "Dealer used", "Private"]))
    got = parse(ld(CARS), objs)
    check("state read from BEFORE the ad id, seller from after it",
          [(got[c["id"]].get("state"), got[c["id"]].get("seller_type")) for c in CARS]
          == [("VIC", "private"), ("WA", "dealer"), ("NSW", "private")],
          str([(got[c['id']].get('state'), got[c['id']].get('seller_type')) for c in CARS]))
    got = parse(ld(CARS), flat(CARS[0], "VIC", "Private").replace('"adtype"', '"adType"'))
    check("the camel-case adType spelling is read too",
          got["EXA-AD-101"].get("seller_type") == "private",
          f"seller {got['EXA-AD-101'].get('seller_type')}")

    print("\n== where attributing would be a guess, nothing is attributed ==")
    got = parse(ld(CARS), flat(CARS[0], "VIC", "Private", extra_id="EXB-AD-202"))
    check("an object naming two cars is used for neither",
          "state" not in got["EXA-AD-101"] and "state" not in got["EXB-AD-202"],
          "no neighbour's state or seller leaks across")
    got = parse(ld(CARS), flat(CARS[0], "VIC", "Private") + flat(CARS[0], "NSW", "Private"))
    check("copies that disagree leave that field empty, the agreeing one set",
          "state" not in got["EXA-AD-101"]
          and got["EXA-AD-101"].get("seller_type") == "private",
          f"state {got['EXA-AD-101'].get('state')}, seller {got['EXA-AD-101'].get('seller_type')}")
    got = parse(ld(CARS), flat(CARS[0], None, "New car"))
    check("a showroom 'New car' is not called a dealer or private sale",
          "seller_type" not in got["EXA-AD-101"], "left empty rather than guessed")

    print("\n== the old prefixed format still reads exactly as before ==")
    old = ('"tracking/item/networkId":"EXA-AD-101","tracking/item/state":"QLD",'
           '"tracking/item/adtype":"Dealer used"')
    got = parse(ld(CARS), old)
    check("prefixed keys with the fields after the id",
          got["EXA-AD-101"].get("state") == "QLD"
          and got["EXA-AD-101"].get("seller_type") == "dealer",
          f"{got['EXA-AD-101'].get('state')} / {got['EXA-AD-101'].get('seller_type')}")
    got = parse(ld(CARS), old, flat(CARS[0], "VIC", "Private"))
    check("when both formats are present, the old one's values stand",
          got["EXA-AD-101"].get("state") == "QLD", "the flat format only fills gaps")

    print("\n== body type from the tiles, only when provably lined up ==")
    got = parse(ld(CARS), tiles(CARS))
    check("one tile per listing, same order, odometers equal: body type set",
          all(got[c["id"]].get("body_type") == "Sedan" for c in CARS),
          str([got[c['id']].get('body_type') for c in CARS]))
    got = parse(ld(CARS), tiles(CARS, km_override=[187000, 117600, 125583]))
    check("tiles out of order (odometers swapped): body type set for NONE",
          all("body_type" not in got[c["id"]] for c in CARS),
          "one mismatch voids the whole page")
    got = parse(ld(CARS), tiles(CARS[:2]))
    check("fewer tiles than listings: body type set for none",
          all("body_type" not in got[c["id"]] for c in CARS), "counts must match")
    got = parse(ld(CARS), tiles(CARS, body="6cyl"))
    check("a tile with no recognisable body word gives no body type",
          all("body_type" not in got[c["id"]] for c in CARS), "not invented from other text")

    link_checks()

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
