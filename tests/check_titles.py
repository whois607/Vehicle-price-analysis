"""Splitting a listing title into make / model / variant.

Run:  $py -m tests.check_titles

The thing under test is `vpa/adapters/titles.py`, which both capture paths
share. Three properties matter and each has its own section:

  1. the gearbox/drive tail is removed, and NOTHING else is;
  2. a badge that legitimately ends in a letter survives intact
     (Golf R, Civic Type R, Model S, A45 S, i30 N) - this is the section
     that stops a "clean up the trailing letter" rule from eating badges;
  3. the paste path and the saved-page path agree on the same car, because
     disagreeing is what put one BMW trim into three pools in the real
     database.

The corpus is Australian-market only. A badge that never sold here would
make a passing test meaningless.
"""
from __future__ import annotations

import sys

from vpa.adapters import htmlpage, pastetext
from vpa.adapters.titles import is_badge_line, split_make_model, strip_tail, take_model
from vpa.schema import make_record

_passed = _failed = 0


def check(name: str, ok: bool, detail: str) -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [PASS] {name}: {detail}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}: {detail}")


def table(name: str, cases: list[tuple], fn) -> None:
    """One check per table, reporting the first failure in full. A check per
    ROW would bury the summary under a hundred lines of PASS."""
    bad = [(src, want, fn(src)) for src, want in cases if fn(src) != want]
    check(name, not bad,
          f"{len(cases) - len(bad)}/{len(cases)} - " +
          ("all as specified" if not bad else
           "; ".join(f"{s!r} -> {g!r} (wanted {w!r})" for s, w, g in bad[:4])))


