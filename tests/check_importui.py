"""Live-HTTP verification of the import UI.

    python -m tests.check_importui     (from vehicle-price-analysis/)

Starts the real server (fresh throwaway DB seeded with one known listing),
then acts as the browser via urllib against the messy sample the user
described: a "$18,990" price, a blank odometer, an exact duplicate of a
listing already in the database, plus a price-change re-sighting and one
unparseable junk row. Asserts the preview's claims, commits a subset with
one cell edited, and checks the database contains exactly what the preview
promised. ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from vpa.importui import _Handler
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "importui_test.db"
PORT = 8799

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def api(path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        return json.loads(r.read().decode())


def raw_get(path: str) -> int:
    """HTTP status of a plain GET — for pages, which return HTML not JSON."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


SAMPLE_CSV = """Ad ID,Title,Make,Model,Year,Price ($),KMs,Rego State,Seller,Date seen
NEW-1,2013 Ford Falcon XR6,Ford,Falcon,2013,"$18,990",,NSW,Private seller,04/07/2026
SEED-1,2012 Ford Falcon G6E,Ford,Falcon,2012,"$20,000","150,000",VIC,Dealer,01/07/2026
SEED-1,2012 Ford Falcon G6E,Ford,Falcon,2012,"$19,000","150,000",VIC,Dealer,04/07/2026
,,,,,"$9,999",,,Dealer,04/07/2026
"""


def _mix_scenario_db() -> Path:
    """Throwaway DB for the one case the mover logic must not get wrong.

    Four cars are re-priced +10% a week later. Six cheap cars appear only in
    the second sweep. The POOLED median therefore falls hard while the SAME
    CARS rose 10% - exactly the "Ford Falcon -28%" illusion from the real
    data, reproduced with known numbers.
    """
    p = ROOT / "synthetic" / "overview_mix.db"
    if p.exists():
        p.unlink()
    raw = []
    for i, price in enumerate((10000, 12000, 14000, 16000)):
        for day, mult in (("2026-03-01", 1.0), ("2026-03-08", 1.10)):
            raw.append({"listing_id": f"MIX-{i}", "make": "Ford",
                        "model": "Falcon", "variant": "XR6", "year": 2014,
                        "price": round(price * mult), "odometer": 100000,
                        "capture_date": day})
    for i in range(6):
        raw.append({"listing_id": f"CHEAP-{i}", "make": "Ford",
                    "model": "Falcon", "variant": "XR6", "year": 2004,
                    "price": 5000, "odometer": 250000,
                    "capture_date": "2026-03-08"})
    recs = [r for r in (make_record(d, source="examplecars") for d in raw) if r]
    with Store(p) as store:
        store.ingest(recs, source_file="(mix-scenario)", adapter="test")
    return p


def main() -> int:
    print("== check_importui: seeding throwaway DB ==")
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    seed = make_record({"listing_id": "SEED-1", "make": "Ford", "model": "Falcon",
                        "year": 2012, "price": 20000, "odometer": 150000,
                        "state": "VIC", "seller_type": "dealer",
                        "capture_date": "2026-07-01"}, source="examplecars")
    with Store(DB) as store:
        store.ingest([seed], source_file="(seed)", adapter="seed")

    handler = type("H", (_Handler,), {"db_path": str(DB)})
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        return run_checks()
    finally:
        httpd.shutdown()


