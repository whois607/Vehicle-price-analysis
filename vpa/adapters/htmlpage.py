"""Saved HTML pages (Ctrl+S from your browser) -> raw field dicts.

Two passes, most-structured first:
  1. JSON-LD (<script type="application/ld+json">): Carsales/Bikesales embed
     schema.org Vehicle/Car/Motorcycle/Product objects with clean fields.
  2. Fallback: strip tags to visible text (stdlib parser, scripts/styles
     dropped, block elements become line breaks) and reuse the pasted-text
     parser. This is what usually fires for Facebook Marketplace saves.

No network access — this only ever reads files you saved yourself.
"""
from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from . import pastetext
from .titles import strip_tail

_LDJSON = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S)

_VEHICLE_TYPES = {"car", "vehicle", "motorcycle", "motorizedvehicle", "product"}

_SITE_HINTS = [("carsales.com", "carsales"), ("bikesales.com", "bikesales"),
               ("facebook.com", "facebook"), ("gumtree.com", "gumtree")]

_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5",
               "section", "article", "header", "footer", "td", "span"}


class _TextExtractor(HTMLParser):
    """Visible text with line structure preserved enough for pastetext."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _walk_ld(node, found: list[dict]):
    """Collect schema.org vehicle-ish objects from arbitrarily nested JSON-LD."""
    if isinstance(node, list):
        for item in node:
            _walk_ld(item, found)
    elif isinstance(node, dict):
        t = node.get("@type", "")
        types = {str(x).casefold() for x in (t if isinstance(t, list) else [t])}
        if types & _VEHICLE_TYPES:
            found.append(node)
        for key in ("@graph", "itemListElement", "item", "mainEntity", "offers"):
            if key in node and key != "offers":
                _walk_ld(node[key], found)
            elif key == "offers" and isinstance(node.get(key), dict):
                _walk_ld(node[key].get("itemOffered"), found)


def _num(v):
    if isinstance(v, dict):
        v = v.get("value")
    return v


_AD_ID = re.compile(r"\b([A-Z]{2,4}-AD-\d+)\b")


def _variant_from_name(name: str, brand: str | None, model: str | None) -> str | None:
    """'2016 Ford Falcon G6E Turbo FG X Auto' minus year/brand/model and the
    trailing gearbox/drive descriptor -> 'G6E Turbo FG X'.

    The tail is removed by `titles.strip_tail`, the SAME function the paste
    path uses, so one car captured both ways cannot end up under two trim
    names. Before they were shared, this path stripped 'Auto' while the paste
    path stripped nothing, and neither stripped 'Auto R'."""
    tokens = name.split()
    if tokens and re.fullmatch(r"(19|20)\d\d", tokens[0]):
        tokens = tokens[1:]
    for part in (brand, model):
        if not part:
            continue
        words = str(part).split()
        if [t.casefold() for t in tokens[:len(words)]] == [w.casefold() for w in words]:
            tokens = tokens[len(words):]
    return strip_tail(" ".join(tokens)) or None


# Carsales result pages embed per-listing analytics attributes keyed by the
# same ad id that appears in each listing's URL — the only place the page
# states seller type and state per listing in machine-readable form.
_TRACK_ID = re.compile(r'"tracking/item/networkId":"([^"]+)"')
_TRACK_STATE = re.compile(r'"tracking/item/state":"([^"]+)"')
_TRACK_ADTYPE = re.compile(r'"tracking/item/adtype":"([^"]+)"')


def _tracking_supplement(html: str) -> dict[str, dict]:
    """ad id -> {'state', 'seller_type'} merged across the page's repeated
    tracking clusters (some clusters carry only part of the fields)."""
    out: dict[str, dict] = {}
    matches = list(_TRACK_ID.finditer(html))
    for m, nxt in zip(matches, matches[1:] + [None]):
        # cap the window at the NEXT cluster so one listing's fields can
        # never bleed into its neighbour's
        end = min(m.end() + 1200, nxt.start() if nxt else len(html))
        window = html[m.end():end]
        info = out.setdefault(m.group(1), {})
        if "state" not in info and (s := _TRACK_STATE.search(window)):
            info["state"] = s.group(1)
        if "seller_type" not in info and (a := _TRACK_ADTYPE.search(window)):
            low = a.group(1).casefold()
            if "private" in low:
                info["seller_type"] = "private"
            elif "dealer" in low or "demo" in low:
                info["seller_type"] = "dealer"
    return out


def _ld_to_raw(obj: dict) -> dict | None:
    raw: dict = {}
    brand = obj.get("brand") or obj.get("manufacturer")
    if isinstance(brand, dict):
        brand = brand.get("name")
    raw["make"] = brand
    model = obj.get("model") or obj.get("vehicleModel")
    if isinstance(model, dict):
        model = model.get("name")
    raw["model"] = model
    raw["raw_title"] = obj.get("name") or obj.get("description")
    raw["year"] = (obj.get("vehicleModelDate") or obj.get("productionDate")
                   or obj.get("modelDate") or obj.get("releaseDate"))
    # `trim` is a clean badge where a site provides it. `vehicleConfiguration`
    # is NOT: on a carsales details page it is the full spec string
    # "FG X XR6 Turbo Sedan 4dr Spts Auto 6sp 4.0T", which stored as a variant
    # invents a trim no other capture of the same car will ever match. The
    # title gives "XR6 Turbo FG X", which is what every search-page capture
    # already produced, so the title wins and the configuration is a last
    # resort — see _variant_from_name below.
    raw["variant"] = obj.get("trim")
    raw["body_type"] = obj.get("bodyType")
    raw["odometer"] = _num(obj.get("mileageFromOdometer"))
    offers = obj.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    raw["price"] = offers.get("price") or _num(obj.get("price"))
    seller = offers.get("seller") or {}
    if isinstance(seller, dict):
        stype = str(seller.get("@type", "")).casefold()
        if "organization" in stype or "dealer" in stype or "autodealer" in stype:
            raw["seller_type"] = "dealer"
        elif "person" in stype:
            raw["seller_type"] = "private"
    area = offers.get("areaServed") or obj.get("address") or {}
    if isinstance(area, dict):
        raw["state"] = area.get("addressRegion") or area.get("name")
    raw["url"] = obj.get("url") or (offers.get("url") if isinstance(offers, dict) else None)
    # Identity: the site's own ad id wins over everything, wherever it is
    # hiding. A search page puts it in the listing URL; a saved DETAILS page
    # puts a full URL in `@id` and no `sku` at all, so taking `@id` verbatim
    # keyed that car by URL while every search capture of the SAME car was
    # keyed 'SSE-AD-20672993' — one car, two identities, silently. The ad id
    # is extracted from any of the candidates before any of them is used raw.
    candidates = [obj.get("sku"), obj.get("@id"), obj.get("identifier"),
                  raw.get("url")]
    raw["listing_id"] = None
    for cand in candidates:
        if cand and (m := _AD_ID.search(str(cand))):
            raw["listing_id"] = m.group(1)
            break
    if not raw["listing_id"]:
        raw["listing_id"] = next((c for c in candidates[:3] if c), None)
    if not raw.get("variant") and raw.get("raw_title"):
        raw["variant"] = _variant_from_name(str(raw["raw_title"]),
                                            raw.get("make"), raw.get("model"))
    if not raw.get("variant") and obj.get("vehicleConfiguration"):
        # only when the title gave nothing at all, and only if it is short
        # enough to be a badge rather than a spec dump
        cfg = str(obj["vehicleConfiguration"]).strip()
        if 0 < len(cfg.split()) <= 4:
            raw["variant"] = cfg
    raw = {k: v for k, v in raw.items() if v not in (None, "", {})}
    # A usable JSON-LD hit needs identity plus at least one listing signal
    # (price, odometer, or the site's own id) — filters out breadcrumb junk.
    if ("make" in raw or "raw_title" in raw) and \
            ("price" in raw or "odometer" in raw or "listing_id" in raw):
        return raw
    return None


def detect_source(html: str) -> str:
    head = html[:200_000].casefold()
    for hint, name in _SITE_HINTS:
        if hint in head:
            return name
    return "other"


def parse(path: str | Path) -> tuple[list[dict], str]:
    """-> (raw dicts, detected source name)."""
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    source = detect_source(html)

    raws: list[dict] = []
    for m in _LDJSON.finditer(html):
        blob = unescape(m.group(1)).strip()
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        found: list[dict] = []
        _walk_ld(data, found)
        for obj in found:
            raw = _ld_to_raw(obj)
            if raw:
                raws.append(raw)
    if raws:
        # join per-listing seller/state from the page's tracking attributes
        tracking = _tracking_supplement(html)
        if tracking:
            for raw in raws:
                adid = raw.get("listing_id")
                info = tracking.get(str(adid)) if adid else None
                if info:
                    raw.setdefault("state", info.get("state"))
                    raw.setdefault("seller_type", info.get("seller_type"))
                    # setdefault(None) still leaves None: drop empties
                    for k in ("state", "seller_type"):
                        if raw.get(k) is None:
                            raw.pop(k, None)
        return raws, source

    extractor = _TextExtractor()
    extractor.feed(html)
    return pastetext.parse(extractor.text()), source
