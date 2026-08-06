"""Parse a saved carsales/bikesales page for ONE vehicle (a "details" page).

    Ctrl+S on the ad itself, not the search results.

A search-results page states very little per listing: the JSON-LD carries
bodyType, brand, image, mileageFromOdometer, model, name, offers, url and
vehicleEngine, and state/seller only come from tracking clusters. A details
page carries a single analytics `metaData` object that states everything at
once, for that one car:

    "make":"Ford","model":"Falcon","badge":"XR6 Turbo","state":"NSW",
    "networkId":"SSE-AD-20672993","colour":"Black","sellertype":"Private",
    "year":"2015","odometermin":"161843","price":"28000","bodystyle":"Sedan"

That is a different shape from `htmlpage._tracking_supplement`'s
`"tracking/item/networkId"` clusters, which is why this is its own parser
rather than a flag on that one.

**Colour lives only here.** It is not on a search page at all, so it cannot
be back-filled for the vehicles already stored - see `identify_payload` in
explore.py, which shows it for the uploaded car and does not pretend the
database knows it. It is also NOT read from the analytics object: see
`_colours` for what that field turned out to contain.

Read-only: this module parses text and returns a dict. It never writes.
"""
from __future__ import annotations

import json
import re

#: the analytics object a details page emits; several copies appear per page
#: and they agree, so the first complete one wins
_META = re.compile(r'"metaData"\s*:\s*\{(.{0,1800}?)\}', re.S)
_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S)

#: fields we lift out of that object, mapped to Record's names.
#: `colour` is deliberately ABSENT - see _colours() below.
_FIELDS = {
    "networkId": "listing_id", "make": "make", "model": "model",
    "badge": "badge", "state": "state",
    "sellertype": "seller_type", "year": "year",
    "odometermin": "odometer", "price": "price", "bodystyle": "body_type",
    "fueltype": "fuel_type",
}

#: page-level tracking, a different blob from the click-tracking metaData:
#: {"type":"KeyValueMetaData","key":"tracking/page/details/colour","value":"kinetic"}
_TRACK_COLOUR = re.compile(
    r'"tracking/page/details/colour"\s*,\s*"value"\s*:\s*"([^"]{1,40})"')
_VALUE = re.compile(r'"value"\s*:\s*"([^"]{1,40})"')


def _labelled(html: str, label: str) -> str | None:
    """The value shown beside `label` in the ad's own specifications table.

    A saved page contains that table twice - once as rendered DOM and once
    as the widget JSON it was rendered from - and either will do:

        >Exterior colour</span></div><div ...><span ...>Kinetic</span>
        "value":"Exterior colour", ... "value":"Kinetic"

    Both searches are length-bounded so a row with a missing value cannot
    run on and return the NEXT row's text as this row's answer.
    """
    m = re.search(r'>' + re.escape(label) + r'</span></div>'
                  r'.{0,200}?<span[^>]*>([^<]{1,40})</span>',
                  html, re.I | re.S)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r'"value"\s*:\s*"' + re.escape(label) + r'"', html, re.I)
    if m:
        nxt = _VALUE.search(html, m.end())
        if nxt and nxt.start() - m.end() < 400 and nxt.group(1).strip():
            return nxt.group(1).strip()
    return None


def _nice(v: str | None) -> str | None:
    """'kinetic' -> 'Kinetic'. The page states the same colour in three
    casings depending on which blob it came from; only obviously-uniform
    casing is touched, so 'Lightning Blue' is left alone."""
    if not v:
        return None
    v = v.strip()
    return v.title() if (v.islower() or v.isupper()) else v