def check_chrome() -> None:
    """The nav is assembled from vpa/ui.py into all four pages. Before that
    it was hand-rolled per page, so Explore was a link on one and a button on
    another, no page marked where you were, and only the import page had a
    way to stop the app."""
    import re

    from vpa import bulk, compare, explore, importui, quickadd
    from vpa.ui import MODES, PAGES

    pages = {"overview": importui.OVERVIEW, "import": importui.PAGE,
             "bulk": bulk.PAGE, "explore": explore.PAGE,
             "compare": compare.PAGE, "quick": quickadd.PAGE}
    # the nav tab each page must mark as current: the three ways of adding
    # data are modes of ONE tab, not three tabs
    tab_of = {"overview": "overview", "import": "add", "bulk": "add",
              "quick": "add", "explore": "explore", "compare": "compare"}
    print("\n== the shared page chrome ==")
    check("no template placeholder survives into any served page",
          all(t not in p for p in pages.values()
              for t in ("__NAV__", "__CHROME_CSS__", "__ACTIONS__", "__MODES__",
                        "__CHROME_JS__")),
          f"NAV / CSS / actions / JS all substituted on {len(pages)} pages")
    # counted with a regex, not `in`: the wrapping <nav class="navtabs"> also
    # contains the substring 'class="navtab' and inflated the count by one
    tabs_re = re.compile(r'class="navtab(?: on)?"')
    check("every page offers every page, and marks which one you are on",
          all(len(tabs_re.findall(p)) == len(PAGES) for p in pages.values())
          and all(f'href="{href}"' in p for p in pages.values()
                  for _s, href, _l, _i in PAGES),
          f"{len(PAGES)} tabs on each of {len(pages)} pages, all hrefs present")
    for slug, page in pages.items():
        on = re.search(r'class="navtab on"[^>]*aria-current="page"[^>]*>'
                       r'.*?<span>([^<]+)</span>', page, re.S)
        want = next(lab for s, _h, lab, _i in PAGES if s == tab_of[slug])
        check(f"{slug}: the current page is the one marked active",
              bool(on) and on.group(1) == want,
              f"marked '{on.group(1) if on else None}', expected '{want}'")
    check("every page can stop the app, not just the import page",
          all('id="quitBtn"' in p for p in pages.values())
          and all("confirm(" in p for p in pages.values()),
          f"Shut down present on all {len(pages)}, behind a confirm")

    print("\n== light and dark ==")
    from vpa import dupes, phoneui
    from vpa.ui import CHART_DARK
    every = dict(pages, duplicates=dupes.PAGE, phone=phoneui.PAGE)
    check("every page settles its theme in <head>, before any style is drawn",
          all(0 < p.find("window.vpaTheme=") < p.find("<style>") for p in every.values())
          and all("__CHART_DARK__" not in p for p in every.values()),
          f"theme script ahead of the stylesheet on all {len(every)} pages")
    check("every page has the dark colours and the light/dark switch",
          all(":root[data-theme=dark]{" in p and 'id="themeBtn"' in p
              and p.count("data-theme-set=") == 3 for p in every.values()),
          "dark tokens, switch and its three choices on each page")
    # A chart colour written into page script (fill="#..", or a legend
    # swatch's style) is lifted for a dark card only if CHART_DARK names it -
    # a new colour that is not mapped would sit dim on the dark card.
    lit = set()
    for pg in every.values():
        lit |= {m.lower() for m in re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,6})"', pg)}
        lit |= {m.lower() for m in re.findall(
            r'style="[^"]*?(?:background|fill|color):(#[0-9a-fA-F]{3,6})', pg)}
    unmapped = sorted(lit - set(CHART_DARK) - {"#fff"})
    check("every literal chart colour has a dark-card equivalent",
          not unmapped, f"{len(lit)} literal colours, unmapped: {unmapped or 'none'}")
    app = Path(__file__).resolve().parents[1] / "vpa" / "phone_app"
    idx = (app / "index.html").read_text(encoding="utf-8")
    css = (app / "app.css").read_text(encoding="utf-8")
    check("the phone app settles its theme before its stylesheet, with a setting to choose",
          0 < idx.find("window.vpaTheme") < idx.find("app.css")
          and ":root[data-theme=dark]{" in css and "prefers-color-scheme" not in css
          and 'data-theme-set="${k}"' in (app / "app.js").read_text(encoding="utf-8"),
          "index.html script, dark tokens in app.css, Auto/Light/Dark on Sync & settings")
    from vpa import ui
    keys = {m: set(ui.PALETTES[ui.DEFAULT_PALETTE][m]) for m in ("light", "dark")}
    check("dash themes: four, each setting the same colours in light and dark",
          len(ui.PALETTES) == 4 and all(set(p[m]) == keys[m] == keys["light"]
                                        for p in ui.PALETTES.values() for m in ("light", "dark")),
          ", ".join(p["label"] for p in ui.PALETTES.values()))
    check("every page carries every dash theme, chosen before the first paint",
          all(p.count("data-palette-set=") == len(ui.PALETTES)
              and all(f":root[data-palette={k}][data-theme=dark]{{" in p
                      and f":root[data-palette={k}]:not([data-theme=dark]){{" in p for k in ui.PALETTES)
              and 0 < p.find("window.vpaPalette=") < p.find("<style>")
              and "__PALETTE" not in p for p in every.values()),
          f"{len(ui.PALETTES)} themes x light/dark on all {len(every)} pages")
    a, b = ui.PHONE_MARK
    check("the phone app carries the same dash themes (generated copy up to date)",
          css[css.index(a) + len(a):css.index(b)].strip() == ui.palette_css("phone")
          and "window.vpaPalette" in idx and "data-palette-set" in (app / "app.js").read_text(encoding="utf-8"),
          "app.css matches ui.palette_css('phone'); if not, run ui.write_phone_palettes()")
    check("Shut down is styled apart from navigation, not identical to it",
          all('class="navbtn danger" id="quitBtn"' in p for p in pages.values()),
          "danger styling on the quit button only")
    check("no page has a duplicate element id",
          all(not {i for i in re.findall(r'id="([a-zA-Z0-9_]+)"', p)
                   if re.findall(r'id="([a-zA-Z0-9_]+)"', p).count(i) > 1}
              for p in pages.values()),
          f"ids unique on all {len(pages)} pages")
    check("every page carries the one shared stylesheet, first in its <style>",
          all(p.split("<style>", 1)[1].lstrip().startswith(":root{")
              and "--accent:" in p for p in pages.values()),
          "shared tokens lead every page's CSS, so page rules come after and win")
    check("every page has the same header: Excel viewer, help, shut down, db line",
          all(p.count('id="xlsxBtn"') == 1 and 'id="navstat"' in p
              and 'class="pagehead"' in p for p in pages.values()),
          "workbook button and database line on all pages, once each")
    check("no page re-declares a base rule the shared stylesheet owns",
          all(p.count("--ui:") == 1 and p.count(".card{") == 1
              for p in pages.values()),
          "one :root and one .card rule per page")
    mode_re = re.compile(r'class="mode(?: on)?"')
    for slug in ("import", "bulk", "quick"):
        on = re.search(r'class="mode on"[^>]*aria-current="page"[^>]*>.*?<b>([^<]+)</b>',
                       pages[slug], re.S)
        want = next(lab for s, _h, lab, _i, _d in MODES if s == slug)
        check(f"{slug}: one Add data tab, this mode marked",
              len(mode_re.findall(pages[slug])) == len(MODES)
              and bool(on) and on.group(1) == want
              and all(f'href="{h}"' in pages[slug] for _s, h, _l, _i, _d in MODES),
              f"{len(MODES)} modes, '{on.group(1) if on else None}' current")
    check("no page outside Add data shows the mode switch",
          all('class="modes"' not in pages[k] for k in ("overview", "explore", "compare")),
          "modes only where there is something to add")
    # VACUOUS-GUARD: counting <svg> would also pass on an empty icon set, so
    # the paths themselves are required to be non-empty
    from vpa.ui import ICONS
    check("every nav item carries a real inline icon, no font and no CDN",
          all(len(d) > 20 and d.startswith("M") for d in ICONS.values())
          and all(p.count('class="ico"') >= len(PAGES) + 2
                  for p in pages.values()),
          f"{len(ICONS)} icons, shortest path {min(len(d) for d in ICONS.values())} chars")


