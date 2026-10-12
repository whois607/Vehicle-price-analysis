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
  * generations from trim_rules.json: BMW chassis codes and Ford series
    codes (a series code only where the year fits: "XT" is a 1968 Falcon
    and a 2006 trim), the trim without its generation, and a missing body
    filled from the chassis code - never over a stored one;
  * an upload whose trim names are unlike the data's (a site writing
    "FG2 XR6 T") is imported and FLAGGED - on Bulk upload, the watch
    folder's runs and Import with preview - never refused; a model with
    too little data, or familiar names, raise nothing;
  * the Trims page: coverage per make, the names the rules leave without a
    generation, and every stored name with its group;
  * suggested shorthand (FG2 = FG MkII, FGX = FG X, XR6 T = XR6 Turbo) only
    where the data holds the long form - never BMW's "M"; approving writes
    ONLY the rules file (a copy here - the real one is checked unchanged),
    backed up first, and the cars join their group; removing undoes it;
  * every search finds the grouped names: Explore's keyword, Compare and
    old links, quick add's list and prices, the phone's old labels;
  * the car check falls back, when a trim has fewer than 8 priced cars (or
    there is no trim), to the model's cars within 2 years - only then, only
    with a year, never a whole model across every year, with no mileage
    curve, and says so; a big enough trim is judged exactly as before;
  * reading writes nothing.

