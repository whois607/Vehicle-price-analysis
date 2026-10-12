"""Splitting one listing's title text into make, model and variant.

ONE module, used by BOTH capture paths — `pastetext` (copy-paste) and
`htmlpage` (saved page). Before this existed they disagreed, and the same
physical trim was stored under different names depending on how it was
captured. Measured in the real database on 04/08/2026:

    19 rows  BMW 4 Series  '420i M Sport'
     6 rows  BMW 4 Series  '420i M Sport Auto R'
     3 rows  BMW 4 Series  '420i M Sport F32'

One trim, three names, three separate pools in every chart. A shared splitter
is the only way a paste and a saved page can be guaranteed to agree, for the
same reason `analyze` is shared between the app, the CLI and the workbook.

What a typical results card looks like:

    line 1 (title)  "2007 Lexus LS"                       {year} {make} {model}
    line 2 (badge)  "LS460 Version U I Package Auto R"     {badge} {series} {transmission} {drive}

**When the badge is on its own line the model needs no guessing at all**: the
whole of line 1 after the make IS the model, however many words that is —
"4 Series", "Grand Cherokee", "Land Cruiser Prado". That exactness is why
`split_title` prefers a badge line over parsing one long title.

The hard part is the tail. "Auto R" is transmission + drive and must go, or
the same car reads as a different trim depending on the day. But a trailing
single letter is often the badge itself — Golf **R**, Civic Type **R**, Model
**S**, A45 **S**, i30 **N**. Stripping trailing letters blindly destroys those.
The rule that separates them: **a drive code is only a drive code when a
transmission word sits immediately before it.** "Auto R" strips; "Golf R"
does not, because "Golf" is not a transmission.

Everything here is pure text. Nothing reads a database or a network.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- tail tokens
#: Words that name a gearbox. Only these AUTHORISE a tail to be removed.
#:
#: Deliberately absent, and each absence is load-bearing:
#:   "Sport"   badge word — "420i M Sport", "GT Sport", "Maxx Sport".
#:   "Sports"  badge word — Lexus "IS200 Sports", "LS500h Sports Luxury".
#:             listings abbreviate the gearbox to "Spts Auto", never
#:             "Sports Auto", so only the abbreviation is a descriptor.
#:   "S tronic" (Audi) — would need "S" to be strippable, and "S" is a badge
#:             in its own right ("A45 S", "C63 S", "Cooper S"). Left alone
#:             on purpose; see the unhandled list in NOTES.
_TRANSMISSION = {
    "auto", "automatic", "man", "manual", "cvt", "dct", "d-ct", "dsg", "smg",
    "tiptronic", "steptronic", "multitronic", "powershift", "seq", "sequential",
}

#: Descriptor words that do NOT authorise a strip on their own but may be
#: carried out with one: "Spts Auto Dual Clutch 7sp" is all gearbox.
_TAIL_EXTRA = {"spts", "sprts", "dual", "clutch"}

#: Drive codes. quattro, xDrive, 4MATIC and AllGrip are NOT here — listings
#: prints those as part of the badge, not as a trailing drive code.
_DRIVE = {"r", "f", "4", "2", "awd", "4wd", "fwd", "rwd", "2wd", "4x4", "4x2"}

#: "6sp", "8sp", "sp" (from "8 Sp"), and the bare count in front of a split
#: "8 Sp". A bare number can only ever leave with a real gearbox word, so
#: "Colorado 7" and "Tank 300" are never touched.
_SPEED = re.compile(r"^(\d{1,2}sp|sp|\d{1,2})$", re.I)

#: Model-year codes: "MY19", "MY22". Redundant with the year column and they
#: split one trim into one pool per model year, so they come off the end.
_MY_CODE = re.compile(r"^my\d{2}$", re.I)

#: Body styles a listing appends AFTER the gearbox on ute and van listings:
#: "XR6 Turbo FG X Manual Super Cab". They are redundant with the body_type
#: column and, being on the far side of the gearbox word, they block it —
#: which is how one real Falcon ute trim reached the database under both
#: "... Auto Super Cab" and "... Manual Super Cab". Longest phrase first.
_BODY_TAIL = ("cab chassis", "super cab", "dual cab", "single cab",
              "crew cab", "extra cab", "king cab", "space cab", "double cab",
              "cab", "wagon", "sedan", "hatch", "hatchback", "coupe",
              "convertible", "cabriolet", "van", "ute", "utility", "liftback",
              "fastback", "roadster", "targa", "softtop", "hardtop")


def _is_tail_token(tok: str) -> bool:
    t = tok.casefold().strip(",")
    return (t in _TRANSMISSION or t in _DRIVE or t in _TAIL_EXTRA
            or bool(_SPEED.match(t)))


def strip_tail(text: str) -> str:
    """Remove a trailing transmission/drive descriptor from a badge string.

        'LS460 Version U I Package Auto R'   -> 'LS460 Version U I Package'
        'XR6 Turbo FG X Auto'                -> 'XR6 Turbo FG X'
        'Trackhawk WK MY19 Spts Auto 8sp 4x4' -> 'Trackhawk WK MY19'
        'GS350 Sports Luxury 8 Sp Automatic R' -> 'GS350 Sports Luxury'
        'Golf R'                             -> 'Golf R'        (untouched)
        'Civic Type R'                        -> 'Civic Type R'  (untouched)
        'A45 S Auto 4'                        -> 'A45 S'
        'Auto'                                -> ''             (no variant)

    The rule in one sentence: **take the longest run of descriptor words at
    the end, and remove it only if a real gearbox word is somewhere inside
    that run.** That single condition is what separates "Auto R" from
    "Golf R" — both end in a lone letter, but only one has a gearbox in
    front of it. Chained tails ("Spts Auto 8sp 4x4") come off in one piece
    for the same reason, which a strip-one-token-at-a-time loop could not do
    once a speed count sat between the gearbox and the drive code.

    A badge that is ENTIRELY descriptor ("Auto", "Auto R") returns empty, so
    the caller stores no variant. A base-model card with a blank badge field
    is a car with no trim, not a car whose trim is "Auto"; recording the
    latter invents a trim that nothing else will ever match.

    Series and chassis codes are never removed. 'FG X', 'FG MkII', 'F32',
    'PX MkIII', 'GUN126R' and 'W205' are real generational differences — the
    user's whole Falcon dataset is separated by exactly such codes — so this
    treats them as part of the trim.
    """
    tokens = text.split()
    if not tokens:
        return text
    # A model-year code can sit on either side of the gearbox run — listings
    # writes both "Wildtrak PX MkIII Auto 4x4 MY22" and "... FY MY21 Auto 4x4"
    # — so the two removals alternate until neither applies. Stripping it in
    # only one position is what left the same Ranger under two trim names.
    changed = True
    while changed:
        changed = False
        while len(tokens) > 1 and _MY_CODE.match(tokens[-1]):
            tokens, changed = tokens[:-1], True
        for phrase in _BODY_TAIL:
            n = len(phrase.split())
            if (len(tokens) > n
                    and " ".join(tokens[-n:]).casefold() == phrase):
                tokens, changed = tokens[:-n], True
                break
        # Widest run of descriptor words at the end...
        start = len(tokens)
        while start > 0 and _is_tail_token(tokens[start - 1]):
            start -= 1
        # ...but the tail does not begin where that run begins. A drive code
        # to the LEFT of the gearbox word is a badge, not a tail: 'Golf R
        # Auto 4' and 'Auto R' both end in a descriptor run, and cutting at
        # the run's start turns the first into plain 'Golf'. The tail starts
        # at the first GEARBOX-OR-SPEED word inside the run — speed included
        # so 'GS350 Sports Luxury 8 Sp Automatic R' loses '8 Sp' too — then
        # extends left over the qualifiers that only ever precede one
        # ('Spts Auto', 'Dual Clutch').
        cut = None
        for i in range(start, len(tokens)):
            t = tokens[i].casefold().strip(",")
            if t in _TRANSMISSION or _SPEED.match(t):
                cut = i
                break
        if cut is not None:
            while cut > start and tokens[cut - 1].casefold() in _TAIL_EXTRA:
                cut -= 1
            if any(t.casefold().strip(",") in _TRANSMISSION
                   for t in tokens[cut:]):
                tokens, changed = tokens[:cut], True
        # A LEADING gearbox word means the badge field was empty and the site
        # started the tail immediately: "Auto quattro MY23" on an Audi RS3.
        # No Australian badge begins with a gearbox word, so this is safe, and
        # without it those cars keep "Auto" welded to the front of their trim.
        while len(tokens) > 1 and tokens[0].casefold() in _TRANSMISSION:
            tokens, changed = tokens[1:], True
    return " ".join(tokens)


# ------------------------------------------------------------ make and model
#: Makes written as two words. Checked before any one-word split.
TWO_WORD_MAKES = {"royal enfield", "alfa romeo", "land rover", "harley davidson",
                  "harley-davidson", "aston martin", "great wall", "cf moto",
                  "mercedes benz", "mercedes-benz", "rolls royce"}

#: Models that are genuinely two or three words, where the first word alone is
#: not a model. Only needed when the badge shares the title line, because a
#: badge on its OWN line makes the whole title tail the model with no guessing.
#: Kept short and specific on purpose: a wrong entry here invents a model.
_MULTI_WORD_MODELS = {
    "land cruiser", "land cruiser prado", "landcruiser prado", "grand cherokee",
    "grand vitara", "grand carnival", "range rover", "range rover sport",
    "range rover evoque", "range rover velar", "santa fe", "grand touring",
    "street glide", "road king", "super duke", "speed triple", "monkey bike",
}
#: "4 Series", "3 Series" — a bare number followed by a joining word.
_MODEL_JOINER = {"series", "class"}
#: "Model 3", "Model S" — a head word that always takes the next token.
_MODEL_HEAD = {"model"}


def split_make_model(tail: list[str]) -> tuple[str, list[str]]:
    """Given the title words after the year, return (make, remaining words).

    Two-word makes are matched first — "Land Rover Discovery" must not become
    make "Land", model "Rover".
    """
    low2 = " ".join(tail[:2]).casefold()
    if low2 in TWO_WORD_MAKES and len(tail) >= 3:
        return " ".join(tail[:2]), tail[2:]
    return (tail[0], tail[1:]) if tail else ("", [])


def take_model(words: list[str]) -> tuple[str, list[str]]:
    """Split the words after the make into (model, badge words).

    Used ONLY when the badge shares the title line. When the badge has its own
    line the caller passes the whole tail as the model instead, which is exact.
    """
    if not words:
        return "", []
    low = [w.casefold() for w in words]
    for n in (3, 2):                       # longest multi-word model first
        if len(words) >= n and " ".join(low[:n]) in _MULTI_WORD_MODELS:
            return " ".join(words[:n]), words[n:]
    if len(words) >= 2 and low[1] in _MODEL_JOINER and re.fullmatch(r"\w{1,2}", words[0]):
        return f"{words[0]} {words[1]}", words[2:]      # "4 Series"
    if len(words) >= 2 and low[0] in _MODEL_HEAD:
        return f"{words[0]} {words[1]}", words[2:]      # "Model 3"
    return words[0], words[1:]


# ------------------------------------------------------------- the badge line
#: Body styles, fuel/engine words and UI furniture that a results card puts
#: on its own line. None of these is ever a badge.
_NOT_BADGE_WORDS = {
    "sedan", "hatch", "hatchback", "suv", "ute", "wagon", "coupe", "convertible",
    "cabriolet", "van", "people mover", "bus", "truck", "automatic", "manual",
    "petrol", "diesel", "hybrid", "electric", "lpg",
}
#: Whole lines a results page renders as buttons, labels or disclaimers.
_UI_LINE = re.compile(
    r"^\s*(view details|contact (dealer|seller)|enquire|save|share|compare|"
    r"excl\.?\s*(est\.?\s*)?govt\.?\s*charges|drive away|dealer (used|new|demo)|"
    r"private seller|id verified|pay through \w+|sponsored|featured|"
    r"finance|instant offer|great price|fair price|new arrival)\b", re.I)
_HAS_PRICE = re.compile(r"\$|\bAU\$")
_HAS_ODO = re.compile(r"\d[\d,\. ]*\s*k?m?s?\s*(km|kms)\b", re.I)
_ENGINE = re.compile(r"\d+\s*cyl|\b\d\.\d\s*L\b|\bkw\b|\bnm\b", re.I)
#: A bare state, or a tile's location line "Sydney, NSW". Matched as a
#: WHOLE line rather than by looking for a state anywhere, so a badge is never
#: refused for containing two letters that happen to spell a state.
_STATE_ONLY = re.compile(
    r"^\s*(.+,\s*)?(NSW|VIC|QLD|SA|WA|TAS|NT|ACT|New South Wales|Victoria|"
    r"Queensland|South Australia|Western Australia|Tasmania|"
    r"Northern Territory|Australian Capital Territory)\s*$", re.I)
_YEAR_LED = re.compile(r"^\s*(19[5-9]\d|20[0-3]\d)\b")

#: A badge longer than this is not a badge — it is a description that happened
#: to land under a title.
MAX_BADGE_WORDS = 8


def is_badge_line(line: str) -> bool:
    """Is this the trim line that sits directly under a listing's title?

    Written as a list of REJECTIONS, deliberately. A wrong badge becomes a
    fabricated trim that quietly splits a pool; a missed badge only leaves
    the variant blank, which is what the tool did before this existed. So
    anything not clearly a badge is refused.
    """
    s = (line or "").strip().strip("·—-•|")
    if not s:
        return False
    words = s.split()
    if not (1 <= len(words) <= MAX_BADGE_WORDS):
        return False
    if _YEAR_LED.match(s):          # the next listing's title
        return False
    if _HAS_PRICE.search(s) or _HAS_ODO.search(s) or _ENGINE.search(s):
        return False
    if _UI_LINE.match(s) or _STATE_ONLY.match(s):
        return False
    if "•" in s or "·" in s:        # "Dealer used • VIC"
        return False
    if s.casefold() in _NOT_BADGE_WORDS:
        return False
    if not any(c.isalnum() for c in s):
        return False
    # a badge always carries a capital or a digit somewhere: XR6, M Sport,
    # Wildtrak, 420i. All-lowercase prose is not a badge.
    return any(c.isupper() or c.isdigit() for c in s)
