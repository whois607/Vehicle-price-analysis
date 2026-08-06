"""Live-HTTP verification of Quick Add.

    python -m tests.check_quickadd     (from vehicle-price-analysis/)

Seeds a prior Quick Add batch (2026-07-01) and an imported carsales row
(2026-07-02) for a test model, then drives /api/quick/* on the real server:
exact median/mode/highest/lowest over the all-history pool, exact signed
percentage changes against the pre-today baseline, the multimodal tie case,
the all-unique no-mode case, the first-time no-prior-data path, entry
removal recompute, unparsable entries, commit into the shared store, and
pickup by the existing analysis code. ASCII output; exit 0 = all pass.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

from vpa import analyze
from vpa.cli import main as cli_main
from vpa.importui import _Handler
from vpa.schema import make_record
from vpa.segments import add_segment_cols
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "quickadd_test.db"
PORT = 8798
TODAY = date.today().isoformat()

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def api(path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        return json.loads(r.read().decode())


def approx(a, b, tol=0.01) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def main() -> int:
    print("== check_quickadd: seeding prior history ==")
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()

    handler = type("H", (_Handler,), {"db_path": str(DB)})
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        return run_checks()
    finally:
        httpd.shutdown()


def run_checks() -> int:
    # seed 1: a previous Quick Add batch on 2026-07-01 (via the real endpoint)
    res = api("/api/quick/commit", {"make": "testmake", "model": "testmodel",
                                    "prices": ["10000", "10,500", "$10,500", "12000"],
                                    "capture_date": "2026-07-01"})
    check("seed batch committed via endpoint", res.get("ok") and res["committed"] == 4,
          f"committed={res.get('committed')} (normalised line: {res.get('line')})")
    # seed 2: an imported carsales row on 2026-07-02 (via the standard pipeline)
    rec = make_record({"listing_id": "CS-1", "make": "Testmake", "model": "Testmodel",
                       "price": 11000, "capture_date": "2026-07-02"}, source="carsales")
    with Store(DB) as store:
        store.ingest([rec], source_file="(seed)", adapter="seed")

    print("\n== model autocomplete ==")
    res = api("/api/quick/models")
    lines = {m["line"]: m["n"] for m in res["models"]}
    check("models endpoint lists normalised line with count",
          lines.get("Testmake Testmodel") == 5, f"models: {lines}")

    print("\n== live stats: all-history pool + pre-today baseline ==")
    # baseline pool (before today): [10000, 10500, 10500, 12000, 11000]
    #   median 10500, hi 12000, lo 10000, mode $10,500 x2
    # batch [13000, 9000, 13000] -> now pool n=8:
    #   [9000,10000,10500,10500,11000,12000,13000,13000]
    #   median 10750, hi 13000, lo 9000, mode tie 10500 & 13000 -> 10500 (tie)
    res = api("/api/quick/stats", {"make": "TESTMAKE", "model": "testmodel",
                                   "prices": ["13000", "9000", "13,000"]})
    now, base, ch = res["now"], res["baseline"], res["changes"]
    check("pool sizes: 5 stored + 3 batch = 8; baseline 5",
          now["n"] == 8 and res["stored_n"] == 5 and res["baseline_n"] == 5,
          f"n={now['n']}, stored={res['stored_n']}, baseline={res['baseline_n']}")
    check("median/highest/lowest over the pool",
          approx(now["median"], 10750) and approx(now["highest"], 13000)
          and approx(now["lowest"], 9000),
          f"median={now['median']}, hi={now['highest']}, lo={now['lowest']}")
    check("multimodal mode -> lowest tied $500 bucket, flagged tie",
          now["mode"] and now["mode"]["value"] == 10500 and now["mode"]["count"] == 2
          and now["mode"]["tie"] is True, f"mode={now['mode']}")
    check("baseline stats (pre-today pool)",
          approx(base["median"], 10500) and approx(base["highest"], 12000)
          and approx(base["lowest"], 10000) and base["mode"]["value"] == 10500
          and base["mode"]["tie"] is False, f"baseline={base}")
    check("signed % changes: +2.38 / +8.33 / -10.0 / 0.0",
          approx(ch["median"], 2.381, 0.01) and approx(ch["highest"], 8.333, 0.01)
          and approx(ch["lowest"], -10.0, 0.01) and approx(ch["mode"], 0.0, 0.001),
          f"median {ch['median']:+.3f}%, hi {ch['highest']:+.3f}%, "
          f"lo {ch['lowest']:+.3f}%, mode {ch['mode']:+.3f}%")

    print("\n== removal, unparsable, edge cases ==")
    res = api("/api/quick/stats", {"make": "Testmake", "model": "Testmodel",
                                   "prices": ["13000", "9000"]})
    now = res["now"]
    check("removing an entry recomputes (n=7, median back to 10500, tie gone)",
          now["n"] == 7 and approx(now["median"], 10500)
          and now["mode"]["value"] == 10500 and now["mode"]["tie"] is False
          and approx(res["changes"]["median"], 0.0, 0.001),
          f"n={now['n']}, median={now['median']}, mode={now['mode']}, "
          f"median change {res['changes']['median']:+.3f}%")
    res = api("/api/quick/stats", {"make": "Testmake", "model": "Testmodel",
                                   "prices": ["negotiable", "12,500"]})
    check("unparsable entry flagged, excluded from stats",
          res["entries"][0]["ok"] is False and res["entries"][1]["ok"] is True
          and res["batch_n"] == 1,
          f"entries={[(e['raw'], e['ok']) for e in res['entries']]}, batch_n={res['batch_n']}")
    res = api("/api/quick/stats", {"make": "Solomake", "model": "Solomodel",
                                   "prices": ["5000", "6100", "7300"]})
    check("all-unique prices -> mode is none",
          res["now"]["mode"] is None, f"mode={res['now']['mode']}")
    check("first-time model -> no baseline, no fake numbers",
          res["baseline"] is None and res["changes"] is None and res["baseline_n"] == 0,
          f"baseline={res['baseline']}, changes={res['changes']}")

    print("\n== commit into the shared store ==")
    res = api("/api/quick/commit", {"make": "Testmake", "model": "Testmodel",
                                    "vehicle_type": "car",
                                    "prices": ["13000", "9000", "13,000", "poa"]})
    check("commit: 3 written, unparsable rejected with reason",
          res.get("ok") and res["committed"] == 3 and res["rejected"] == ["poa"],
          f"committed={res.get('committed')}, rejected={res.get('rejected')}")
    with Store(DB) as store:
        df = store.frame()
    qa = df[(df["source"] == "quick_add") & (df["capture_date"] == TODAY)]
    check("rows landed: source=quick_add, unique QA ids, empty odometer/state",
          len(qa) == 3 and qa["listing_id"].nunique() == 3
          and qa["listing_id"].str.startswith("QA-").all()
          and qa["odometer_km"].isna().all() and qa["state"].isna().all(),
          f"{len(qa)} rows, ids={list(qa['listing_id'])[:3]}")
    res = api("/api/quick/stats", {"make": "Testmake", "model": "Testmodel",
                                   "prices": []})
    check("post-commit stats: batch now in the stored pool (n=8, median 10750)",
          res["stored_n"] == 8 and approx(res["now"]["median"], 10750),
          f"stored_n={res['stored_n']}, median={res['now']['median']}")

    print("\n== existing analysis picks the rows up ==")
    dfx = add_segment_cols(df)
    summary = analyze.segment_summary(dfx, "model_line")
    row = summary[summary["model_line"] == "Testmake Testmodel"]
    check("segment_summary sees the merged pool",
          len(row) == 1 and int(row.iloc[0]["listings"]) == 8
          and approx(float(row.iloc[0]["median_price"]), 10750),
          f"listings={int(row.iloc[0]['listings']) if len(row) else '?'}, "
          f"median={float(row.iloc[0]['median_price']) if len(row) else '?'}")
    rc = cli_main(["--db", str(DB), "report", "--segment", "testmake",
                   "--no-charts"])
    check("existing CLI report runs over quick_add data", rc == 0, f"rc={rc}")

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
