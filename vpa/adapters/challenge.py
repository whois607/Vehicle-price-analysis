"""Recognising a bot-protection challenge page saved instead of a real page.

When a listing site decides a request looks automated it serves an
interstitial — a CAPTCHA or "checking your browser" page — with HTTP 200. Save
that with Ctrl+S and you get a file that *looks* like a capture, is a few
hundred bytes, and contains no listings at all.

Measured on the real `data/raw/` folder on 06/08/2026: **4 of 48 saved
captures were challenge pages**, each exactly 777 bytes:

    Audi cars for sale in Australia
    Ford cars for sale in Australia
    Mercedes-Benz cars for sale in Australia
    Ford Falcon FG MkII Sedan cars for sale in Australia

Their whole body is:

    <p id="cmsg">Please enable JS and disable any ad blocker</p>
    <script ... src="https://ct.captcha-delivery.com/i.js"></script>

This module only *identifies* them, so the app can say what happened instead
of reporting "0 listings" and leaving you to guess whether the page was empty,
the parser broke, or the save failed. It does not and must not do anything to
get past a challenge: working around a site's bot protection is off limits,
and the fix is always to re-save the page in a normal browser session.

**The marker is the interstitial, not the vendor.** A site's every page may
load its bot-protection vendor's ordinary tag; only the challenge page
contacts `captcha-delivery.com`. Keying on "datadome" would flag all 48
files. Verified: `captcha-delivery` appears in 4 of 48 and in none of the 44
good ones.
"""
from __future__ import annotations

import re

#: (marker, vendor) — each marker appears ONLY on an interstitial, never on a
#: normal page of the same site. Keep that property when adding to this list.
_MARKERS: tuple[tuple[re.Pattern, str], ...] = (
    # DataDome: the challenge fetches its script from captcha-delivery.com
    (re.compile(r"captcha-delivery\.com", re.I), "DataDome"),
    (re.compile(r"""\bdd\s*=\s*\{[^}]*['"]hsh['"]""", re.I), "DataDome"),
    # Cloudflare's "Checking your browser" / managed challenge
    (re.compile(r"cf-browser-verification|__cf_chl_|cf_chl_opt", re.I),
     "Cloudflare"),
    (re.compile(r"<title>\s*Just a moment", re.I), "Cloudflare"),
    # generic WAF block pages
    (re.compile(r"<title>\s*(Access Denied|Attention Required|"
                r"Request Rejected)", re.I), "the site's firewall"),
    (re.compile(r"\bPerimeterX\b|\b_px(?:Captcha|Config)\b"), "PerimeterX"),
    (re.compile(r"\bhcaptcha\.com/captcha|recaptcha/api\.js\?render", re.I),
     "a CAPTCHA"),
)

#: A real results page is hundreds of kilobytes. This is only a corroborating
#: signal — never the trigger on its own, because a genuine search with one
#: result is small too, and a genuine empty search is legitimate.
SMALL_PAGE_BYTES = 20_000


def detect(html: str) -> str | None:
    """Name the bot-protection vendor if this is a challenge page, else None.

    Returns a vendor name rather than True so the message can say which
    system fired — "the site's bot protection (DataDome)" tells you the page
    needs re-saving; "could not parse" sends you looking at the parser.
    """
    if not html:
        return None
    head = html[:400_000]
    for pattern, vendor in _MARKERS:
        if pattern.search(head):
            return vendor
    return None


def explain(html: str, name: str = "") -> str | None:
    """The message shown to the user, or None when the page looks genuine."""
    vendor = detect(html)
    if not vendor:
        return None
    size = len(html.encode("utf-8", errors="ignore"))
    what = f"<b>{name}</b> is not a listings page" if name else "That is not a listings page"
    return (
        f"{what} — it is the site's bot-protection challenge "
        f"({vendor}), saved instead of the results. It is {size:,} bytes and "
        f"holds no listings; a real results page is several hundred kilobytes."
        "<br><br>This happens when a burst of page loads looks automated. "
        "Open that search again in your browser, complete the check the site "
        "shows you, wait for the listings to actually render, then Ctrl+S "
        "again. Nothing was imported."
    )
