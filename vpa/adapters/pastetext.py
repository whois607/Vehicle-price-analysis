"""Pasted/saved raw listing text -> raw field dicts.

Works on results-page dumps and single-listing pastes. The anchor is a
*title line*: a line starting with a plausible year followed by words
("2014 Ford Falcon XR6 Turbo"). Everything until the next title line is that
listing's block; price, odometer, state, seller type are mined from the block.

Two tile layouts are auto-detected:
  title-led   title first, price below (carsales-style, hand-written notes)
  price-led   price line ABOVE the title (Facebook Marketplace tiles:
              "AU$55,000" / "2010 Ford Falcon" / "Sydney, NSW"). When most
              titles have a lone price line directly above, each block is
              extended one line up so the price binds to the RIGHT vehicle.
              Marketplace strikethrough deals ("AU$18,000AU$22,500") take
              the first (current) price; tiles without a year-led title
              (wheels, parts) are skipped.

A carsales card also puts the TRIM on its own line under the title:

    2007 Lexus LS                       <- title line
    LS460 Version U I Package Auto R    <- badge line

Both are read (see `titles.is_badge_line`). Without the badge line every
Lexus LS on the page — LS460, LS500, LS500h, F Sport, Sports Luxury, three
Version packages — parsed as one undifferentiated "Lexus LS", which is the
single largest way this tool could silently pool cars that are not
comparable. The badge line also makes the MODEL exact: whatever remains of
the title after the make is the model, however many words, so "4 Series"
and "Grand Cherokee" need no guessing.
"""
from __future__ import annotations

import re

from .titles import (TWO_WORD_MAKES as _TWO_WORD_MAKES, is_badge_line,
                     split_make_model, strip_tail, take_model)

_TITLE = re.compile(r"^\s*(19[5-9]\d|20[0-3]\d)\s+([A-Za-z][\w&.-]*(?:\s+\S+)*)\s*$")
_PRICE = re.compile(r"\$[ ]*\d{1,3}(?:[ ,]?\d{3})*(?:\.\d+)?|\b\d{4,7}[ ]*(?:AUD|dollars)\b", re.I)
# a line that is JUST a price (optionally 'AU$', optionally followed by a
# struck-through previous price) - marketplace tiles put this above the title
_PRICE_LINE = re.compile(
    r"^\s*(?:AU\s?)?\$\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?"
    r"(?:\s*(?:AU\s?)?\$\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)?\s*$", re.I)
_ODO = re.compile(r"([\d,\.]+\s*k?)\s*(?:km\b|kms\b|kilometer|kilometre)", re.I)
_ODO_LABEL = re.compile(r"odometer[:\s]+([\d,\.]+\s*k?)", re.I)
_SELLER = re.compile(r"\b(dealer(?:ship)?(?:\s+used)?|private(?:\s+seller)?|demo)\b", re.I)
_NEGOTIABLE = re.compile(r"\bnegotiable\b|\bono\b|\bor best offer\b|\bobo\b", re.I)
_DRIVE_AWAY = re.compile(r"drive\s*away", re.I)
_STATE_TOKEN = re.compile(
    r"\b(NSW|VIC|QLD|SA|WA|TAS|NT|ACT|New South Wales|Victoria|Queensland|"
    r"South Australia|Western Australia|Tasmania|Northern Territory)\b", re.I)
# Lines that look like prices/UI junk shouldn't be mistaken for titles.
_JUNK_TITLE = re.compile(r"km|kms|\$|sponsored|^\d+\s*$", re.I)


def split_title(year: str, rest: str,
                badge: str | None = None) -> tuple[str, str, str | None]:
    """'Ford Falcon XR6 Turbo' -> (make, model, variant).

    With a `badge` from the line below the title, the split is EXACT: the
    make comes off the front and everything left on the title line is the
    model, so "BMW 4 Series" gives model "4 Series" rather than model "4"
    with "Series" swallowed into the variant. Without one, where the model
    ends has to be guessed — see `titles.take_model`.

    Either way the trailing gearbox/drive descriptor is removed, so a paste
    and a saved page produce the same variant for the same car.
    """
    words = rest.split()
    if not words:
        return "", "", None
    make, tail = split_make_model(words)
    if badge:
        model = " ".join(tail)
        variant = strip_tail(badge.strip())
    else:
        model, badge_words = take_model(tail)
        variant = strip_tail(" ".join(badge_words)) if badge_words else None
    return make, model, (variant or None)


def parse(text: str) -> list[dict]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    # indices of title lines
    starts: list[int] = []
    for i, ln in enumerate(lines):
        m = _TITLE.match(ln)
        if m and not _JUNK_TITLE.search(m.group(2)):
            rest = m.group(2)
            if len(rest.split()) >= 2:      # need at least make + model
                starts.append(i)
    # layout detection: when most titles have a lone price line directly
    # above them, this is a price-led dump (Facebook Marketplace) - extend
    # each block one line up so the price binds to its own vehicle, not the
    # previous block's tail
    def price_above(i: int) -> bool:
        return i > 0 and bool(_PRICE_LINE.match(lines[i - 1]))

    price_led = starts and sum(price_above(i) for i in starts) * 2 >= len(starts)
    begins = [i - 1 if price_led and price_above(i) else i for i in starts]

    out: list[dict] = []
    for n, i in enumerate(starts):
        j = begins[n + 1] if n + 1 < len(starts) else len(lines)
        title = lines[i].strip()
        block = "\n".join(lines[begins[n]:j])
        m = _TITLE.match(title)
        year, rest = m.group(1), m.group(2)
        # The trim line sits directly under the title on a carsales card. It
        # is only looked for in the title-led layout: a Marketplace tile puts
        # a LOCATION there ("Sydney, NSW"), and reading that as a trim would
        # invent one. `is_badge_line` refuses anything not clearly a badge,
        # so the worst case is the blank variant this produced before.
        badge = None
        if not price_led and i + 1 < len(lines) and i + 1 < j:
            nxt = lines[i + 1].strip()
            if is_badge_line(nxt):
                badge = nxt
        make, model, variant = split_title(year, rest, badge)
        raw: dict = {"raw_title": title, "year": year, "make": make,
                     "model": model, "variant": variant}
        if (pm := _PRICE.search(block)):
            price_text = pm.group(0)
            if _NEGOTIABLE.search(block):
                price_text += " negotiable"
            if _DRIVE_AWAY.search(block):
                price_text += " drive away"
            raw["price"] = price_text
        elif _NEGOTIABLE.search(block):
            raw["price"] = "negotiable"
        om = _ODO_LABEL.search(block) or _ODO.search(block)
        if om:
            raw["odometer"] = om.group(1).strip() + " km"
        if (sm := _STATE_TOKEN.search(block)):
            raw["state"] = sm.group(1)
        if (sel := _SELLER.search(block)):
            raw["seller_type"] = "private" if sel.group(1).casefold().startswith("private") else "dealer"
        out.append(raw)
    return out
