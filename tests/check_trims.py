"""Trim names as the analysis groups them (vpa/trims.py).

    python -m tests.check_trims      (from vehicle-price-analysis/)

The database keeps every trim exactly as imported; the analysis frame groups
them under a derived name, worked out on read. Checked here:

  * the frame keeps every row, key, price and odometer, and `variant_stored`
    is exactly what the database holds - read independently through SQL;
  * the wording fixes: gearbox, model-year and trailing drive/body words
    and "Mk II" spellings are folded away, capitals/hyphens are one trim
    under its most common spelling - and nothing else merges (spaces count:
    "Sting Ray" is not "Stingray"); applying them twice changes nothing;
  * gearbox / drive / model year land in their own columns, and the car
    check compares a manual with the other manuals;
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
    ("Ford", "Mustang", "GT FN Auto RWD MY21.5", 2021, 9),
    ("Ford", "Mustang", "GT FN Manual RWD MY22.25", 2022, 9),
]

#: stored name -> the name it must be grouped under (None: no trim)
CLEAN = {
    "Auto quattro MY18": "quattro", "Manual quattro": "quattro",
    "edition one Auto quattro MY25": "edition one quattro",
    "320i F30 Auto MY13.5": "320i F30", "420i M Sport Auto R": "420i M Sport",
    "XR6 Turbo BA Mk II": "XR6 Turbo BA MkII", "GT F 351 FG MK II": "GT F 351 FG MkII",
    "XR8 FG Manual Super Cab": "XR8 FG", "GT FN Auto RWD MY22.25": "GT FN",
    "Dark Horse FO Manual MY24.50": "Dark Horse FO",
    "206TSI SportLine Auto 4x4 MY18.5": "206TSI SportLine",
    "Stingray 3LT R Auto MY26": "Stingray 3LT R", "E-Ray 3LZ Auto AWD MY24": "E-Ray 3LZ",
    "Sting Ray Manual MY66": "Sting Ray", "Auto MY86": None, "Auto": None, "  ": None,
    # left exactly as they are
    "Golf R": "Golf R", "XR6 FG MkII": "XR6 FG MkII", "FG X": "FG X",
    "M340i xDrive G20 LCI II": "M340i xDrive G20 LCI II",
    "LS460 Version U I Package": "LS460 Version U I Package", "330i M Sport G20 LCI II":
    "330i M Sport G20 LCI II", "XR6 50th Anniversary FG": "XR6 50th Anniversary FG",
}


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
    print("\n== the wording fixes ==")
    bad = {k: trims.clean(k) for k, v in CLEAN.items() if trims.clean(k) != v}
    check("each stored name -> the trim it belongs to (and the ones to leave alone are left)",
          not bad, f"{len(CLEAN)} names" + (f"; WRONG {bad}" if bad else ""))
    names = [n for n in df["variant_stored"].dropna().unique()] + list(CLEAN)
    check("applying the fixes twice changes nothing",
          all(trims.clean(trims.clean(n)) == trims.clean(n) for n in names), f"{len(names)} names")
    def grouped(mk, mo, va):
        return set(df[(df["make"] == mk) & (df["model"] == mo)
                      & (df["variant"] == va)]["variant_stored"].unique())
    groups = df.drop_duplicates("key").groupby(["make", "model", "variant"])["key"].nunique()
    check("Audi S5: 'quattro' and 'Auto quattro MY18' are one trim of 10 cars",
          grouped("Audi", "S5", "quattro") == {"quattro", "Auto quattro MY18"}
          and groups[("Audi", "S5", "quattro")] == 10
          and groups[("Audi", "S5", "edition one quattro")] == 2, f"{dict(groups['Audi'])}")
    check("Falcon: 'Mk II' and 'MkII' are one trim; FG MkII untouched",
          groups[("Ford", "Falcon", "XR6 Turbo BA MkII")] == 5
          and grouped("Ford", "Falcon", "XR6 FG MkII") == {"XR6 FG MkII"}
          and groups[("Ford", "Falcon", "XR6 FG MkII")] == 10, f"{dict(groups['Ford']['Falcon'])}")
    check("capitals: 'SportLine' + 'Sportline' shown as the more common spelling",
          groups.get(("Skoda", "Superb", "206TSI SportLine")) == 3
          and ("Skoda", "Superb", "206TSI Sportline") not in groups, f"{dict(groups['Skoda'])}")
    vette = df[df["model"] == "Corvette"].drop_duplicates("key")
    check("Corvette: 'Sting Ray' (1960s) and 'Stingray' stay apart; 'Auto MY86' has no trim",
          {"Sting Ray", "Stingray", "Stingray 3LT"} <= set(vette["variant"].dropna())
          and vette[vette["variant_stored"] == "Auto MY86"]["variant"].isna().all(),
          f"{sorted(vette['variant'].dropna().unique())}")
    two = df.drop_duplicates("key").groupby(["make", "model", "variant"])["variant_stored"].nunique()
    expected_merges = {("Audi", "S5", "quattro"), ("BMW", "4 Series", "420i M Sport"),
                       ("BMW", "3 Series", "320i F30"), ("Ford", "Falcon", "XR6 Turbo BA MkII"),
                       ("Skoda", "Superb", "206TSI SportLine"), ("Ford", "Mustang", "GT FN")}
    check("nothing else merged", set(two[two > 1].index) == expected_merges,
          f"{sorted(set(two[two > 1].index) ^ expected_merges) or 'only the six expected'}")

    print("\n== gearbox, drive and model year in their own columns ==")
    one = df.drop_duplicates("key").set_index("variant_stored")
    s5 = one.loc["Auto quattro MY18"].iloc[0]
    st = one.loc["GT FN Manual RWD MY22.25"].iloc[0]
    check("read from the stored trim and the title",
          (s5["gearbox"], s5["drive"], s5["model_year"]) == ("auto", "awd", 2018)
          and (st["gearbox"], st["drive"], st["model_year"]) == ("manual", "rwd", 2022.25),
          f"S5 {s5['gearbox']}/{s5['drive']}/{s5['model_year']}, Mustang "
          f"{st['gearbox']}/{st['drive']}/{st['model_year']}")
    man = df[(df["variant_stored"] == "GT FN Manual RWD MY22.25")].iloc[-1]
    cp = explore.car_payload(DB, "Ford", "Mustang", "GT FN", key=man["key"])
    g = cp["splits"]["gearbox"]
    want = df[(df["model"] == "Mustang") & (df["gearbox"] == "manual") & (df["key"] != man["key"])]
    want = want.sort_values("capture_date", kind="stable").drop_duplicates("key", keep="last")
    check("the car check judges a manual against the other manuals of its trim",
          cp["usable"] and cp["pool"]["n"] == 17 and g["value"] == "manual" and g["n"] == 8
          and g["median"] == float(want["price_aud"].median()),
          f"trim pool {cp['pool']['n']}, manuals {g['n']} at {g['median']}")
    typed = explore.car_payload(DB, "Ford", "Mustang", "GT FN", price="30000")
    check("...and a typed car, whose gearbox is not known, gets no gearbox row",
          typed["splits"]["gearbox"]["value"] is None, typed["splits"]["gearbox"]["reason"])

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
              derived == r["variant"] and a["head"] == b["head"]
              and a["vehicles"] == b["vehicles"], f"{r['variant_stored']!r} -> {derived!r}")
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
