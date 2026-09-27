"""Links from a stored car to its advert.

    python -m tests.check_links

carsales' own links are bare paths, stored as they came. Shown as they
stand they resolve against the app (127.0.0.1) and go nowhere, so every
link that leaves the app gets the site's address put in front. These checks
pin that rule, that nothing is guessed for a path of unknown origin, and
that the stored values themselves are never touched by it.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from vpa import links
from vpa.store import Store

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


PATH = "/cars/details/2016-ford-falcon-g6e-turbo-fg-x-auto/SSE-AD-20616552/"
FULL = "https://www.carsales.com.au" + PATH


def main() -> int:
    print("\n== the full address of an advert ==")
    check("a carsales bare path gets the site in front",
          links.ad_url(PATH, "carsales") == FULL, links.ad_url(PATH, "carsales"))
    check("a full address is left exactly as it is",
          links.ad_url("https://example.com/ad/1", "facebook") == "https://example.com/ad/1",
          "unchanged")
    check("a protocol-relative address gets https",
          links.ad_url("//www.carsales.com.au" + PATH, None) == FULL, "https: added")
    check("a bare path from a site with no known address is not guessed",
          links.ad_url(PATH, "facebook") is None and links.ad_url(PATH, None) is None,
          "no link rather than a wrong one")
    check("no link, an empty link and a NaN all give None",
          all(links.ad_url(v, "carsales") is None for v in (None, "", "  ", float("nan"))),
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
                           "listing_id": "SSE-AD-20616552", "url": PATH,
                           "capture_date": "2026-09-01"}, source="carsales")
        store.ingest([rec], source_file="t", adapter="t")
        links.ad_url(store.frame()["url"].iloc[0], "carsales")
        store.close()
        con = sqlite3.connect(db)
        stored = con.execute("SELECT url FROM vehicles").fetchone()[0]
        con.close()
        check("the database still holds the bare path it was given",
              stored == PATH, stored)
    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
