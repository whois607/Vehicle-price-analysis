"""Which websites your saved pages come from - kept OUT of the code.

The code names no website. Everything that depends on one particular site
lives in a small private file, `data/sites.json` (or the file named by the
VPA_SITES environment variable), which is never committed or packaged:

* which label a saved page is stored under (its `source`), recognised by
  words in the page or a link (`domains`);
* the address a site's bare advert paths belong to (`address`), so an
  "open the advert" link can be built;
* which sites' listings carry no usable link (`links: false`, with any
  extra words that mark such a link in `link_words`);
* the path starts of an advert address (`ad_paths`), the site ad-id
  prefixes (`id_prefixes`), the photo server (`photos`), the site a
  "search for it" link searches (`search_site`), and the label a single
  saved advert is stored under (`default_source`), and the most listings
  one search shows (`max_results`, for planning searches that fit -
  sweeps.py; a site may also get a `total_pattern` there).

Without the file the app still works: every import is labelled `other`,
there are no advert links, searches or photos, and no id counts as a site
ad id. See `sites.example.json` for the shape. Nothing here reaches a
website; it only reads text you already saved.

A source label is part of every stored car's identity (schema.dedupe_key),
so once data is stored under a label the file must keep naming it exactly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "data" / "sites.json"
#: labels that are the app's own, never a website's
OWN_SOURCES = ("manual", "v2", "other")

_cache: dict = {}


def path() -> Path:
    env = os.environ.get("VPA_SITES")
    return Path(env) if env else PRIVATE


def config() -> dict:
    """The site file, read once per change; {} when there is none."""
    p = path()
    try:
        mt = p.stat().st_mtime_ns
    except OSError:
        return {}
    hit = _cache.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{p} could not be read as JSON: {e}") from None
    if not isinstance(cfg, dict):
        raise ValueError(f"{p} must hold a JSON object")
    _cache[str(p)] = (mt, cfg)
    return cfg


def sites() -> list[dict]:
    """The configured sites, in the file's order (detection order)."""
    return [s for s in config().get("sites", []) if isinstance(s, dict) and s.get("source")]


def labels() -> list[str]:
    """Source labels for a picker: the sites, in order (the first is the
    default where a picker has no "detect" choice)."""
    return [str(s["source"]) for s in sites()]


def options_html() -> str:
    """<option>s for a source picker: each site, then "other"."""
    from html import escape
    return "".join(f'<option value="{escape(x)}">{escape(x)}</option>'
                   for x in labels() + ["other"] if x)


def _domains(s: dict) -> list[str]:
    return [str(d).casefold() for d in s.get("domains", []) if d]


def detect(text: str, limit: int = 200_000) -> str:
    """The label of the first site whose domain appears in the text, else
    'other'."""
    head = (text or "")[:limit].casefold()
    for s in sites():
        if any(d in head for d in _domains(s)):
            return str(s["source"])
    return "other"


def address(source) -> str | None:
    """The site address a bare advert path from this source belongs to."""
    for s in sites():
        if str(s["source"]) == str(source) and s.get("address"):
            return str(s["address"]).rstrip("/")
    return None


def default_source() -> str:
    return str(config().get("default_source") or "other")


def id_prefixes() -> tuple[str, ...]:
    return tuple(str(p) for p in config().get("id_prefixes", []) if p)


def search_site() -> str | None:
    return config().get("search_site") or None


def photos() -> dict:
    """{'server': 'host.example', 'size': 'query with {w} and {h}'} or {}."""
    p = config().get("photos") or {}
    return p if isinstance(p, dict) and p.get("server") else {}


def link_rules() -> dict:
    """What a pasted advert link is checked against (laptop and phone alike):
    `unlinked` - words of sites whose listings were captured without links;
    `words` - words that mark a link as an advert of a known site (labels
    and domains); `paths` - the path starts of an advert address."""
    unlinked, words, paths = [], [], []
    for s in sites():
        if s.get("links") is False:
            unlinked += _domains(s) + [str(w).casefold() for w in s.get("link_words", []) if w]
        else:
            words += [str(s["source"]).casefold()] + _domains(s)
            paths += [str(p).casefold() for p in s.get("ad_paths", []) if p]
    return {"unlinked": unlinked, "words": words, "paths": paths}


def private_words() -> list[str]:
    """Every site-specific word in the PRIVATE file (data/sites.json, not
    VPA_SITES) - what tests/check_neutral.py makes sure no shared file holds."""
    try:
        cfg = json.loads(PRIVATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = set()
    for s in cfg.get("sites", []):
        src = str(s.get("source") or "")
        if src and src not in OWN_SOURCES:
            out.add(src)
        out.update(str(d) for d in s.get("domains", []) if d)
        if s.get("address"):
            out.add(str(s["address"]).split("://")[-1].removeprefix("www.").rstrip("/"))
        out.update(str(w) for w in s.get("link_words", []) if w)
    out.update(str(p) for p in cfg.get("id_prefixes", []) if p)
    out.update(str(w) for w in cfg.get("private_words", []) if w)
    if cfg.get("search_site"):
        out.add(str(cfg["search_site"]))
    if (cfg.get("photos") or {}).get("server"):
        out.add(str(cfg["photos"]["server"]))
    return sorted(w for w in out if len(w) >= 4)
