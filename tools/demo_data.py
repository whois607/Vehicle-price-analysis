"""A demo database of made-up listings - to try the app without collecting
anything, and for the screenshots in the README.

    python tools/demo_data.py            build it, then open the app on it
    python tools/demo_data.py --build    build only

Everything is invented: the prices, odometers and dates come from a seeded
random generator, the "site" is democars.example (a reserved, unreachable
domain), the ad ids are DEMO-AD-..., and there are no photos. It is written
to tests/synthetic/demo/ with its own site file, so it never touches
data/ or your private sites.json.
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEMO = ROOT / "tests" / "synthetic" / "demo"
DB = DEMO / "data" / "vpa.db"
SITES = DEMO / "sites.json"

#: make, model, trim, years, typical asking price (mid year, typical km),
#: % per model year, % per 10,000 km, cars
LINES = [
    ("Ford", "Falcon", "XR6 FG X", (2015, 2016), 24000, 6, 2.5, 46),
    ("Ford", "Falcon", "XR6 Turbo FG X", (2015, 2016), 36000, 6, 2.8, 38),
    ("Ford", "Falcon", "G6E Turbo FG X", (2015, 2016), 31000, 6, 2.6, 22),
    ("Ford", "Falcon", "XR8 FG X", (2015, 2016), 52000, 4, 2.2, 14),
    ("Ford", "Falcon", "XR6 FG MkII", (2011, 2014), 14500, 7, 2.4, 40),
    ("Holden", "Commodore", "SS VF", (2013, 2017), 33000, 7, 2.6, 44),
    ("Holden", "Commodore", "SV6 VF", (2013, 2017), 19500, 8, 2.4, 36),
    ("Holden", "Commodore", "Calais V VF", (2014, 2017), 24500, 8, 2.4, 18),
    ("Toyota", "Hilux", "SR5", (2016, 2023), 46000, 6, 2.0, 52),
    ("Toyota", "Hilux", "Workmate", (2016, 2023), 31000, 6, 2.0, 28),
    ("Mazda", "CX-5", "Maxx Sport", (2017, 2023), 29000, 7, 2.2, 34),
    ("Mazda", "CX-5", "Akera", (2018, 2023), 39000, 7, 2.2, 16),
    ("BMW", "3 Series", "330i M Sport G20", (2019, 2023), 55000, 8, 3.0, 20),
    ("BMW", "3 Series", "320i Luxury Line F30", (2015, 2018), 23000, 9, 2.8, 18),
]
STATES = ["NSW"] * 5 + ["VIC"] * 4 + ["QLD"] * 3 + ["SA", "WA", "WA", "TAS", "ACT"]
DAYS = [date(2026, 6, 6) + timedelta(days=7 * i) for i in range(16)]       # weekly, Jun-Sep
TODAY = DAYS[-1] + timedelta(days=4)


def _site_file() -> dict:
    return {"_note": "The demo data's made-up site - see tools/demo_data.py.",
            "sites": [{"source": "democars", "domains": ["democars.example"],
                       "address": "https://www.democars.example", "ad_paths": ["/cars/"]}],
            "default_source": "democars", "id_prefixes": ["DEMO-AD-"],
            "search_site": "democars.example"}


def build() -> Path:
    from vpa.schema import make_record
    from vpa.store import Store
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DB.parent.mkdir(parents=True)
    SITES.write_text(json.dumps(_site_file(), indent=1), encoding="utf-8")
    rnd = random.Random(2026)
    recs, n = [], 0
    for mk, mo, va, (y0, y1), typical, pct_year, pct_10k, cars in LINES:
        drift = rnd.uniform(-0.012, 0.006)                # the trim's own trend, per week
        for _ in range(cars):
            n += 1
            lid = f"DEMO-AD-{40000 + n * 7}"
            year = rnd.randint(y0, y1)
            age = TODAY.year - year
            km = max(4000, int(rnd.gauss(15000 * age + 8000, 6000 + 3500 * age)) // 100 * 100)
            if rnd.random() < 0.15:
                km = round(km, -3)                         # some sellers round
            seller = "private" if rnd.random() < 0.4 else "dealer"
            mid_year = (y0 + y1) / 2
            mid_km = 15000 * (TODAY.year - mid_year) + 8000
            base = (typical * math.exp(pct_year / 100 * (year - mid_year))
                    * math.exp(-pct_10k / 100 * (km - mid_km) / 10000))
            base *= math.exp(rnd.gauss(0, 0.08)) * (0.95 if seller == "private" else 1.0)
            base = max(4500, base)
            # a third already listed at the first capture, the rest arriving
            # week by week; most stay a few weeks, some much longer
            first = 0 if rnd.random() < 0.3 else rnd.randrange(1, len(DAYS) - 1)
            stay = max(1, int(rnd.expovariate(1 / 5)) + 1)
            gear = rnd.choice(["Auto", "Auto", "Auto", "Manual"]) if mk != "Toyota" else "Auto"
            state = rnd.choice(STATES)
            price = base
            for i, d in enumerate(DAYS[first:first + stay]):
                if i and rnd.random() < 0.18:
                    price *= rnd.uniform(0.93, 0.985)      # a price drop
                p = int(price * math.exp(drift * (DAYS.index(d))) // 10 * 10 - 10)
                slug = f"{year}-{mk}-{mo}-{va}".lower().replace(" ", "-")
                recs.append(make_record({
                    "listing_id": lid, "make": mk, "model": mo, "variant": va, "year": year,
                    "price": p, "odometer": km + 120 * i, "state": state, "seller_type": seller,
                    "body_type": "Ute" if mo == "Hilux" else ("SUV" if mo == "CX-5" else "Sedan"),
                    "title": f"{year} {mk} {mo} {va} {gear}",
                    "url": f"/cars/details/{slug}/{lid}/", "capture_date": d.isoformat()},
                    source="democars"))
    with Store(DB) as st:
        st.ingest([r for r in recs if r], source_file="(demo data)", adapter="demo")
        keys = [r[0] for r in st.conn.execute(
            "SELECT v.key FROM vehicles v JOIN observations o ON o.key = v.key "
            "WHERE v.variant = 'XR6 Turbo FG X' GROUP BY v.key ORDER BY MIN(o.price_aud) LIMIT 3")]
        for k in keys:
            st.conn.execute("INSERT INTO starred (key, starred_at, note) VALUES (?, ?, ?)",
                            (k, f"{DAYS[-3].isoformat()} 19:30:00", "demo"))
        st.conn.commit()
        s = st.status()
    print(f"demo data: {s['vehicles']} cars, {s['observations']} sightings -> {DB}")
    return DB


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    build()
    if "--build" in argv:
        return 0
    env = dict(os.environ, VPA_SITES=str(SITES))
    print("serving the demo at http://127.0.0.1:8873 (made-up data; Ctrl+C to stop)")
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "vpa", "--db", str(DB), "import-ui",
                           "--port", "8873"], cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
