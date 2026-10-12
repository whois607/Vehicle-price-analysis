"""Trim names as the analysis groups them (vpa/trims.py).

    python -m tests.check_trims      (from vehicle-price-analysis/)

The database keeps every trim exactly as imported; the analysis frame groups
them under a derived name, worked out on read. Checked here:

  * the frame keeps every row, key, price and odometer, and `variant_stored`
    is exactly what the database holds - read independently through SQL;
  * a trim asked for by its STORED name (an old link, a saved comparison)
    finds the trim it is grouped under, on Explore, the car check and
    Compare - and Compare's list of selections gains no duplicate rows;
  * the saved record notices cars that move group, and cars whose stored
    data changed (which must never happen);
  * reading writes nothing.

Throwaway database only. ASCII output; exit 0 only if every check passes.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

from vpa import compare, explore, trims
from vpa.schema import make_record
from vpa.store import Store, read_frame

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "trims_work"
DB = WORK / "trims_test.db"
RESULTS: list[tuple[bool, str]] = []
DAY_A, DAY_B = "2026-09-01", "2026-09-15"


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


#: (make, model, STORED trim, year, how many cars). Stored names are written
#: straight into the database afterwards, as older imports left them.
FLEET = [
    ("Ford", "Falcon", "XR6 FG MkII", 2012, 10),
    ("Ford", "Falcon", "XR6 Turbo BA MkII", 2005, 3),
    ("Ford", "Falcon", "XR6 Turbo BA Mk II", 2005, 2),
    ("Ford", "Falcon", "XR8 FG X", 2015, 4),
    ("Audi", "S5", "quattro", 2018, 6),
    ("Audi", "S5", "Auto quattro MY18", 2018, 4),
    ("Audi", "S5", "edition one Auto quattro MY25", 2025, 2),
    ("BMW", "4 Series", "420i M Sport", 2016, 3),
    ("BMW", "4 Series", "420i M Sport Auto R", 2016, 2),
    ("BMW", "3 Series", "320i F30 Auto MY13.5", 2013, 3),
    ("BMW", "3 Series", "320i F30", 2013, 3),
    ("Chevrolet", "Corvette", "Auto MY86", 1986, 2),
    ("Chevrolet", "Corvette", "Sting Ray Manual MY66", 1966, 1),
    ("Chevrolet", "Corvette", "Stingray Manual MY68", 1968, 1),
    ("Chevrolet", "Corvette", "Stingray 3LT Auto MY24", 2024, 3),
    ("Lexus", "IS", "IS350 F Sport", 2015, 4),
    ("Skoda", "Superb", "206TSI SportLine", 2021, 2),
    ("Skoda", "Superb", "206TSI Sportline", 2021, 1),
    ("Toyota", "86", None, 2017, 2),
]


def seed() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    raw, stored, n = [], [], 0
    for mk, mo, va, yr, count in FLEET:
        for i in range(count):
            n += 1
            lid = f"T-{n}"
            for k, day in enumerate((DAY_A, DAY_B)):
                raw.append({"listing_id": lid, "make": mk, "model": mo,
                            "variant": "x", "year": yr,
                            "price": 20000 + n * 250 - k * 100,
                            "odometer": 60000 + n * 3000, "state": "NSW",
                            "seller_type": "dealer", "capture_date": day,
                            "raw_title": f"{yr} {mk} {mo} {va or ''}".strip()})
            stored.append((lid, va))
    with Store(DB) as st:
        st.ingest([r for r in (make_record(x, "carsales") for x in raw) if r])
        st.conn.executemany("UPDATE vehicles SET variant = ? WHERE listing_id = ?",
                            [(va, lid) for lid, va in stored])
        st.conn.commit()


def db_hash() -> str:
    with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as c:
        rows = [c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("vehicles", "observations")]
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:16]


def main() -> int:
    seed()
    h0 = db_hash()
    df = read_frame(DB)

    print("\n== the frame keeps the data as stored ==")
    with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as c:
        sql = c.execute("""SELECT o.key, o.capture_date, o.price_aud, o.odometer_km,
                                  v.make, v.model, v.variant
                           FROM observations o JOIN vehicles v ON v.key = o.key
                           ORDER BY o.capture_date, o.key""").fetchall()
    got = [(r.key, r.capture_date.strftime("%Y-%m-%d"), r.price_aud, r.odometer_km,
            r.make, r.model, None if r.variant_stored is None or r.variant_stored != r.variant_stored
            else r.variant_stored) for r in df.itertuples(index=False)]
    check("every row, key, price, km, make and model, and the stored trim, as SQL reads them",
          got == sql, f"{len(got)} rows vs {len(sql)}")
    check("the derived trim is the clean() of the stored one, car by car",
          all((trims.clean(a) == b) or (trims.clean(a) is None and b != b)
              for a, b in zip(df["variant_stored"], df["variant"])),
          f"{df['variant'].nunique()} grouped names from {df['variant_stored'].nunique()} stored")

    print("\n== a trim asked for by its stored name ==")
    moved = df.dropna(subset=["variant_stored"])
    moved = moved[moved["variant_stored"] != moved["variant"]]
    if moved.empty:
        check("(no rules yet: every stored name is its own group)",
              trims.resolve(df, "Audi", "S5", "Auto quattro MY18") == "Auto quattro MY18",
              "resolve leaves it as it is")
    else:
        r = moved.iloc[0]
        derived = trims.resolve(df, r["make"], r["model"], r["variant_stored"])
        a = explore.detail_payload(DB, r["make"], r["model"], r["variant_stored"])
        b = explore.detail_payload(DB, r["make"], r["model"], derived)
        check("Explore: the old name opens the trim it is grouped under",
              derived == r["variant"] and a["label"] == b["label"]
              and a.get("stats") == b.get("stats"), f"{r['variant_stored']!r} -> {derived!r}")
        c1 = explore.car_payload(DB, r["make"], r["model"], r["variant_stored"], key=r["key"])
        c2 = explore.car_payload(DB, r["make"], r["model"], derived, key=r["key"])
        check("the car check too", c1 == c2, c1.get("label", "")[:60])
        lab = f"{r['make']} {r['model']} {r['variant_stored']}"
        cp = compare.compare_payload(DB, [lab])
        check("Compare: a saved comparison under the old name still finds it",
              not cp["unknown"] and cp["columns"][0]["label"].endswith(str(derived)),
              cp["columns"][0]["label"] if cp["columns"] else cp["unknown"])
    check("an unknown trim name stays unknown",
          trims.resolve(df, "Audi", "S5", "RS9 nonsense") == "RS9 nonsense"
          and explore._select(df, "Audi", "S5", "RS9 nonsense").empty, "matches nothing")
    cat = compare.catalogue_payload(DB)
    labels = [x["label"] for x in cat["rows"]]
    check("Compare's list of selections has one row per grouped trim, no aliases",
          len(labels) == len(set(labels))
          and sum(1 for x in cat["rows"] if x["level"] == "trim")
          == df.dropna(subset=["variant"]).groupby(["make", "model", "variant"]).ngroups,
          f"{len(labels)} rows")

    print("\n== the saved record ==")
    rec = trims.record(df)
    same = trims.compare_records(rec, trims.record(read_frame(DB)))
    check("a record compared with itself: nothing moved, nothing changed",
          not same["moved"] and not same["data_changed"] and not same["cars_missing"],
          f"{len(rec['cars'])} cars, {len(rec['groups'])} groups")
    fake = {"cars": {k: v[:3] + ["something else"] for k, v in list(rec["cars"].items())[:3]}
            | {k: v for k, v in list(rec["cars"].items())[3:]}, "groups": rec["groups"]}
    tampered = {"cars": dict(rec["cars"]), "groups": rec["groups"]}
    k0 = next(iter(tampered["cars"]))
    tampered["cars"][k0] = ["Holden"] + tampered["cars"][k0][1:]
    d1, d2 = trims.compare_records(fake, rec), trims.compare_records(tampered, rec)
    check("...and it reports cars that moved group, and stored data that changed",
          sum(n for *_, n in d1["moved"]) == 3 and d2["data_changed"] == [k0],
          f"{sum(n for *_, n in d1['moved'])} moved; {d2['data_changed']}")

    check("reading wrote nothing", db_hash() == h0, h0)
    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