def run_checks() -> int:
    print("== the gearbox/drive tail comes off ==")
    table("carsales tails are removed, whatever their shape", [
        ("LS460 Version U I Package Auto R", "LS460 Version U I Package"),
        ("LS500 F Sport Auto R", "LS500 F Sport"),
        ("LS500h Sports Luxury Auto R", "LS500h Sports Luxury"),
        ("XR6 Turbo FG X Auto", "XR6 Turbo FG X"),
        ("420i M Sport Auto R", "420i M Sport"),
        ("SR5 Auto 4x4", "SR5"),
        ("Wildtrak Auto 4x4", "Wildtrak"),
        ("Raptor Auto 4x4", "Raptor"),
        ("WRX STI Premium Man 6sp", "WRX STI Premium"),
        ("GT Sport Spts Auto 6sp", "GT Sport"),
        ("Akera Auto", "Akera"),
        ("Highlander Auto AWD", "Highlander"),
        ("SS-V Redline Man", "SS-V Redline"),
        ("Calais V Auto", "Calais V"),
    ], strip_tail)

    print("\n== a badge that ends in a letter is NOT a drive code ==")
    # The whole point. A rule that strips a trailing letter reads every one
    # of these as a transmission suffix and destroys the badge.
    table("badges ending in a letter survive untouched", [
        ("Golf R", "Golf R"),
        ("Civic Type R", "Civic Type R"),
        ("Model S", "Model S"),
        ("A45 S", "A45 S"),
        ("C63 S", "C63 S"),
        ("i30 N", "i30 N"),
        ("Cooper S", "Cooper S"),
        ("Megane RS", "Megane RS"),
        ("Focus RS", "Focus RS"),
        ("Fiesta ST", "Fiesta ST"),
        ("Navara ST-X", "Navara ST-X"),
        ("X-Trail Ti-L", "X-Trail Ti-L"),
        ("Lancer Evolution X", "Lancer Evolution X"),
        ("Carrera 4S", "Carrera 4S"),
        ("Amarok W580", "Amarok W580"),
        ("Stinger GT", "Stinger GT"),
    ], strip_tail)
    table("...and they still lose a REAL tail when one is appended", [
        ("Golf R Auto 4", "Golf R"),
        ("Civic Type R Man", "Civic Type R"),
        ("A45 S Auto 4", "A45 S"),
        ("i30 N Man 6sp", "i30 N"),
        ("Cooper S Auto", "Cooper S"),
        ("Megane RS Man", "Megane RS"),
    ], strip_tail)
    check("'Sport' is never treated as a gearbox word",
          strip_tail("420i M Sport") == "420i M Sport"
          and strip_tail("Maxx Sport") == "Maxx Sport"
          and strip_tail("Sports Luxury") == "Sports Luxury",
          "'M Sport', 'Maxx Sport' and 'Sports Luxury' are badges, and "
          "'Spts Auto' is a gearbox - only the second is stripped")
    check("a badge letter to the LEFT of the gearbox is never eaten",
          strip_tail("Golf R Auto 4") == "Golf R"
          and strip_tail("Civic Type R Man") == "Civic Type R"
          and strip_tail("F Auto R") == "F"
          and strip_tail("Auto R") == "" and strip_tail("") == "",
          "'Golf R Auto 4' and 'Auto R' both end in a run of descriptor "
          "words; the tail starts at the gearbox word, so only the second "
          "loses everything")
    check("a leading gearbox word means the badge field was blank",
          strip_tail("Auto quattro MY23") == "quattro"
          and strip_tail("Auto") == "" and strip_tail("Manual MY60") == "",
          "Audi 'Auto quattro MY23' -> 'quattro'; a Corvette listed as "
          "'Manual MY60' has no trim at all rather than a trim called Manual")

    print("\n== chained and long-form tails come off in one piece ==")
    # Every case below is one an adversarial pass produced against an earlier
    # version of this rule, which stripped ONE token per pass and so halted
    # the moment a speed count or a model-year code sat between the gearbox
    # word and the drive code.
    table("a gearbox run is removed whole, not one token at a time", [
        ("Trackhawk WK MY19 Spts Auto 8sp 4x4", "Trackhawk WK"),
        ("GT CK MY20 Spts Auto 8sp R", "GT CK"),
        ("GS350 Sports Luxury 8 Sp Automatic R", "GS350 Sports Luxury"),
        ("330i M Sport G20 Spts Auto 8sp", "330i M Sport G20"),
        ("M135i F20 Man 6sp", "M135i F20"),
        ("C200 W205 5 SP AUTO", "C200 W205"),
        ("A45 4MATIC W176 Spts Auto Dual Clutch 4", "A45 4MATIC W176"),
        ("GT BD MY21 D-CT F", "GT BD"),
        ("Ascent Sport Hybrid ZWE211R CVT F", "Ascent Sport Hybrid ZWE211R"),
        ("Workmate VDJ79R Man 4x4", "Workmate VDJ79R"),
        ("xDrive30d M Sport G05 Auto 4x4", "xDrive30d M Sport G05"),
        ("M340i xDrive G20 Auto 4", "M340i xDrive G20"),
        ("220d M Sport F22 Auto R", "220d M Sport F22"),
        ("SR5 Auto 4X4", "SR5"),
    ], strip_tail)
    check("a model-year code goes whichever side of the gearbox it sits",
          strip_tail("Wildtrak PX MkIII Auto 4x4 MY22") == "Wildtrak PX MkIII"
          and strip_tail("Wildtrak PX MkIII Auto 4x4") == "Wildtrak PX MkIII"
          and strip_tail("45 TFSI quattro sport FY MY21 Auto 4x4")
              == "45 TFSI quattro sport FY",
          "the same Ranger written two ways gives one trim; MY## duplicates "
          "the year column and otherwise splits a trim per model year")
    table("a badge word that merely CONTAINS a gearbox word is safe", [
        ("Sportivo MCV36R Auto F", "Sportivo MCV36R"),   # 'Sportivo' vs 'Sport'
        ("Rugged X GUN126R Auto 4x4", "Rugged X GUN126R"),  # lone X vs '4X4'
        ("GR Sport GUN126R Auto 4x4", "GR Sport GUN126R"),
        ("IS200 Sports Auto R", "IS200 Sports"),   # a real Lexus badge
        ("LS460 Version S I Auto R", "LS460 Version S I"),  # lone trailing I
        ("RX450h Version L Auto 4", "RX450h Version L"),    # lone trailing L
    ], strip_tail)
    check("a blank badge field becomes no variant, but a one-letter badge lives",
          strip_tail("Auto") == "" and strip_tail("Auto R") == ""
          and strip_tail("Man") == "" and strip_tail("F Auto R") == "F",
          "'Auto R' is an empty badge field and stores nothing; 'F Auto R' is "
          "a Lexus IS F and keeps its F - the first token is the only thing "
          "that separates them")

    print("\n== the badge line is found, and nothing else is mistaken for it ==")
    accepted = ["LS460 Version U I Package Auto R", "XR6 Turbo FG X Auto",
                "Wildtrak", "Sports Luxury", "420i M Sport Auto R"]
    rejected = ["$74,990", "57,852 km", "Sedan", "Automatic",
                "6cyl 3.5L T Petrol", "Dealer used • VIC", "NSW",
                "Sydney, NSW", "Perth, Western Australia",
                "Drive away $71,918 Excl. Est. Govt. Charges",
                "Excl. Govt. Charges", "View details", "Contact dealer",
                "2007 Lexus LS", ""]
    table("a real badge line is accepted",
          [(s, True) for s in accepted], is_badge_line)
    table("every other line a carsales card emits is refused",
          [(s, False) for s in rejected], is_badge_line)

    print("\n== make and model ==")
    check("a two-word make is taken before any model",
          split_make_model("Land Rover Discovery Sport".split())
          == ("Land Rover", ["Discovery", "Sport"]),
          "'Land Rover Discovery Sport' -> make 'Land Rover', not make 'Land'")
    table("multi-word models are kept whole when the badge shares the line",
          [(("4 Series 420i M Sport").split(), ("4 Series", ["420i", "M", "Sport"])),
           (("Grand Cherokee Trackhawk").split(), ("Grand Cherokee", ["Trackhawk"])),
           (("Land Cruiser Prado GXL").split(), ("Land Cruiser Prado", ["GXL"])),
           (("Model 3 Long Range").split(), ("Model 3", ["Long", "Range"])),
           (("Santa Fe Elite").split(), ("Santa Fe", ["Elite"])),
           (("Falcon XR6 Turbo FG X").split(), ("Falcon", ["XR6", "Turbo", "FG", "X"]))],
          take_model)

    print("\n== the Lexus page from the screenshot ==")
    # The case that prompted all of this: one results page, one model, eight
    # badges. Before the badge line was read, every one of these parsed as
    # "Lexus LS" with no variant and they all landed in one pool.
    lexus = "\n".join([
        "2018 Lexus LS", "LS500 F Sport Auto R", "$74,990", "57,852 km",
        "Dealer used • VIC",
        "2007 Lexus LS", "LS460 Version U I Package Auto R", "$18,900",
        "89,000 km", "Dealer used • NSW",
        "2021 Lexus LS", "LS500h Sports Luxury Auto", "$99,000", "40,000 km",
        "Dealer used • QLD",
        "2013 Lexus LS", "LS460 Sports Luxury Auto R", "$35,000", "120,000 km",
        "Private seller • VIC",
    ])
    got = [make_record(r, source="carsales") for r in pastetext.parse(lexus)]
    variants = [r.variant for r in got]
    check("one page of one model separates into its badges",
          variants == ["LS500 F Sport", "LS460 Version U I Package",
                       "LS500h Sports Luxury", "LS460 Sports Luxury"],
          f"4 cars -> {len(set(variants))} distinct trims: "
          + ", ".join(repr(v) for v in variants))
    check("...and the model stays 'LS' rather than absorbing the badge",
          all(r.make == "Lexus" and r.model == "LS" for r in got),
          "every row is Lexus / LS, so the picker shows one model with four trims")
    check("...with price, odometer, state and seller still bound correctly",
          [(int(r.price_aud), int(r.odometer_km), r.state, r.seller_type)
           for r in got] ==
          [(74990, 57852, "VIC", "dealer"), (18900, 89000, "NSW", "dealer"),
           (99000, 40000, "QLD", "dealer"), (35000, 120000, "VIC", "private")],
          "reading the badge line did not shift any block boundary")

    print("\n== many makes in ONE paste ==")
    mixed = "\n".join([
        "2016 Ford Falcon XR6 Turbo FG X Auto", "$32,000", "120,000 km",
        "Private seller • NSW",
        "2017 BMW 4 Series", "420i M Sport Auto R", "$38,000", "60,000 km",
        "Dealer used • QLD",
        "2019 Toyota HiLux", "SR5 Auto 4x4", "$45,000", "80,000 km",
        "Dealer used • WA",
        "2020 Volkswagen Golf", "R Auto 4", "$52,000", "30,000 km",
        "Dealer used • VIC",
        "2021 Yamaha YZ250F", "$9,500", "Private seller • VIC",
    ])
    rows = [make_record(r, source="carsales") for r in pastetext.parse(mixed)]
    check("five makes in one upload separate cleanly",
          [(r.make, r.model, r.variant) for r in rows] ==
          [("Ford", "Falcon", "XR6 Turbo FG X"),
           ("BMW", "4 Series", "420i M Sport"),
           ("Toyota", "Hilux", "SR5"),
           ("Volkswagen", "Golf", "R"),
           ("Yamaha", "YZ250F", None)],
          "; ".join(f"{r.make} {r.model} {r.variant!r}" for r in rows))
    check("...and the bike is recognised as a bike, not a car",
          [r.vehicle_type for r in rows] == ["car", "car", "car", "car", "bike"],
          "vehicle_type follows the make, so bikes never pool with cars")
    check("...and every one of them gets its own identity",
          len({r.dedupe_key() for r in rows}) == len(rows),
          f"{len(rows)} rows -> {len({r.dedupe_key() for r in rows})} distinct keys")

    print("\n== the two capture paths agree ==")
    # A paste and a saved page of the SAME car must produce the same variant.
    # When they did not, one BMW trim occupied three pools in the real data.
    pairs = [
        ("2015 Ford Falcon G6E Turbo FG X Auto", "Ford", "Falcon"),
        ("2017 BMW 4 Series 420i M Sport Auto R", "BMW", "4 Series"),
        ("2018 Lexus LS LS500 F Sport Auto R", "Lexus", "LS"),
    ]
    bad = []
    for title, make, model in pairs:
        from_html = htmlpage._variant_from_name(title, make, model)
        paste_rows = pastetext.parse(f"{title}\n$30,000\n50,000 km")
        from_paste = paste_rows[0]["variant"] if paste_rows else None
        if from_html != from_paste:
            bad.append((title, from_html, from_paste))
    check("a saved page and a paste of one car give one variant",
          not bad,
          "3/3 agree" if not bad else
          "; ".join(f"{t!r}: html {h!r} vs paste {p!r}" for t, h, p in bad))

    print("\n== nothing regressed on the real captures ==")
    from pathlib import Path
    raw_dir = Path(__file__).resolve().parent.parent / "data" / "raw"
    if not raw_dir.exists():
        check("real saved pages still parse", True, "skipped - no data/raw here")
    else:
        # Length is NOT the test. 'Boss 335 GT RSPEC FG MK II' is a real FPV
        # trim and six words long; 'FG X XR6 Turbo Sedan 4dr Spts Auto 6sp
        # 4.0T' is a spec dump. The difference is the CONTENT - a spec dump
        # carries a body style, a door count, a gear count or a displacement.
        import re as _re
        dump = _re.compile(
            r"\b\d+\s?dr\b|\b\d{1,2}sp\b|\b\d\.\d\s?[TL]?\b"
            r"|\b(sedan|hatch|wagon|coupe|ute|suv)\b.*\b(auto|manual|spts)\b",
            _re.I)
        bad, longest, total = [], "", 0
        for f in sorted(raw_dir.glob("*.htm")):
            for r in htmlpage.parse(f)[0]:
                total += 1
                v = str(r.get("variant") or "")
                if v and dump.search(v):
                    bad.append(v)
                if len(v.split()) > len(longest.split()):
                    longest = v
        check("no saved page yields a spec dump as a trim name",
              not bad,
              f"{total} listing(s) across every saved page; no trim carries a "
              f"door count, gear count, displacement or body style. Longest "
              f"real trim: {longest!r}"
              if not bad else f"{bad[:2]}")

    print(f"\n== {_passed}/{_passed + _failed} checks passed ==")
    return 1 if _failed else 0


def main() -> int:
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
