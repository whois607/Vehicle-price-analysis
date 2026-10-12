"""Searches and how much of each was saved (vpa/sweeps.py).

    python -m tests.check_sweeps

Everything on throwaway files under tests/synthetic/sweeps_work/:

  * the stated total is read from a page's visible text in the usual
    wordings, never from a price, and a site's own `total_pattern` wins;
  * the pages of one search add up (a page saved twice counts once), and a
    search is short only when it misses more than a little slack;
  * every import path notes the search: Bulk upload (and so the watch
    folder), Import with preview (only what is committed), `vpa ingest`;
    and noting never writes to the database;
  * the analysis frame flags the cars of a search that missed more than 5%,
    and turnover then shows exits into that capture but never counts them;
  * the planner's bands each fit, size and limit come from where it says,
    and a search that fits gets no bands;
  * a backfill rebuilds the notes from kept pages.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "sweeps_work"
DB = WORK / "data" / "vpa.db"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def car(i: int, price: int) -> dict:
    return {"id": f"EXA-AD-{70000 + i}", "km": 90000 + i * 1371, "price": price}


def page_html(cars, total: int | None, title="Ford Falcon cars for sale - examplecars.example",
              phrase="{n} Ford Falcon cars for sale in Australia") -> str:
    items = [{"@type": "ListItem", "position": i + 1, "item": {
        "@type": "Vehicle", "url": f"/cars/details/x/{c['id']}/",
        "name": "2016 Ford Falcon XR6 Turbo FG X Auto", "model": "Falcon",
        "brand": {"@type": "Brand", "name": "Ford"},
        "mileageFromOdometer": {"@type": "QuantitativeValue", "value": str(c["km"])},
        "offers": {"@type": "Offer", "priceCurrency": "AUD", "price": str(c["price"])}}}
        for i, c in enumerate(cars)]
    blob = {"@context": "https://schema.org", "@type": "SearchResultsPage",
            "mainEntity": {"@type": "OfferCatalog", "itemListElement": items}}
    head = f"<h1>{phrase.format(n=f'{total:,}')}</h1>" if total is not None else "<h1>Ford Falcon</h1>"
    cards = "".join(f"<div><p>2016 Ford Falcon XR6 Turbo</p><p>${c['price']:,}</p>"
                    f"<p>{c['km']:,} km</p></div>" for c in cars)
    return (f"<html><head><title>{title}</title></head><body>{head}{cards}"
            f'<script type="application/ld+json">{json.dumps(blob)}</script></body></html>')


def db_rows_hash() -> str:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    h = hashlib.sha256()
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        for r in con.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
            h.update(repr(r).encode())
    con.close()
    return h.hexdigest()[:16]


def main() -> int:
    from vpa import bulk, explore, importui, sweeps
    from vpa.store import Store
    if WORK.exists():
        shutil.rmtree(WORK)
    DB.parent.mkdir(parents=True)
    Store(DB).close()

    print("\n== the stated total, from what the page shows ==")
    st = sweeps.stated_total
    cases = {"1,234 Ford Falcon cars for sale in Australia": 1234, "312 results": 312,
             "Showing 48 listings found near you": 48, "17 BMW 3 Series cars for sale": 17,
             "Price $12,990 drive away": None, "no count anywhere here": None}
    got = {k: st(f"<html><body><p>{k}</p></body></html>") for k in cases}
    check("the usual wordings are read; a price or no count gives nothing",
          got == cases, str(got))
    check("a count in a script, not on the page, is not read",
          st('<html><body><script>var x="999 cars for sale"</script><p>Falcon</p></body></html>') is None,
          "scripts and styles are dropped first")

    print("\n== the pages of one search add up ==")
    A = [car(i, 20000 + i * 300) for i in range(60)]
    p1, p2, p3 = A[:22], A[22:44], A[44:60]
    h0 = db_rows_hash()
    r1 = bulk.run([{"name": "p1.htm", "text": page_html(p1, 60), "capture_date": "2026-09-01"}], DB, label="t")
    f1 = r1["files"][0]
    check("Bulk upload notes the page's search", f1.get("search", {}).get("total") == 60
          and f1["search"]["saved"] == 22 and f1["search"]["short"], json.dumps(f1.get("search")))
    r2 = bulk.run([{"name": "p2.htm", "text": page_html(p2, 60), "capture_date": "2026-09-01"},
                   {"name": "p3.htm", "text": page_html(p3, 60), "capture_date": "2026-09-01"}], DB, label="t")
    s = r2["files"][-1].get("search") or {}
    check("three pages of a 60-listing search: complete, 3 pages",
          s.get("saved") == 60 and s.get("pages") == 3 and not s.get("short"), json.dumps(s))
    again = sweeps.note(DB, page_html(p2, 60), _recs(page_html(p2, 60), "2026-09-01"))
    check("the same page noted again counts once", again["saved"] == 60 and again["pages"] == 3,
          f"{again['saved']} saved, {again['pages']} pages")
    noted_only = db_rows_hash()
    # dated a day with no captures, so its cars flag nothing in the frame
    sweeps.note(DB, page_html(p1, 61), _recs(page_html(p1, 61), "2026-08-25"))
    check("noting a search never writes to the database", db_rows_hash() == noted_only,
          "row fingerprint unchanged by a note")
    other = [x for x in sweeps.searches(DB) if x["total"] == 61]
    check("a different stated total is a different search", len(other) == 1 and other[0]["saved"] == 22,
          f"{len(sweeps.searches(DB))} searches recorded")

    print("\n== short, a little short, and the slack ==")
    check("short only past the slack: 60 of 61 is complete, 22 of 61 is short",
          not sweeps._short(61, 60) and sweeps._short(61, 22), "slack 2 or 1%")
    check("set aside for turnover only past 5%: 154 of 159 stays in, 415 of 437 is set aside",
          not sweeps._material(159, 154) and sweeps._material(437, 415), "CUT_SHARE 5%")

    print("\n== turnover sets a cut-short capture aside ==")
    # day 1: the 60 cars, complete. day 2: a search stating 60, only 44 saved
    # (the rest still listed, past the last page saved) - not so much
    # smaller that it reads as a narrowed search on its own
    B = A[:44]
    bulk.run([{"name": "d2.htm", "text": page_html(B, 60), "capture_date": "2026-09-08"}], DB, label="t")
    df = explore._frame(DB)
    cut = df[df["cut"]]
    check("the frame flags the cut-short day's cars, and only those",
          set(cut["day"]) == {"2026-09-08"} and len(cut) == 44, f"{len(cut)} rows flagged")
    t = explore.detail_payload(DB, "Ford", "Falcon", None)["churn"]
    iv = [i for i in t["intervals"] if i["to"] == "2026-09-08"]
    check("exits into it are shown but not counted: 16 'missing', none rated",
          iv and iv[0]["scope"] == "cut short" and not iv[0]["counts_exits"] and iv[0]["exits"] == 16
          and t["exits"] == 0, f"interval {iv[0] if iv else None}")
    why = " ".join(t["notes"]) + " " + t.get("reason", "")
    check("...and the page says why", "past the last page" in why, why[:160])
    p = sweeps.store_path(DB)
    p.rename(p.with_name("off.json"))
    sweeps._CACHE.clear()
    t2 = explore.detail_payload(DB, "Ford", "Falcon", None)["churn"]
    p.with_name("off.json").rename(p)
    sweeps._CACHE.clear()
    check("without the notes the same 16 would have been counted as gone",
          not any(i["scope"] == "cut short" for i in t2["intervals"])
          and sum(i["exits"] for i in t2["intervals"] if i["counts_exits"]) >= 16,
          f"{sum(i['exits'] for i in t2['intervals'] if i['counts_exits'])} exits counted")

    print("\n== the planner ==")
    big = [car(1000 + i, 9000 + i * 97) for i in range(300)]
    bulk.run([{"name": "big.htm", "text": page_html(big, 1200, phrase="{n} results"),
               "capture_date": "2026-09-15"}], DB, label="t")
    pl = sweeps.plan(DB, "Ford", "Falcon", None)
    cap = pl["capacity"]
    check("the limit comes from the site file when it names one, and says so",
          cap["basis"] in ("site file", "seen", "assumed") and cap["cars"] > 0, cap["note"])
    check("a 1,200-listing search gets bands that each fit",
          pl["size"] == 1200 and not pl["fits"] and len(pl["bands"]) >= 3
          and all(b["fits"] for b in pl["bands"]) and sum(b["estimate"] for b in pl["bands"]) in range(1190, 1211),
          f"{len(pl['bands'])} bands: {[b['estimate'] for b in pl['bands']]}; size {pl.get('size')} "
          f"({pl.get('size_basis')}) {pl.get('error', '')}")
    edges = [b["to"] for b in pl["bands"][:-1]]
    check("band edges rise and are round numbers",
          edges == sorted(edges) and all(e % 500 == 0 for e in edges), str(edges))
    small = sweeps.plan(DB, "Ford", "Falcon", None, total=50)
    check("a search that fits gets no bands, and is told so",
          small["fits"] and not small["bands"] and small["tips"], small["tips"][0] if small["tips"] else "")

    print("\n== the other import paths ==")
    C = [car(5000 + i, 30000 + i * 50) for i in range(10)]
    html = page_html(C, 25, title="Holden cars - examplecars.example", phrase="{n} Ford Falcon results")
    prev = importui.parse_payload({"text": html, "name": "c.htm"}, DB)
    rows = [{k: v for k, v in r.items() if k not in ("key", "status", "flags", "checked")}
            for r in prev["rows"]][:6]
    importui.commit_payload({"rows": rows}, DB)
    ui = [x for x in sweeps.searches(DB) if x["total"] == 25]
    check("Import with preview notes only the cars committed",
          len(ui) == 1 and ui[0]["saved"] == 6, f"{ui[0]['saved'] if ui else 0} of 10 previewed, 6 committed")
    f = _tmp(page_html([car(6000 + i, 41000) for i in range(5)], 9, phrase="{n} listings found"))
    from vpa import cli
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(["--db", str(DB), "ingest", str(f)])
    check("vpa ingest notes it and says how much of the search is saved",
          "stated 9" in buf.getvalue() and "5 saved" in buf.getvalue(),
          next((ln for ln in buf.getvalue().splitlines() if "Search" in ln), buf.getvalue()[-120:]))

    print("\n== a backfill from kept pages ==")
    raw = DB.parent / "raw"
    raw.mkdir()
    for i, part in enumerate((p1, p2, p3)):
        (raw / f"k{i}.htm").write_text(page_html(part, 60), encoding="utf-8")
    p.unlink()
    sweeps._CACHE.clear()
    b = sweeps.backfill(DB)
    again = [x for x in sweeps.searches(DB) if x["total"] == 60]
    check("the notes are rebuilt from the kept pages", b["noted"] == 3 and len(again) == 1
          and again[0]["saved"] == 60, json.dumps(b))
    check("and the database was never written by any of it except the imports",
          h0 != db_rows_hash(), "the imports above wrote; notes did not (checked above)")

    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


def _tmp(html: str) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    n = len(list(WORK.glob("t*.htm")))
    f = WORK / f"t{n}.htm"
    f.write_text(html, encoding="utf-8")
    return f


def _recs(html: str, day: str):
    from vpa.adapters import parse_file
    return parse_file(_tmp(html), capture_date=day)[0]


if __name__ == "__main__":
    sys.exit(main())