def _colours(html: str) -> tuple[str | None, str | None]:
    """(exterior, interior) as the ad itself displays them.

    Read from the labelled spec rows, NOT from the analytics object, which
    is the obvious-looking source and is wrong. On the real page checked on
    03/08/2026 (2015 Ford Falcon XR6 Turbo FG X, SSE-AD-20672993) the ad
    displays:

        Exterior colour   Kinetic
        Interior colour   Black

    and the two analytics blobs disagree with each other:

        enquiry-form metaData        "colour":"Black"    <- the INTERIOR
        tracking/page/details/colour "kinetic"           <- the exterior

    The first version of this parser took metaData.colour and so reported a
    Kinetic Blue car as black. The spec row is what a human reading the ad
    would see, is unambiguously labelled, and is therefore what is used;
    page-level tracking is the fallback because it agreed with it. The
    metaData colour is not consulted at all - it is better to report no
    colour than to report the seat trim as the paint.
    """
    ext = _labelled(html, "Exterior colour")
    if not ext:
        m = _TRACK_COLOUR.search(html)
        if m:
            ext = m.group(1)
    return _nice(ext), _nice(_labelled(html, "Interior colour"))


def _kv(blob: str) -> dict:
    out = {}
    for k in _FIELDS:
        m = re.search(r'"' + k + r'"\s*:\s*"([^"]*)"', blob)
        if m and m.group(1).strip():
            out[k] = m.group(1).strip()
    return out


def is_detail_page(html: str) -> bool:
    """True when the page describes ONE vehicle rather than a search result.

    A details page names exactly one ad id in its analytics; a results page
    repeats a different one per tile, under the `tracking/item/networkId`
    key that `htmlpage` reads instead.

    Two things this has to get right, both learned from the real file:

    * the id FORMAT is not constrained - what makes this a details page is
      that it describes one vehicle, not that the id looks like `SSE-AD-…`;
    * ids are compared CASE-FOLDED. A real carsales details page emits the
      same ad as `SSE-AD-20672993` fifteen times and `sse-ad-20672993` ten
      times; counting them case-sensitively says "two vehicles" and rejects
      the page. A search page carries 5-10 genuinely different ids, so
      folding case cannot make one look like a details page."""
    ids = {i.casefold() for i in
           re.findall(r'"networkId"\s*:\s*"([^"]+)"', html)}
    return len(ids) == 1


def parse_html(html: str) -> dict | None:
    """Everything the page states about its one vehicle, or None.

    Returns Record-shaped keys plus `colour` and `interior_colour`, which
    Record has no fields for and which the caller must therefore treat as
    display-only."""
    best: dict = {}
    for m in _META.finditer(html):
        got = _kv(m.group(1))
        if len(got) > len(best):
            best = got
        if len(best) >= len(_FIELDS) - 2:
            break
    if not best:
        # some layouts put the same names outside a metaData wrapper
        best = _kv(html)
    if not best.get("networkId"):
        return None

    raw: dict = {}
    for src, dst in _FIELDS.items():
        if src in best:
            raw[dst] = best[src]

    # JSON-LD gives the full model name, which is where the trim/series text
    # lives ("2015 Ford Falcon XR6 Turbo FG X Auto" -> "XR6 Turbo FG X")
    name = config = None
    for blob in _LD.findall(html):
        try:
            d = json.loads(blob)
        except (ValueError, TypeError):
            continue
        for node in (d if isinstance(d, list) else [d]):
            if isinstance(node, dict):
                name = name or node.get("name")
                config = config or node.get("vehicleConfiguration")
    if name:
        raw["raw_title"] = name
        from .htmlpage import _variant_from_name
        variant = _variant_from_name(name, raw.get("make"), raw.get("model"))
        if variant:
            raw["variant"] = variant
    if not raw.get("variant"):
        # fall back to badge + series words from the configuration string
        badge = raw.get("badge")
        if badge and config:
            series = re.match(r"([A-Z0-9]{2,3}(?: [A-Z0-9]{1,3})?)\s", config)
            raw["variant"] = (f"{badge} {series.group(1)}".strip()
                              if series else badge)
        elif badge:
            raw["variant"] = badge
    raw.pop("badge", None)

    exterior, interior = _colours(html)
    if exterior:
        raw["colour"] = exterior
    if interior:
        raw["interior_colour"] = interior

    m = re.search(r'"@id"\s*:\s*"([^"]+/details/[^"]+)"', html)
    if m:
        raw["url"] = m.group(1)
    return raw


def parse(path) -> dict | None:
    from pathlib import Path
    return parse_html(Path(path).read_text(encoding="utf-8", errors="ignore"))