Throwaway database only. ASCII output; exit 0 only if every check passes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from vpa import bulk, compare, explore, importui, phonebuild, quickadd, trims, trimspage
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
    # the nearby-years fallback: a thin trim, a car with no trim, and cars
    # outside the window that must NOT be pooled
    ("Holden", "Caprice", "V8", 2013, 5),
    ("Holden", "Caprice", "V8 Version C", 2014, 4),
    ("Holden", "Caprice", "V8", 2016, 2),
    ("Holden", "Caprice", "Auto", 2014, 1),
    ("Holden", "Caprice", "V8", 2009, 3),
    # generations by year table and badge
    ("Lexus", "LS", "LS460", 2013, 5),
    ("Lexus", "LS", "LS460 Version C", 2014, 4),
    ("Lexus", "LS", "LS500h Sports Luxury", 2019, 2),
    ("Lexus", "IS", "IS350 Version L", 2013, 1),
    ("Lexus", "IS", "IS350 F Sport", 2013, 1),
    ("Audi", "S5", "quattro", 2016, 1),
    # classics (35+ years old)
    ("Ford", "Falcon", "GT XY", 1971, 9),
    # generations
    ("Ford", "Falcon", "XT BF MkII", 2006, 2),
    ("Ford", "Falcon", "XT", 1968, 1),
    ("BMW", "3 Series", "330i M Sport G20 LCI II", 2024, 2),
    ("BMW", "4 Series", "428i Sport Line F36", 2015, 3),
    ("BMW", "4 Series", "428i Sport Line F32", 2016, 6),
    ("Ford Performance Vehicles", "GT", "Cobra BF MkII", 2007, 3),
    ("Ford Performance Vehicles", "GT", "BF MkII", 2007, 6),
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
        # one F36 stored with a body that disagrees with its chassis code:
        # the stored body must win
        st.conn.execute("""UPDATE vehicles SET body_type = 'coupe' WHERE key = (
            SELECT key FROM vehicles WHERE variant = '428i Sport Line F36' ORDER BY key LIMIT 1)""")
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
    check("Audi S5: 'quattro' and 'Auto quattro MY18' are one trim of 10 cars (B9, 2018)",
          grouped("Audi", "S5", "quattro B9") == {"quattro", "Auto quattro MY18"}
          and groups[("Audi", "S5", "quattro B9")] == 10
          and groups[("Audi", "S5", "edition one quattro B10")] == 2, f"{dict(groups['Audi'])}")
    check("Falcon: 'Mk II' and 'MkII' are one trim; FG MkII untouched",
          groups[("Ford", "Falcon", "XR6 Turbo BA MkII")] == 5
          and grouped("Ford", "Falcon", "XR6 FG MkII") == {"XR6 FG MkII"}
          and groups[("Ford", "Falcon", "XR6 FG MkII")] == 10, f"{dict(groups['Ford']['Falcon'])}")
    check("capitals: 'SportLine' + 'Sportline' shown as the more common spelling",
          groups.get(("Skoda", "Superb", "206TSI SportLine")) == 3
          and ("Skoda", "Superb", "206TSI Sportline") not in groups, f"{dict(groups['Skoda'])}")
    vette = df[df["model"] == "Corvette"].drop_duplicates("key")
    check("Corvette: 1960s 'Sting Ray' and 1968 'Stingray' are their eras (C2, C3); 'Auto MY86' is a C4",
          {"C2", "C3", "Stingray 3LT C8"} <= set(vette["variant"].dropna())
          and (vette[vette["variant_stored"] == "Auto MY86"]["variant"] == "C4").all(),
          f"{sorted(vette['variant'].dropna().unique())}")
    two = df.drop_duplicates("key").groupby(["make", "model", "variant"])["variant_stored"].nunique()
    expected_merges = {("Audi", "S5", "quattro B9"), ("BMW", "4 Series", "420i M Sport"),
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

    print("\n== generations ==")
    one2 = df.drop_duplicates("key")
    def gt(stored, year=None):
        r = one2[(one2["variant_stored"] == stored) & ((one2["year"] == year) if year else True)].iloc[0]
        un = lambda v: None if not isinstance(v, str) else v
        return (un(r["generation"]), un(r["trim_base"]))
    want = {("XT BF MkII", 2006): ("BF MkII", "XT"), ("XT", 1968): ("XT", None),
            ("XR6 FG MkII", None): ("FG MkII", "XR6"), ("XR8 FG X", None): ("FG X", "XR8"),
            ("XR6 Turbo BA Mk II", None): ("BA MkII", "XR6 Turbo"),
            ("330i M Sport G20 LCI II", None): ("G20 LCI II", "330i M Sport"),
            ("320i F30 Auto MY13.5", None): ("F30", "320i"),
            ("BF MkII", None): ("BF MkII", None), ("quattro", 2018): ("B9", "quattro"),
            ("quattro", 2016): ("B8/B9", "quattro"), ("Sting Ray Manual MY66", None): ("C2", None),
            ("Auto MY86", None): ("C4", None), ("LS460", 2013): ("XF40", "LS460"),
            ("IS350 Version L", None): ("XE20", "IS350 Version L"),
            ("IS350 F Sport", 2013): ("XE20/XE30", "IS350 F Sport"),
            ("LS500h Sports Luxury", None): ("XF50", "LS500h Sports Luxury")}
    got = {k: gt(*k) for k in want}
    bad = {k: v for k, v in got.items() if v != want[k]}
    check("generation and the trim without it, per brand rule",
          not bad, "; ".join(f"{k[0]} -> {v}" for k, v in list(got.items())[:4]) + (f" WRONG {bad}" if bad else ""))
    g20 = one2[one2["variant_stored"] == "330i M Sport G20 LCI II"]
    f36 = one2[one2["variant_stored"] == "428i Sport Line F36"]
    check("a missing body is filled from the chassis code; a stored one is kept",
          (g20["body_type"] == "sedan").all() and g20["body_type_stored"].isna().all()
          and sorted(f36["body_type"]) == ["coupe", "hatch", "hatch"]
          and int(f36["body_type_stored"].notna().sum()) == 1,
          f"G20 {set(g20['body_type'])}, F36 {sorted(f36['body_type'])}")

    print("\n== generations by year table ==")
    cars = df.drop_duplicates("key").set_index("key")
    lx = cars[cars["variant"] == "LS460 Version C XF40"].index[0]
    pl = explore.car_payload(DB, "Lexus", "LS", "LS460 Version C XF40", key=lx)
    check("a thin Lexus trim widens to its generation (XF40), not the LS500s",
          pl["level"] == "generation" and pl["label"] == "Lexus LS XF40" and pl["pool"]["n"] == 8,
          f"{pl['label']}, {pl['pool']['n']} others")
    check("an old link to 'Audi S5 quattro' opens every generation it now spans, pooled",
          set(trims.resolve_all(df, "Audi", "S5", "quattro")) == {"quattro B9", "quattro B8/B9"}
          and explore.detail_payload(DB, "Audi", "S5", "quattro")["head"]["vehicles"] == 11,
          f"{trims.resolve_all(df, 'Audi', 'S5', 'quattro')}")
    check("...and an old stored name finds exactly its own cars' group",
          trims.resolve_all(df, "Audi", "S5", "Auto quattro MY18") == ["quattro B9"]
          and trims.resolve_all(df, "Chevrolet", "Corvette", "Sting Ray") == ["C2"],
          "Auto quattro MY18 -> quattro B9; Sting Ray -> C2")
    typed_old = explore.car_payload(DB, "Audi", "S5", "quattro", price="60000", year="2018")
    check("a typed check under an old name lands on the generation its year gives",
          typed_old["label"] == "Audi S5 quattro B9", typed_old["label"])

    print("\n== classics ==")
    gt = cars[cars["variant_stored"] == "GT XY"].index[0]
    pg = explore.car_payload(DB, "Ford", "Falcon", "GT XY", key=gt)
    tg = explore.car_payload(DB, "Ford", "Falcon", "GT XY", odometer="90000", year="1971")
    check("a 35+ year-old car is ranked, but gets no mileage curve and no expected price",
          pg["usable"] and pg["subject"]["classic"] and not pg["fit"]["usable"]
          and "classic" in pg["fit"]["reason"] and pg["adjusted"] is None
          and not tg["estimate"]["usable"] and "classic (1971)" in tg["estimate"]["reason"],
          tg["estimate"]["reason"][:70])
    modern = explore.car_payload(DB, "Ford", "Falcon", "XR6 FG MkII", odometer="90000", year="2012")
    check("...a modern one still gets both", not modern["subject"]["classic"]
          and modern["estimate"]["usable"], modern["estimate"].get("basis"))

    print("\n== too few of a trim: the ladder ==")
    k36 = f36[f36["body_type_stored"].isna()]["key"].iloc[0]
    p36 = explore.car_payload(DB, "BMW", "4 Series", "428i Sport Line F36", key=k36)
    check("a thin trim first widens to the same trim of any generation, within the years",
          p36["level"] == "trim_years" and p36["label"] == "BMW 4 Series 428i Sport Line 2013-2017"
          and p36["pool"]["n"] == 8 and "of any generation" in (p36["fallback"] or {}).get("note", ""),
          f"{p36['label']}, {p36['pool']['n']} others")
    kc = one2[one2["variant_stored"] == "Cobra BF MkII"]["key"].iloc[0]
    pc = explore.car_payload(DB, "Ford Performance Vehicles", "GT", "Cobra BF MkII", key=kc)
    check("...and when that is thin too, to every trim of its generation",
          pc["level"] == "generation" and pc["label"] == "Ford Performance Vehicles GT BF MkII"
          and pc["pool"]["n"] == 8 and "every trim of that generation" in (pc["fallback"] or {}).get("note", ""),
          f"{pc['label']}, {pc['pool']['n']} others")

    print("\n== too few of a trim: the model's nearby years ==")
    cars = df.drop_duplicates("key").set_index("key")
    vc = cars[cars["variant"] == "V8 Version C"].index[0]
    p = explore.car_payload(DB, "Holden", "Caprice", "V8 Version C", key=vc)
    pf = p.get("fallback") or {"note": "(no fallback)", "trim_n": None, "trim": "?"}
    want = set(cars[(cars["model"] == "Caprice") & cars["year"].between(2012, 2016)].index) - {vc}
    got = {c["key"] for c in p["comparables"]} | {vc}
    check("a thin trim (3 others) with a year: every Caprice 2012-2016, labelled and said",
          p["level"] == "years" and p["label"] == "Holden Caprice 2012-2016"
          and p["pool"]["n"] == len(want) == 11 and pf["trim_n"] == 3
          and "Only 3 other priced V8 Version C" in pf["note"]
          and "rough guide" in pf["note"], pf["note"])
    check("...never the 2009 cars outside the window",
          not (set(cars[cars["year"] == 2009].index) & {c["key"] for c in p["comparables"]})
          and p["pool"]["n"] == 11, f"pool {p['pool']['n']} of {int((cars['model'] == 'Caprice').sum())} Caprice")
    check("...with no mileage curve (it would measure the trim mix) and no trim caveats",
          not p["fit"]["usable"] and "the mix" in p["fit"]["reason"] and p["caveats"] == []
          and p["adjusted"] is None and p["expected"] is None, p["fit"]["reason"][:50])
    nt = cars[cars["variant_stored"] == "Auto"].index[0]
    q = explore.car_payload(DB, "Holden", "Caprice", None, key=nt)
    qf = q.get("fallback") or {"note": q.get("reason") or "(no fallback)", "trim": "?"}
    check("a stored car with no trim is judged the same way, not refused",
          q.get("level") == "years" and q.get("usable") and qf["trim"] is None
          and qf["note"].startswith("No trim is recorded"), qf["note"][:60])
    t = explore.car_payload(DB, "Holden", "Caprice", None, odometer="120000", year="2014")
    e = t.get("estimate") or {}
    check("a typed model + year gets an expected range from those cars",
          t.get("level") == "years" and e.get("usable") and e.get("basis") == "nearest"
          and "these cars" in (e.get("why_not_curve") or "")
          and (t.get("fallback") or {}).get("note", "").startswith("No trim was picked"),
          f"{e.get('basis')} {e.get('lo')}-{e.get('hi')}" if e else t.get("reason", ""))
    a = explore.car_payload(DB, "Holden", "Caprice", None, price="30000")
    b = explore.car_payload(DB, "Holden", "Caprice", None, price="30000", year="2003")
    c = explore.car_payload(DB, "Holden", "Caprice", "V8 Version C", price="30000")
    check("no year, or too few nearby: refused or trim-only, exactly as before",
          a["level"] == "too broad" and "give the year" in a["reason"]
          and b["level"] == "too broad" and "fewer than 8" in b["reason"]
          and c["level"] == "trim" and not c["raw"]["usable"] and c["fallback"] is None,
          f"{a['reason'][:40]} | {b['reason'][:40]} | trim n={c['raw']['n']}")
    big = cars[cars["variant"] == "XR6 FG MkII"].index[0]
    r = explore.car_payload(DB, "Ford", "Falcon", "XR6 FG MkII", key=big)
    check("a trim with 8+ others is judged against its trim alone",
          r["level"] == "trim" and r["fallback"] is None and r["pool"]["n"] == 9, f"pool {r['pool']['n']}")
    other = cars[cars["variant"] == "V8"].index[0]
    w = explore.car_payload(DB, "Holden", "Caprice", "V8 Version C", key=other)
    check("a car asked for under a trim it is not in is still an error",
          w["ok"] is False, w.get("error", ""))

    print("\n== uploads worded differently ==")
    odd = ["FG2 XR6", "FG2 XR6 T", "FGX G6E T", "FGX XR8", "FG2 XR8", "XR6 FG MkII"]
    recs = [{"make": "Ford", "model": "Falcon", "variant": v, "year": 2012} for v in odd]
    w = trims.wording_check(df, recs)
    check("a page in shorthand is flagged, naming the new words",
          w["warn"] and w["checked"] == 6 and w["recognised"] == 1
          and {"FG2", "FGX", "T"} <= set(w["new_words"]), w["message"][:90])
    fam = [{"make": "Ford", "model": "Falcon", "variant": v, "year": 2012}
           for v in ["XR6 FG MkII", "XR6 Turbo BA Mk II", "XR8 FG X Auto", "XT BF MkII", "XR6 FG MkII"]]
    new = [{"make": "Holden", "model": "Commodore", "variant": f"SS VE{i}"} for i in range(9)]
    w2, w3 = trims.wording_check(df, fam), trims.wording_check(df, new)
    check("familiar names (in any spelling) and a model with too little data raise nothing",
          not w2["warn"] and w2["recognised"] == 5 and not w3["warn"] and w3["checked"] == 0,
          f"familiar {w2['recognised']}/{w2['checked']}, new model checked {w3['checked']}")
    shutil.copy(DB, WORK / "upload.db")
    up = WORK / "upload.db"
    csv = "listing_id,make,model,variant,year,price,odometer\n" + "\n".join(
        f"U-{i},Ford,Falcon,{v},2012,{15000 + i * 500},{120000 + i * 1000}" for i, v in enumerate(odd))
    r = bulk.run([{"name": "other-site.csv", "text": csv, "capture_date": "2026-09-20"}], up)
    f = r["files"][0]
    check("Bulk upload (and so the watch folder): imported, and flagged",
          r["wrote"] and f["ok"] and f["obs_new"] == 6 and f["wording"]["warn"],
          f"{f['obs_new']} written; {f['wording']['message'][:60]}")
    pv = importui.parse_payload({"name": "other-site.csv", "text": csv.replace("U-", "V-"),
                                 "capture_date": "2026-09-21"}, DB)
    check("Import with preview says so in its note, every row still ticked",
          pv["ok"] and "Trim names:" in (pv["note"] or "") and all(x["checked"] for x in pv["rows"]),
          (pv["note"] or "")[:70])

    print("\n== the Trims page ==")
    tp = trimspage.payload(DB)
    cov = {c["make"]: c for c in tp["coverage"]}
    um = {(u["make"], u["model"], u["variant"]) for u in tp["unmatched"]}
    nm = {(n["make"], n["stored"]): n for n in tp["names"]}
    check("coverage per make: BMW cars placed in a generation, Skoda none (no rules)",
          cov["BMW"]["generation_rules"] and cov["BMW"]["with_generation"]
          == int(df.drop_duplicates("key").query("make == 'BMW'")["generation"].notna().sum())
          and not cov["Skoda"]["generation_rules"], f"BMW {cov['BMW']['with_generation']}/{cov['BMW']['cars']}")
    check("the names left without a generation are listed (BMW 420i M Sport: no code)",
          ("BMW", "4 Series", "420i M Sport") in um and ("Ford", "Falcon", "XR6 FG MkII") not in um,
          f"{len(um)} listed")
    check("every stored name, with the group it lands in",
          nm[("Audi", "Auto quattro MY18")]["variant"] == "quattro B9"
          and nm[("Ford", "XT BF MkII")]["generation"] == "BF MkII"
          and sum(n["cars"] for n in tp["names"]) == df["key"].nunique(),
          f"{len(tp['names'])} names, {sum(n['cars'] for n in tp['names'])} cars")
    check("the page is complete (every placeholder filled)",
          "__" not in trimspage.PAGE.replace("__proto__", "") and 'id="trimsBtn"' in trimspage.PAGE,
          f"{len(trimspage.PAGE):,} bytes")

    print("\n== suggested shorthand ==")
    real_rules = trims.RULES_PATH
    real_hash = hashlib.sha256(real_rules.read_bytes()).hexdigest()
    copy = WORK / "rules.json"
    shutil.copy(real_rules, copy)
    trims.RULES_PATH, trims._RULES["sig"] = copy, None
    try:
        updf = read_frame(up)
        sg = {(x["from"], x["to"]): x for x in trims.suggestions(updf)}
        check("FG2 = FG MkII, FGX = FG X and T = Turbo are suggested, each with what changes",
              {("FG2", "FG MkII"), ("FGX", "FG X"), ("T", "Turbo")} <= set(sg)
              and sg[("FG2", "FG MkII")]["cars"] == 3
              and any(e["then"] == "XR6 FG MkII" for e in sg[("FG2", "FG MkII")]["examples"]),
              "; ".join(f"{a}={b} ({x['cars']})" for (a, b), x in sg.items()))
        check("nothing is suggested where the data has no long form (BMW 'M', every other trim)",
              trims.suggestions(df) == [], f"{len(trims.suggestions(df))} on the main data")
        bad1 = trims.approve_shorthand({"make": "Ford", "model": "Falcon", "from": "FG 2", "to": "x"}, WORK / "bk")
        bad2 = trims.approve_shorthand({"make": "Ford", "model": "Falcon", "from": "XR6", "to": "xr6"}, WORK / "bk")
        check("a malformed or empty shorthand is refused", not bad1["ok"] and not bad2["ok"],
              f"{bad1['error']} | {bad2['error']}")
        for frm, to in (("FG2", "FG MkII"), ("FGX", "FG X"), ("T", "Turbo")):
            r = trims.approve_shorthand({"make": "Ford", "model": "Falcon", "from": frm, "to": to}, WORK / "bk")
            assert r["ok"], r
        after = read_frame(up).drop_duplicates("key")
        moved = dict(zip(after["variant_stored"], after["variant"]))
        check("approved: the shorthand cars join their groups, in the usual word order",
              moved["FG2 XR6"] == "XR6 FG MkII" and moved["FGX G6E T"] == "G6E Turbo FG X"
              and moved["FG2 XR6 T"] == "XR6 Turbo FG MkII" and moved["FGX XR8"] == "XR8 FG X",
              f"FG2 XR6 T -> {moved['FG2 XR6 T']}; FGX G6E T -> {moved['FGX G6E T']}")
        bks = sorted((WORK / "bk").glob("trim_rules_*.json"))
        check("the rules file was backed up before each change, and still parses",
              len(bks) == 3 and json.loads(copy.read_text(encoding="utf-8"))["shorthand"][-1]["from"] == "T",
              f"{len(bks)} backups")
        w = trims.wording_check(read_frame(DB), [{"make": "Ford", "model": "Falcon", "variant": v, "year": 2012}
                                                 for v in odd])
        # (judged against the main data, which simply has no XR6 Turbo FG
        # MkII: those stay new TRIMS - but no longer new WORDS)
        check("...and the same page's shorthand no longer reads as new words",
              w["recognised"] == 3 and not {"FG2", "FGX", "T"} & set(w["new_words"]),
              f"{w['recognised']}/{w['checked']} recognised; new words {w['new_words']}")
        r = trims.remove_shorthand({"make": "Ford", "model": "Falcon", "from": "T"}, WORK / "bk")
        again = dict(zip(*[read_frame(up).drop_duplicates("key")[c] for c in ("variant_stored", "variant")]))
        check("removing one takes it straight back out", r["ok"] and again["FG2 XR6 T"] == "XR6 T FG MkII"
              and ("T", "Turbo") in {(x["from"], x["to"]) for x in trims.suggestions(read_frame(up))},
              f"FG2 XR6 T -> {again['FG2 XR6 T']}")
        trimspage.dismiss(up, "Ford|Falcon|T|Turbo", True)
        check("'Not this' hides a suggestion (kept beside the database, not in the rules)",
              ("T", "Turbo") not in {(x["from"], x["to"]) for x in trims.suggestions(
                  read_frame(up), trimspage.dismissed(up))}
              and (WORK / "trim_dismissed.json").exists(), "dismissed")
    finally:
        trims.RULES_PATH, trims._RULES["sig"] = real_rules, None
    check("the real trim_rules.json was never touched",
          hashlib.sha256(real_rules.read_bytes()).hexdigest() == real_hash, real_hash[:12])

    print("\n== searching with the new names ==")
    kw = explore.detail_payload(DB, "Audi", "S5", None, keyword="B9")
    kw2 = explore.detail_payload(DB, "Audi", "S5", None, keyword="MY18")
    check("Explore's keyword finds a generation (B9) and a stored word (MY18, from the title)",
          # 11 = the 10 B9s and the 2016 car that may be one ("B8/B9")
          kw["head"]["vehicles"] == 11 and kw2["head"]["vehicles"] == 4,
          f"B9: {kw['head']['vehicles']} cars, MY18: {kw2['head']['vehicles']}")
    qm = {m["line"] for m in quickadd.models_payload(DB)["models"]}
    check("quick add lists the grouped trims, not each stored spelling",
          "Audi S5 quattro B9" in qm and "Audi S5 Auto quattro MY18" not in qm
          and "Ford Falcon XR6 Turbo BA MkII" in qm, f"{len(qm)} lines")
    check("...and its stored prices for an old name are every group it went to",
          len(quickadd._stored_prices(DB, "Audi", "S5", "quattro"))
          == int(df[(df["model"] == "S5") & df["variant"].isin(["quattro B9", "quattro B8/B9"])]["price_aud"].notna().sum()),
          f"{len(quickadd._stored_prices(DB, 'Audi', 'S5', 'quattro'))} prices")
    sels = {lab: {} for lab in compare.selection_index(df)}
    al = phonebuild._aliases(df, sels)
    check("the phone copy carries old labels -> new ones (links saved on the phone)",
          al.get("Audi S5 Auto quattro MY18") == "Audi S5 quattro B9"
          and al.get("Ford Falcon XR6 Turbo BA Mk II") == "Ford Falcon XR6 Turbo BA MkII"
          and all(v in sels for v in al.values()) and not (set(al) & set(sels)),
          f"{len(al)} old labels")
    cp = compare.compare_payload(DB, ["Audi S5 Auto quattro MY18", "Chevrolet Corvette Auto MY86"])
    check("Compare finds old labels too", not cp["unknown"]
          and [c["label"] for c in cp["columns"]] == ["Audi S5 quattro B9", "Chevrolet Corvette C4"],
          f"{[c['label'] for c in cp['columns']]}")

    check("reading wrote nothing", db_hash() == h0, h0)
    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