def run_checks() -> int:
    import vpa.importui as _IU
    import vpa.explore as _EX
    _iu = _IU
    print("\n== preview (no writes) ==")
    st0 = api("/api/status")
    res = api("/api/parse", {"name": "sample.csv", "text": SAMPLE_CSV,
                             "source": "examplecars", "capture_date": None})
    check("parse ok over live HTTP", res.get("ok") is True, f"ok={res.get('ok')}")
    rows = res.get("rows", [])
    check("3 of 4 raw rows parsed, 1 dropped and reported",
          len(rows) == 3 and res.get("dropped") == 1,
          f"parsed={len(rows)}, raw={res.get('raw_count')}, dropped={res.get('dropped')}")

    by_id = {r.get("listing_id"): r for r in rows}
    new1 = by_id.get("NEW-1")
    check("'$18,990' parsed to 18990", new1 and new1["price_aud"] == 18990,
          f"price_aud={new1 and new1['price_aud']}")
    check("blank odometer flagged, row still ticked",
          new1 and new1["odometer_km"] is None
          and any("no odometer" in f for f in new1["flags"]) and new1["checked"],
          f"flags={new1 and new1['flags']}, checked={new1 and new1['checked']}")

    dupes = [r for r in rows if r["status"] == "duplicate"]
    check("exact duplicate of seeded listing flagged + unticked",
          len(dupes) == 1 and not dupes[0]["checked"]
          and any("already in database" in f for f in dupes[0]["flags"]),
          f"{len(dupes)} duplicate(s), flags={dupes and dupes[0]['flags']}")

    change = [r for r in rows if r["status"] == "new_observation"]
    check("price change on known listing surfaced",
          len(change) == 1 and any("20,000 -> A$19,000" in f for f in change[0]["flags"]),
          f"flags={change and change[0]['flags']}")

    st1 = api("/api/status")
    check("preview wrote NOTHING",
          (st0["vehicles"], st0["observations"]) == (st1["vehicles"], st1["observations"]),
          f"vehicles {st0['vehicles']}->{st1['vehicles']}, obs {st0['observations']}->{st1['observations']}")

    print("\n== commit (edited cell, duplicate left unticked) ==")
    to_commit = []
    for r in rows:
        if r["status"] == "duplicate":
            continue                      # user leaves the dupe unticked
        r = {k: v for k, v in r.items() if k not in ("flags", "checked", "status", "key")}
        if r.get("listing_id") == "NEW-1":
            r["odometer_km"] = "145,000"  # user fixes the blank odometer in the cell
        to_commit.append(r)
    res = api("/api/commit", {"rows": to_commit})
    check("commit ok: 2 observations, 1 new vehicle",
          res.get("ok") and res["committed"] == 2 and res["new_vehicles"] == 1
          and res["duplicates_skipped"] == 0,
          f"committed={res.get('committed')}, new_vehicles={res.get('new_vehicles')}, "
          f"skipped={res.get('duplicates_skipped')}")

    with Store(DB) as store:
        df = store.frame()
        st = store.status()
    check("database matches preview promise (2 vehicles, 3 observations)",
          st["vehicles"] == 2 and st["observations"] == 3,
          f"vehicles={st['vehicles']}, observations={st['observations']}")
    new_row = df[df["listing_id"] == "NEW-1"]
    check("edited odometer went through the real normaliser",
          len(new_row) == 1 and new_row.iloc[0]["odometer_km"] == 145000.0,
          f"stored odometer_km={new_row.iloc[0]['odometer_km'] if len(new_row) else 'missing'}")
    seed_prices = sorted(df[df["listing_id"] == "SEED-1"]["price_aud"].tolist())
    check("price history kept for known listing",
          seed_prices == [19000.0, 20000.0], f"SEED-1 prices={seed_prices}")

    print("\n== idempotency: commit the same rows again ==")
    res = api("/api/commit", {"rows": to_commit})
    check("re-commit skips everything as duplicates",
          res.get("ok") and res["committed"] == 0 and res["duplicates_skipped"] == 2,
          f"committed={res.get('committed')}, skipped={res.get('duplicates_skipped')}")
    res = api("/api/parse", {"name": "sample.csv", "text": SAMPLE_CSV,
                             "source": "examplecars", "capture_date": None})
    now_dupes = [r for r in res["rows"] if r["status"] == "duplicate"]
    known = [r for r in res["rows"] if r["status"] == "new_observation"]
    # NEW-1 was committed WITH the hand-edited odometer, so the original file
    # row (blank odometer) is rightly a differing sighting of a known
    # listing, not an exact duplicate. The two untouched rows are exact dupes.
    check("re-parse: 2 exact duplicates + edited row shown as known listing",
          len(now_dupes) == 2 and len(known) == 1
          and known[0].get("listing_id") == "NEW-1",
          f"{len(now_dupes)} duplicate(s), known={[(r.get('listing_id'), r['flags']) for r in known]}")

    print("\n== Example Ads paste (price-above-title tiles) ==")
    fb_paste = "\n".join([
        "Just listed",
        "AU$55,000", "2010 Ford Falcon", "Sydney, NSW",
        "AU$18,000AU$22,500", "2010 Ford Falcon", "Taree, NSW",
        "AU$11,500", "2007 Ford Falcon", "Sydney, NSW",
        "AU$20,000", "2004 Ford Falcon", "Sydney, NSW",
        "AU$10,000AU$123,456", "2013 Ford Falcon", "Sydney, NSW",
        "AU$18,500", "2010 Ford Falcon", "Sydney, NSW",
        "AU$600", "Ford Falcon FG Mk2 Luxury Pack Rims", "Box Hill, NSW",
        "AU$13,000", "2010 Ford Falcon", "Sydney, NSW",
    ])
    res = api("/api/parse", {"name": "fb.txt", "text": fb_paste,
                             "source": "exampleads", "format": "text",
                             "capture_date": None})
    rows = res.get("rows", [])
    check("7 vehicle tiles parsed; rims tile (no year) skipped",
          res.get("ok") and len(rows) == 7,
          f"parsed={len(rows)} of raw {res.get('raw_count')}")
    got_pairs = sorted((r["year"], r["price_aud"]) for r in rows)
    want_pairs = sorted([(2010, 55000.0), (2010, 18000.0), (2007, 11500.0),
                         (2004, 20000.0), (2013, 10000.0), (2010, 18500.0),
                         (2010, 13000.0)])
    check("each price binds to ITS OWN tile (price-led layout; struck-through "
          "old prices ignored)", got_pairs == want_pairs,
          f"(year, price): {got_pairs}")
    check("no odometer + no id -> distinct keys per listing "
          "(three same-day 2010 Sydney Falcons stay separate)",
          len({r["key"] for r in rows}) == 7
          and all(r["state"] == "NSW" for r in rows),
          f"distinct keys={len({r['key'] for r in rows})}, "
          f"states={sorted({r['state'] for r in rows})}")
    res = api("/api/commit", {"rows": [
        {k: v for k, v in r.items() if k not in ("flags", "checked", "status", "key")}
        for r in rows]})
    check("commit writes 7 distinct vehicles",
          res.get("ok") and res["committed"] == 7 and res["new_vehicles"] == 7,
          f"committed={res.get('committed')}, new_vehicles={res.get('new_vehicles')}")
    res = api("/api/parse", {"name": "fb.txt", "text": fb_paste,
                             "source": "exampleads", "format": "text",
                             "capture_date": None})
    n_dupe = sum(1 for r in res["rows"] if r["status"] == "duplicate")
    check("re-pasting the same dump flags all 7 as duplicates", n_dupe == 7,
          f"{n_dupe} of {len(res['rows'])} duplicate")

    print("\n== empty Exampleads HTML save -> actionable note ==")
    res = api("/api/parse", {"name": "fb_save.htm",
                             "text": "<html><head><title>Exampleads</title></head>"
                                     "<body><div>exampleads.example shell</div>"
                                     "</body></html>",
                             "capture_date": None})
    check("0-listing HTML explains itself and points to the paste box",
          res.get("ok") and not res["rows"] and res.get("note")
          and "paste box" in res["note"],
          f"note: '{str(res.get('note'))[:90]}'")

    print("\n== overview endpoint ==")
    st = api("/api/status")
    ov = api("/api/overview?level=line")
    check("overview serves over live HTTP and agrees with /api/status",
          ov.get("ok") and ov["totals"]["vehicles"] == st["vehicles"]
          and ov["totals"]["observations"] == st["observations"],
          f"{ov['totals']['vehicles']} vehicles, "
          f"{ov['totals']['observations']} observations, "
          f"{ov['totals']['capture_days']} capture days")
    line_names = [m["name"] for m in ov["models"]]
    check("model lines are named 'Make Model' (same string the Excel picker uses)",
          all(len(n.split()) >= 2 for n in line_names) and "Ford Falcon" in line_names,
          f"{line_names}")

    ovt = api("/api/overview?level=trim")
    check("trimless rows are excluded from the trim level, not renamed",
          ovt["level"] == "trim" and ovt["models"] == [],
          "seeded rows carry no variant, so nothing groups at trim level")

    print("\n== movers: a changed pool must not read as a price move ==")
    mixdb = _mix_scenario_db()
    from vpa.importui import overview_payload
    o = overview_payload(mixdb, level="trim")
    # structural checks belong here: this DB actually HAS a trim
    trim_names = [m["name"] for m in o["models"]]
    check("trim names extend a model line ('Ford Falcon' -> 'Ford Falcon XR6')",
          trim_names == ["Ford Falcon XR6"],
          f"{trim_names}")
    check("sparkline carries one point per capture day of that model",
          o["models"] and all(len(m["spark"]) == m["days"] for m in o["models"]),
          f"{[(m['name'], m['days'], len(m['spark'])) for m in o['models']]}")
    check("a 2-day trim is listed as thin coverage",
          [t["name"] for t in o["thin"]] == ["Ford Falcon XR6"]
          and all(t["days"] <= 2 for t in o["thin"]),
          f"thin={o['thin']}")
    m = o["models"][0]
    check("pooled median FALLS when cheap stock enters the sweep",
          m["step"] is not None and m["step"] < -50,
          f"raw median step {m['step']}% (13,000 -> 5,000)")
    check("like-for-like sees the real +10% those same cars moved",
          m["matched"] == 10.0 and m["n_matched"] == 4,
          f"matched {m['matched']}% over {m['n_matched']} cars")
    check("mix flag raised when listing count doubles (4 -> 10)",
          m["mix"] is True, f"mix={m['mix']}")
    top_mover = o["movers"][0] if o["movers"] else {}
    check("mover reports the same-cars figure, not the misleading pooled one",
          top_mover.get("basis") == "same cars" and top_mover.get("step") == 10.0,
          f"{top_mover.get('step')}% basis={top_mover.get('basis')!r}")

    print("\n== the workbook can be rebuilt without opening it ==")
    # Both endpoints run the SAME generate(); only the last step differs.
    # Asserted by watching os.startfile, because "did not open Excel" is not
    # observable from the response alone.
    import vpa.viewer_xlsx as _vx
    opened: list = []
    real_startfile = getattr(_iu.os, "startfile", None)
    real_out = _vx.DEFAULT_OUT
    # Build into a THROWAWAY file. The user's real viewer.xlsx is frequently
    # open in Excel, and a test that rebuilds it either fails for reasons
    # that are not about the code, or rewrites a file the user is reading.
    tmp_out = DB.parent / "viewer_rebuild_test.xlsx"
    if tmp_out.exists():
        tmp_out.unlink()
    _iu.os.startfile = lambda p: opened.append(p)     # type: ignore[attr-defined]
    _vx.DEFAULT_OUT = tmp_out
    try:
        built = api("/api/viewer/build", {"wait": True})
        after_build, built_stamp = list(opened), (
            tmp_out.stat().st_mtime_ns if tmp_out.exists() else None)
        status_fresh = api("/api/status")["workbook"]
        shown = api("/api/viewer/open", {"wait": True})

        # The normal path: the request returns AT ONCE with a job, and the
        # page polls progress. A second click while it runs joins the same
        # job rather than starting a rival build of the same file.
        # held at the start by a gate, so "still running" is certain when
        # the second click lands - a tiny test database builds in a blink
        gate, real_generate = threading.Event(), _vx.generate

        def held(*a, **k):
            gate.wait(10)
            return real_generate(*a, **k)
        _vx.generate = held
        try:
            t0 = time.time()
            first = api("/api/viewer/build", {})
            quick = time.time() - t0
            second = api("/api/viewer/build", {})
        finally:
            gate.set()
            _vx.generate = real_generate
        seen, frac_seen, job = [], [], None
        for _ in range(600):
            job = api("/api/viewer/progress")["job"]
            if job and job.get("text"):
                seen.append(job["text"])
                frac_seen.append(job["frac"])
            if job and not job["running"]:
                break
            time.sleep(0.05)
        polled_status = api("/api/status")["workbook"]
    finally:
        _vx.DEFAULT_OUT = real_out
        if real_startfile is not None:
            _iu.os.startfile = real_startfile          # type: ignore[attr-defined]
    check("rebuild writes the workbook and does NOT open Excel",
          built.get("ok") is True and built.get("opened") is False
          and not after_build and tmp_out.exists(),
          f"ok={built.get('ok')} opened={built.get('opened')}, "
          f"startfile called {len(after_build)} time(s), "
          f"{tmp_out.stat().st_size:,} bytes written")
    check("...while 'Excel viewer' rebuilds AND opens it",
          shown.get("ok") is True and shown.get("opened") is True
          and len(opened) == 1 and opened[0] == str(tmp_out),
          f"opened={shown.get('opened')}, startfile called "
          f"{len(opened)} time(s)")
    check("...and both produce the same file, rebuilt each time",
          built.get("path") == shown.get("path") == str(tmp_out)
          and built_stamp is not None
          and tmp_out.stat().st_mtime_ns >= built_stamp,
          "one generate(), two endings - a rebuild is never a stale no-op")
    check("the build runs in the background: the click returns at once",
          first.get("ok") is True and first.get("job") and quick < 1.5
          and first.get("joined") is False,
          f"answered in {quick:.2f}s with job {first.get('job')}")
    check("...a second click while it runs joins that job, not a rival build",
          second.get("job") == first.get("job") and second.get("joined") is True,
          f"first job {first.get('job')}, second {second.get('job')} "
          f"joined={second.get('joined')}")
    check("...progress is reported stage by stage and only moves forward",
          job and job["ok"] is True and frac_seen == sorted(frac_seen)
          and job["frac"] == 1.0 and len(set(seen)) >= 2,
          f"{len(set(seen))} distinct stages seen, last '{seen[-1] if seen else None}', "
          f"finished ok={job and job['ok']} in {job and job['elapsed']}s")
    check("...and the status poll carries the finished job to every page",
          (polled_status.get("job") or {}).get("id") == first.get("job"),
          f"status job id {(polled_status.get('job') or {}).get('id')}")
    check("a freshly built workbook is reported as up to date",
          status_fresh.get("stale") is False and status_fresh.get("built"),
          f"stale={status_fresh.get('stale')} built={status_fresh.get('built')}")
    check("the user's real viewer.xlsx was never touched by this test",
          _vx.DEFAULT_OUT == real_out and not opened[0].endswith(real_out.name),
          f"built {tmp_out.name}, left {real_out.name} alone")
    for f in (tmp_out, tmp_out.with_name("viewer_cache.json"),
              tmp_out.with_name(f"{tmp_out.stem}.previous{tmp_out.suffix}")):
        f.unlink(missing_ok=True)

    print("\n== a bot-protection page is named, not reported as empty ==")
    # The real interstitial, byte for byte, from data/raw. It parses to zero
    # rows; without this check the app said "no listings", which sends you
    # looking at the parser instead of at the file.
    challenge = (
        '<html lang="en"><head><title>examplecars.example</title></head>'
        '<body style="margin:0"><p id="cmsg">Please enable JS and disable any '
        'ad blocker</p><script data-cfasync="false">var dd={\'rt\':\'i\','
        "'cid':'AHrlqAAAAAMAfkXkTHrVvv0AOqEJIQ==','hsh':'C0705ACD75EBF650',"
        "'host':'geo.captcha-delivery.com'}</script>"
        '<script src="https://ct.captcha-delivery.com/i.js"></script>'
        "</body></html>")
    got = api("/api/parse", {"name": "Ford cars for sale.htm",
                             "text": challenge, "source": "examplecars"})
    check("a saved challenge page is refused and names the system that fired",
          got.get("ok") is False and got.get("challenge") is True
          and "DataDome" in got.get("error", "")
          and "bot-protection" in got.get("error", ""),
          (got.get("error", "") or "")[:96].replace("<b>", "").replace("</b>", ""))
    check("...and the message says to re-save rather than how to get past it",
          "Ctrl+S" in got.get("error", "")
          and "Nothing was imported" in got.get("error", ""),
          "tells you to re-open the search and save it again")
    from vpa.adapters.challenge import detect
    check("Cloudflare and generic block pages are recognised too",
          detect("<title>Just a moment...</title>") == "Cloudflare"
          and detect('<div class="cf-browser-verification">') == "Cloudflare"
          and detect("<title>Access Denied</title>") is not None,
          "three vendors, one message shape")
    # VACUOUS-GUARD: a detector that fired on everything would pass the above
    # while blocking every real capture. DataDome's ORDINARY tag runs on every
    # examplecars page - only the interstitial contacts captcha-delivery.com.
    check("a normal page carrying the vendor's ordinary tag is NOT flagged",
          detect('<script id="datadomeScriptId" '
                 'src="https://dd.examplecars.example/tags.js" async=""></script>'
                 '<html>...listings...</html>') is None,
          "keying on 'datadome' would have flagged all 45 good captures")
    good = api("/api/parse", {"name": "sample.csv", "text": SAMPLE_CSV,
                              "source": "examplecars"})
    check("...and an ordinary import is untouched by the check",
          good.get("ok") is True and len(good.get("rows", [])) == 3,
          f"{len(good.get('rows', []))} rows still parse")

    print("\n== several files in one go ==")
    check("the file input accepts more than one file",
          'id="file"' in _IU.PAGE and "multiple hidden" in _IU.PAGE,
          "multiple attribute present on the picker")
    check("each file keeps its OWN capture date",
          "Each file keeps its own capture date" in _IU.PAGE
          and 'f.capture_date' in _IU.PAGE,
          "a folder saved across three evenings is three capture days, not one")
    check("the date box is disabled when it cannot speak for every file",
          'capdate").disabled=pending.length>1' in _IU.PAGE,
          "one file: editable; several: each uses its own mtime")
    check("rows remember which file they came from",
          "SOURCES.push(f.name)" in _IU.PAGE and "res.files>1" in _IU.PAGE,
          "the preview breaks the count down per file")

    print("\n== nothing this app serves may be cached ==")
    # A cached page pins an OLD copy of the inline JavaScript against today's
    # API: the shell renders, the panels stay blank, no console error, and the
    # server answers every probe correctly. Diagnosed 06/08/2026 - the served
    # page was byte-identical to source and the same URL rendered fine in a
    # different browser, which is the signature of a stale cache.
    def headers_for(path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
                r.read()
                return {k.lower(): v for k, v in r.headers.items()}
        except Exception:
            return {}

    for path in ("/", "/explore", "/compare", "/quick", "/api/status"):
        h = headers_for(path)
        cc = h.get("cache-control", "")
        check(f"{path} forbids caching",
              "no-store" in cc and "no-cache" in cc,
              f"Cache-Control: {cc or '(none)'}")

    print("\n== an endpoint this build lacks says RESTART, not 'unknown' ==")
    # The app is a long-lived Python process that holds the code it imported
    # at launch. After an upgrade, a freshly-served page can call an endpoint
    # the running process has never heard of. That surfaced as
    # alert("unknown endpoint"), which reads like a broken feature rather
    # than "restart me" - and cost real time when starring appeared to fail.
    import urllib.error as _ue

    def api_404(path, post=False):
        url = f"http://127.0.0.1:{PORT}{path}"
        req = urllib.request.Request(url, data=b"{}" if post else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except _ue.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    for path, post in (("/api/no-such-thing", False), ("/api/nope", True)):
        code, body = api_404(path, post)
        check(f"{'POST' if post else 'GET '} to a missing API route explains itself",
              code == 404 and body.get("stale") is True
              and "older version" in body.get("error", "")
              and path in body.get("error", ""),
              f"HTTP {code}, stale={body.get('stale')}: "
              f"{body.get('error', '')[:70]}")
    check("a missing NON-api path is still a plain 404, not that message",
          raw_get("/definitely-not-a-page") == 404,
          "only /api/* routes get the restart advice")
    page = _IU.PAGE
    check("the page reacts to it with a banner instead of an alert",
          "showStale" in _EX.PAGE and "apiCall" in _EX.PAGE
          and "needs restarting" in _EX.PAGE,
          "one banner with a Shut down button, not one alert per click")
    check("...and every endpoint added after launch goes through that helper",
          all(f'apiCall("{p}' in _EX.PAGE or f"apiCall(\"{p}" in _EX.PAGE
              for p in ("/api/star", "/api/watchlist", "/api/edit/preview",
                        "/api/edit/apply"))
          and '/api/edit/row' in _EX.PAGE,
          "star, watchlist and both edit calls are stale-aware")

    print("\n== every page serves, however you arrive at it ==")
    # REGRESSION (06/08/2026): `python -m vpa.importui` served every page
    # except "/", which died on NameError: PAGE. The __main__ block sat ABOVE
    # the PAGE assignment and main() blocks in serve_forever(), so the module
    # body never finished. Running the module in a subprocess is the only way
    # to catch it - importing it, as every other test does, completes the body
    # and hides the fault.
    import subprocess
    probe = subprocess.run(
        [sys.executable, "-c",
         "import runpy,sys;sys.argv=['x','--help'];\n"
         "mod=__import__('vpa.importui',fromlist=['PAGE']);\n"
         "print('PAGE' in dir(mod))"],
        capture_output=True, text=True, cwd=str(ROOT.parent))
    check("the served page object exists after the module body runs",
          probe.stdout.strip().endswith("True"),
          f"vpa.importui.PAGE present: {probe.stdout.strip() or probe.stderr[-90:]}")
    src = (ROOT.parent / "vpa" / "importui.py").read_text(encoding="utf-8")
    check("...and the __main__ block is the LAST statement in the module",
          src.rstrip().endswith("raise SystemExit(main())")
          and src.index('if __name__ == "__main__":') > src.index("PAGE = _chrome"),
          "nothing executes after main() blocks, so PAGE must be assigned "
          "before it")
    for path in ("/", "/?v=1", "/index.html", "/add", "/import", "/bulk", "/quick"):
        code = raw_get(path)
        check(f"GET {path} serves a page", code == 200, f"HTTP {code}")
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=30) as r:
        home = r.read().decode("utf-8")
    check("the home page is the Overview, not an import form",
          "dataset at a glance" in home and 'id="paste"' not in home,
          "/ reads the data; adding it is one tab over")

    print("\n== the data check leads the Overview; adding lives in Add data ==")
    page = _iu.OVERVIEW
    at = lambda s: page.find(s)
    check("data check comes before 'dataset at a glance'",
          0 <= at('id="verify"') < at("dataset at a glance"),
          f'character offsets: data check {at(chr(34).join(["id=", "verify", ""]))}'
          f' < at a glance {at("dataset at a glance")}')
    check("...and it appears exactly once, and only on the Overview",
          page.count('id="verify"') == 1 and page.count('id="vfspan"') == 1
          and 'id="verify"' not in _iu.PAGE,
          "one data-check card, not two")
    check("the import form is on Add data, with its preview directly under it",
          0 <= _iu.PAGE.find('id="paste"') < _iu.PAGE.find('id="previewCard"')
          and "dataset at a glance" not in _iu.PAGE,
          "no dataset panels between the drop box and its preview")
    check("rebuild-without-opening is its own button beside Excel viewer",
          'id="rebuildBtn"' in page and 'id="xlsxBtn"' in page
          and "/api/viewer/build" in page and "/api/viewer/open" in page,
          "Rebuild workbook and Excel viewer are separate actions")

    check_chrome()

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
