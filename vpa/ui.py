"""Shared page chrome: the nav bar, its icons, the one stylesheet, and the
behaviour every page has in common.

Every page used to hand-roll its own header, so "Explore" was a link on one
page and a button on another, the current page was never marked, and
`Shut down` looked exactly like `Quick add` - one navigates, the other kills
the app. This is the single place that is now defined.

Pages embed `__CHROME_CSS__` at the TOP of their <style> (so their own rules
come after it and win), `__NAV__` and `__ACTIONS__` in their body, and
`__CHROME_JS__` at the end of their script; `chrome(page, active, actions)`
substitutes all four. Icons are inline SVG paths - no font, no CDN, no
network, consistent with every chart in this project.
"""
from __future__ import annotations

import json
from pathlib import Path

#: 16x16 stroke icons on a 24-unit grid, drawn from one visual family:
#: 2-unit round strokes, no fills, so they sit evenly beside 13px text.
ICONS = {
    # four panels - the dataset at a glance
    "overview": "M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z",
    # tray with an arrow coming down into it - bringing listings in
    "import": "M12 3v10m0 0 4-4m-4 4-4-4M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3",
    # magnifier - looking one vehicle up
    "explore": "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4.2-4.2",
    # two columns side by side - comparing
    "compare": "M4 20V10m5 10V4m5 16v-7m5 7V7",
    # lightning - the fast entry path
    "quick": "M13 3 5 14h6l-1 7 8-11h-6l1-7z",
    # stacked layers with an arrow into them - many files at once, no preview
    "bulk": "M12 3 3 7.5 12 12l9-4.5L12 3zM3 12.5 12 17l9-4.5M3 17.5 12 22l9-4.5",
    # grid - the spreadsheet
    "excel": "M4 5h16v14H4zM4 10h16M4 15h16M10 5v14",
    # question mark in a circle - help
    "help": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM9.5 9.2a2.6 2.6 0 0 1 5 .9c0 1.8-2.5 2.2-2.5 3.9M12 17.2h.01",
    # tick in a circle - the data check
    "check": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8.2 12.2l2.6 2.6 5-5.4",
    # power symbol - stops the app
    "power": "M12 3v9M7.5 6.4a7.5 7.5 0 1 0 9 0",
    # a price tag - trim names and how they are grouped
    "trims": "M3 12V4h8l9 9-8 8-9-9zM7.5 7.5h.01",
    # two overlapping cards - one car stored twice
    "dupes": "M8 8h12v12H8zM16 8V4H4v12h4",
    # a phone - carrying the data to the car yard
    "phone": "M8 2h8a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM11 18h2",
    # rising line - the brand mark: prices over time
    "trend": "M3 17l6-6 4 4 8-8M15 7h6v6",
    # circular arrow - rebuild in place, without opening anything
    "rebuild": "M20 12a8 8 0 1 1-2.6-5.9M20 4v4h-4",
    # theme: a screen (follow Windows), a sun (light), a moon (dark)
    "t-auto": "M3 5h18v11H3zM8 20h8M12 16v4",
    "t-light": "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
    "t-dark": "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z",
}

#: (setting, label) - the theme menu. "auto" follows Windows' own setting.
THEMES = [("auto", "Match Windows"), ("light", "Light"), ("dark", "Dark")]

#: Dash themes - the colours of an instrument cluster, each in a light and a
#: dark version. Light or dark is still chosen apart (THEMES, or following
#: Windows); the dash only recolours. Values are CSS tokens (CHROME_CSS
#: names); `palette_css` writes them for the laptop pages and, renamed, for
#: the phone app (vpa/phone_app/app.css holds a generated copy - see
#: write_phone_palettes). Order = the menu's order; the first is the default.
#:   needle  the second colour of a dash: the active tab's underline
#:   digits  the big figures - lit like a dash readout in dark
#:   glow    the lit edge of the logo and the figures (dark only)
#:   chart   the line of a single-series chart (collected over time)
#:   heat0-4 the capture-day calendar, quietest to busiest
PALETTES = {
    "classic": {
        "label": "Classic", "blurb": "green instrument glow",
        "light": {
            "bg": "#f3f6f3", "card": "#fff", "head": "#f5f8f6", "line": "#e0e8e2", "line-2": "#edf2ee",
            "tile-a": "#fff", "tile-b": "#f8fbf9", "topbar": "rgba(255,255,255,.86)",
            "topbar-line": "rgba(208,222,212,.9)", "field-bg": "#fff", "seg-on": "#fff",
            "accent": "#1d7a45", "accent-2": "#155f35", "accent-soft": "#e6f4eb", "accent-line": "#bfe1cc",
            "ring": "0 0 0 3px rgba(29,122,69,.18)", "fill": "#1d7a45", "fill-2": "#155f35",
            "sel": "#c6e8d3", "row-hover": "#f4faf6", "help-a": "#f3fbf6", "needle": "#1d7a45",
            "logo": "linear-gradient(135deg,#34a865,#11502c)", "digits": "var(--ink)",
            "glow": "0 0 0 transparent", "glow-text": "none", "chart": "#1d7a45",
            "heat0": "#e6ebe7", "heat1": "#c9e6c9", "heat2": "#8fcb8f", "heat3": "#4d9e4d", "heat4": "#256e25"},
        "dark": {
            "bg": "#070c09", "card": "#0f1612", "head": "#131c17", "line": "#1f2c24", "line-2": "#18221c",
            "tile-a": "#111a15", "tile-b": "#0f1612", "topbar": "rgba(15,22,18,.85)",
            "topbar-line": "rgba(44,64,52,.9)", "field-bg": "#131c17", "seg-on": "#1e2c24",
            "accent": "#4ee08a", "accent-2": "#8af0b3", "accent-soft": "#0f2e1c", "accent-line": "#22603c",
            "ring": "0 0 0 3px rgba(78,224,138,.3)", "fill": "#1f8a4c", "fill-2": "#27a05a",
            "sel": "#1d5534", "row-hover": "#142019", "help-a": "#0f2418", "needle": "#4ee08a",
            "logo": "linear-gradient(135deg,#2fd07a,#0b4a27)", "digits": "#9ef5c2",
            "glow": "0 0 14px rgba(78,224,138,.45)", "glow-text": "0 0 10px rgba(78,224,138,.55)",
            "chart": "#4ee08a",
            "heat0": "#17211b", "heat1": "#14462a", "heat2": "#1f7a44", "heat3": "#35b866", "heat4": "#7dffb0"},
    },
    "xr": {
        "label": "XR", "blurb": "Falcon blue, red needles",
        "light": {
            "bg": "#f1f4fb", "card": "#fff", "head": "#f4f7fd", "line": "#dfe5f2", "line-2": "#ecf0f8",
            "tile-a": "#fff", "tile-b": "#f7f9fe", "topbar": "rgba(255,255,255,.86)",
            "topbar-line": "rgba(205,215,236,.9)", "field-bg": "#fff", "seg-on": "#fff",
            "accent": "#1a4fd6", "accent-2": "#133fae", "accent-soft": "#e7eefd", "accent-line": "#c2d2f7",
            "ring": "0 0 0 3px rgba(26,79,214,.18)", "fill": "#1a4fd6", "fill-2": "#133fae",
            "sel": "#c9d8fb", "row-hover": "#f4f7ff", "help-a": "#f2f6ff", "needle": "#d61f2c",
            "logo": "linear-gradient(135deg,#2d6bff 0%,#1a3fae 62%,#d61f2c 63%)", "digits": "var(--ink)",
            "glow": "0 0 0 transparent", "glow-text": "none", "chart": "#1a4fd6",
            "heat0": "#e5e9f2", "heat1": "#c4d4f6", "heat2": "#86a6ef", "heat3": "#2f5fd6", "heat4": "#d61f2c"},
        "dark": {
            "bg": "#050914", "card": "#0c1224", "head": "#10182e", "line": "#1c2744", "line-2": "#151e36",
            "tile-a": "#0e152b", "tile-b": "#0c1224", "topbar": "rgba(12,18,36,.86)",
            "topbar-line": "rgba(40,58,100,.9)", "field-bg": "#10182e", "seg-on": "#1b2748",
            "accent": "#4c8dff", "accent-2": "#8db8ff", "accent-soft": "#11224d", "accent-line": "#29489a",
            "ring": "0 0 0 3px rgba(76,141,255,.35)", "fill": "#2a62e6", "fill-2": "#3a74f5",
            "sel": "#213f8c", "row-hover": "#111b35", "help-a": "#0f1c3e", "needle": "#ff3340",
            "logo": "linear-gradient(135deg,#3d7dff 0%,#1b3a9a 62%,#ff3340 63%)", "digits": "#a9c8ff",
            "glow": "0 0 14px rgba(76,141,255,.5)", "glow-text": "0 0 10px rgba(76,141,255,.6)",
            "chart": "#4c8dff",
            "heat0": "#121a30", "heat1": "#16306a", "heat2": "#2152c0", "heat3": "#4c8dff", "heat4": "#ff3340"},
    },
    "m": {
        "label": "M", "blurb": "M stripes and loud paint",
        "light": {
            "bg": "#f5f4f9", "card": "#fff", "head": "#f7f6fb", "line": "#e5e2ef", "line-2": "#f0eef6",
            "tile-a": "#fff", "tile-b": "#faf9fd", "topbar": "rgba(255,255,255,.86)",
            "topbar-line": "rgba(220,214,234,.9)", "field-bg": "#fff", "seg-on": "#fff",
            "accent": "#6d28d9", "accent-2": "#5b21b6", "accent-soft": "#f1eafd", "accent-line": "#d9c6f7",
            "ring": "0 0 0 3px rgba(109,40,217,.18)", "fill": "#6d28d9", "fill-2": "#5b21b6",
            "sel": "#dccbf8", "row-hover": "#f8f5fe", "help-a": "#f7f3ff", "needle": "#e4002b",
            "logo": "linear-gradient(135deg,#1c9be0 0 34%,#24307f 34% 66%,#e4002b 66%)",
            "digits": "var(--ink)", "glow": "0 0 0 transparent", "glow-text": "none", "chart": "#6d28d9",
            "heat0": "#e9e6f0", "heat1": "#7cc4f2", "heat2": "#3fbf73", "heat3": "#f2c80f", "heat4": "#ff5a1f"},
        "dark": {
            "bg": "#08070c", "card": "#121018", "head": "#17141f", "line": "#262133", "line-2": "#1d1928",
            "tile-a": "#15121c", "tile-b": "#121018", "topbar": "rgba(18,16,24,.86)",
            "topbar-line": "rgba(60,50,80,.9)", "field-bg": "#17141f", "seg-on": "#2a2338",
            "accent": "#b77cff", "accent-2": "#d6b3ff", "accent-soft": "#2a1746", "accent-line": "#5a3394",
            "ring": "0 0 0 3px rgba(183,124,255,.35)", "fill": "#7c3aed", "fill-2": "#8b5cf6",
            "sel": "#45267a", "row-hover": "#1b1726", "help-a": "#1e1530", "needle": "#ff2d55",
            "logo": "linear-gradient(135deg,#29b6ff 0 34%,#3949c7 34% 66%,#ff2d55 66%)",
            "digits": "#ffe14d", "glow": "0 0 16px rgba(183,124,255,.45)",
            "glow-text": "0 0 10px rgba(255,225,77,.45)", "chart": "#ffe14d",
            "heat0": "#1a1722", "heat1": "#1d5fd1", "heat2": "#1fbf6a", "heat3": "#ffe14d", "heat4": "#ff6a13"},
    },
    "amber": {
        "label": "Amber", "blurb": "old-school orange gauges",
        "light": {
            "bg": "#faf6f0", "card": "#fff", "head": "#fcf8f2", "line": "#ece3d6", "line-2": "#f5efe6",
            "tile-a": "#fff", "tile-b": "#fdfaf5", "topbar": "rgba(255,255,255,.86)",
            "topbar-line": "rgba(232,220,203,.9)", "field-bg": "#fff", "seg-on": "#fff",
            "accent": "#b45309", "accent-2": "#92400e", "accent-soft": "#fdf0e0", "accent-line": "#f2d1a8",
            "ring": "0 0 0 3px rgba(180,83,9,.18)", "fill": "#b45309", "fill-2": "#92400e",
            "sel": "#f7d8b3", "row-hover": "#fdf8f1", "help-a": "#fff8ee", "needle": "#b45309",
            "logo": "linear-gradient(135deg,#f59e0b,#9a3d06)", "digits": "var(--ink)",
            "glow": "0 0 0 transparent", "glow-text": "none", "chart": "#b45309",
            "heat0": "#efe9e0", "heat1": "#f8d9ae", "heat2": "#f0b061", "heat3": "#d9801f", "heat4": "#9a4a06"},
        "dark": {
            "bg": "#0d0905", "card": "#17110a", "head": "#1d150c", "line": "#322517", "line-2": "#261c11",
            "tile-a": "#1a130b", "tile-b": "#17110a", "topbar": "rgba(23,17,10,.86)",
            "topbar-line": "rgba(80,58,30,.9)", "field-bg": "#1d150c", "seg-on": "#33261a",
            "accent": "#ffa53a", "accent-2": "#ffc67f", "accent-soft": "#3a230b", "accent-line": "#7a4813",
            "ring": "0 0 0 3px rgba(255,165,58,.32)", "fill": "#c4640c", "fill-2": "#db7614",
            "sel": "#6a3a10", "row-hover": "#20170d", "help-a": "#2a1a0a", "needle": "#ff6a1a",
            "logo": "linear-gradient(135deg,#ffb347,#a34a05)", "digits": "#ffc067",
            "glow": "0 0 14px rgba(255,165,58,.45)", "glow-text": "0 0 10px rgba(255,165,58,.55)",
            "chart": "#ffa53a",
            "heat0": "#1e170f", "heat1": "#4a2c0d", "heat2": "#8a4f12", "heat3": "#d27a1c", "heat4": "#ffb347"},
    },
}
DEFAULT_PALETTE = next(iter(PALETTES))

#: laptop token -> phone token (vpa/phone_app/app.css names), for the ones
#: the phone has
_PHONE_TOKENS = {"bg": "bg", "card": "card", "line": "line", "line-2": "line2",
                 "accent": "accent", "accent-2": "accent2", "accent-soft": "accent-soft"}


def _rgba(hexc: str, a: float) -> str:
    h = hexc.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def _sel(name: str, mode: str) -> str:
    # light blocks must not apply in dark: equal specificity would let
    # source order decide
    return (f':root[data-palette={name}]:not([data-theme=dark])' if mode == "light"
            else f':root[data-palette={name}][data-theme=dark]')


def palette_css(target: str = "laptop") -> str:
    """Every dash theme's tokens, light and dark, as CSS rules."""
    out = []
    for name, p in PALETTES.items():
        for mode in ("light", "dark"):
            t = p[mode]
            if target == "phone":
                kv = {_PHONE_TOKENS[k]: v for k, v in t.items() if k in _PHONE_TOKENS}
                kv.update({"c0": t["accent"], "on-accent": "#fff" if mode == "light" else t["bg"],
                           "band": _rgba(t["accent"], .13 if mode == "light" else .16),
                           "band2": _rgba(t["accent"], .22 if mode == "light" else .26),
                           "digits": t["digits"],
                           "glow-text": t["glow-text"]})
            else:
                kv = t
            out.append(_sel(name, mode) + "{" + ";".join(f"--{k}:{v}" for k, v in kv.items()) + "}")
    return "\n".join(out)


#: the phone's app.css carries a generated copy between these markers
PHONE_MARK = ("/* dash themes: generated from vpa/ui.py PALETTES (ui.write_phone_palettes) */",
              "/* end of dash themes */")


def write_phone_palettes() -> bool:
    """Bring app.css's copy of the dash themes up to date. True if changed."""
    css = Path(__file__).resolve().parent / "phone_app" / "app.css"
    text = css.read_text(encoding="utf-8")
    a, b = PHONE_MARK
    block = f"{a}\n{palette_css('phone')}\n{b}"
    if a in text:
        new = text[:text.index(a)] + block + text[text.index(b) + len(b):]
    else:
        anchor = "*{box-sizing:border-box"
        new = text.replace(anchor, block + "\n" + anchor, 1)
    if new != text:
        css.write_bytes(new.encode("utf-8"))           # keep LF line ends
    return new != text

#: (slug, href, label, icon) - the pages you can be ON. Four, not six:
#: Import, Bulk and Quick add were three tabs for one job, and are now the
#: three modes of Add data (MODES below). The dataset overview, which used to
#: be the bottom of the Import page, is the home page.
PAGES = [
    ("overview", "/", "Overview", "overview"),
    ("add", "/add", "Add data", "import"),
    ("explore", "/explore", "Explore", "explore"),
    ("compare", "/compare", "Compare", "compare"),
]

#: (slug, href, label, icon, what it is for) - the ways of adding data, in
#: order of care: the careful one first, the fast ones after it.
MODES = [
    ("import", "/add", "Import with preview", "import",
     "saved pages, CSV or pasted text; check each row first"),
    ("bulk", "/bulk", "Bulk upload", "bulk",
     "many files at once, no preview; backed up first and undoable"),
    ("quick", "/quick", "Quick add", "quick",
     "type in asking prices you saw, a model at a time"),
]


def icon(name: str, size: int = 16) -> str:
    """One inline SVG icon."""
    return (f'<svg class="ico" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'<path d="{ICONS[name]}"/></svg>')


def nav(active: str) -> str:
    """The header: page tabs on the left, the utility cluster on the right.
    It is identical on every page - page-specific actions go in the page
    header (`__ACTIONS__`), not here, because putting them here made the bar
    a different width on each page and wrapped the tabs onto two lines on
    Import. `active` marks the current page."""
    tabs = "".join(
        f'<a class="navtab{" on" if slug == active else ""}" href="{href}"'
        f'{" aria-current=\"page\"" if slug == active else ""}>'
        f'{icon(ic)}<span>{label}</span></a>'
        for slug, href, label, ic in PAGES)
    # the theme switch: an icon per choice (CSS shows the current one), and its menu
    th_icons = "".join(icon("t-" + k).replace('class="ico"', f'class="ico ti-{k}"', 1)
                       for k, _ in THEMES)
    th_items = "".join(f'<button type="button" role="menuitemradio" data-theme-set="{k}">'
                       f'{icon("t-" + k)}<span>{label}</span></button>' for k, label in THEMES)
    # the dash themes, each with a swatch: its dark face and its two colours
    th_items += '<div class="tmh">Dash</div>' + "".join(
        f'<button type="button" class="pal" role="menuitemradio" data-palette-set="{k}">'
        f'<i class="sw" style="background:linear-gradient(135deg,{p["dark"]["bg"]} 0 45%,'
        f'{p["dark"]["accent"]} 45% 78%,{p["dark"]["needle"]} 78%)"></i>'
        f'<span>{p["label"]}<small>{p["blurb"]}</small></span></button>'
        for k, p in PALETTES.items())
    return f"""<div id="vpaload" aria-hidden="true"><i></i></div>
<header class="topbar" data-sticky="1">
 <a class="brand" href="/" style="text-decoration:none"><span class="logo">{icon("trend", 17)}</span><span class="bt"><b>Vehicle prices</b><small id="navstat"></small></span></a>
 <nav class="navtabs">{tabs}</nav>
 <div class="navacts">
  <a class="navbtn dup" id="dupBtn" href="/duplicates" hidden title="the same car stored as two identities - review">{icon("dupes")}<span id="dupTxt">Duplicates</span></a>
  <a class="navbtn{" on" if active == "trims" else ""}" id="trimsBtn" href="/trims" title="how trim names are grouped, and what the rules don't cover yet">{icon("trims")}<span>Trims</span></a>
  <a class="navbtn{" on" if active == "phone" else ""}" id="phoneBtn" href="/phone" title="sync your data to your phone">{icon("phone")}<span>Phone</span></a>
  <button class="navbtn" id="xlsxBtn" title="rebuild viewer.xlsx from the database and open it in Excel">{icon("excel")}<span>Excel</span></button>
  <span class="navsep"></span>
  <span class="themewrap"><button class="navbtn" id="themeBtn" aria-haspopup="menu" aria-expanded="false" title="Theme" aria-label="Theme">{th_icons}</button>
   <div class="thememenu" id="themeMenu" role="menu" hidden>{th_items}</div></span>
  <button class="navbtn" id="helpBtn" title="How to use this page" aria-label="How to use this page">{icon("help")}<span>How to use</span></button>
  <button class="navbtn danger" id="quitBtn" title="Shut down the app - your data is already saved" aria-label="Shut down">{icon("power")}<span>Shut down</span></button>
 </div>
</header>"""


def modes(active: str) -> str:
    """The mode switch shared by the three Add data pages. Each mode is its
    own page and URL (so a bookmark or the desktop shortcut can land on one),
    but they read as one tab with three ways of working."""
    items = "".join(
        f'<a class="mode{" on" if slug == active else ""}" href="{href}"'
        f'{" aria-current=\"page\"" if slug == active else ""}>'
        f'{icon(ic)}<span class="mode-t"><b>{label}</b><span>{desc}</span></span></a>'
        for slug, href, label, ic, desc in MODES)
    return f'<nav class="modes" aria-label="ways to add data">{items}</nav>'


def action(btn_id: str, label: str, ic: str, title: str = "") -> str:
    """A page-specific action button for the page header's actions slot."""
    return (f'<button class="navbtn" id="{btn_id}" title="{title}">'
            f'{icon(ic)}<span>{label}</span></button>')


#: The one stylesheet. Every page used to carry its own copy of the base
#: rules and the copies had drifted: five page widths, three button styles,
#: two grey scales, and a page-level `.danger` on Explore that painted
#: Shut down solid red with invisible text. Pages now add only what is
#: genuinely theirs, and use these tokens when they do.
#:
#: Element rules are wrapped in :where() so they carry ZERO specificity -
#: any rule a page writes (`td input`, `.fixgrid input`) wins without a fight.
CHROME_CSS = """
 :root{
   color-scheme:light;
   --ui:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
   --ui-d:"Segoe UI Variable Display","Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
   --bg:#f4f5f8; --card:#fff;
   --ink:#151a24; --ink-2:#3d4656; --dim:#667083; --faint:#9ea5b3;
   --line:#e4e7ed; --line-2:#eff1f5; --field:#d0d6df; --head:#f7f8fa;
   --accent:#2b5fc4; --accent-2:#224ea5; --accent-soft:#edf2fc; --accent-line:#c8d7f3;
   --good:#23793a; --good-soft:#e8f4ec; --good-line:#c5e2cf;
   --bad:#b23a3a; --bad-soft:#fcefee; --bad-line:#efcaca;
   --warn:#8a5a12; --warn-soft:#fff6e6; --warn-line:#f0d7ab;
   --mine:#e8a317;
   --r:14px; --r-sm:9px;
   --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.05);
   --shadow-lg:0 10px 30px -12px rgba(16,24,40,.25);
   --ring:0 0 0 3px rgba(43,95,196,.18);
   --navh:74px;               /* sticky header's bottom edge, for other sticky bars */
   /* surfaces and states that used to be written as literal colours */
   --field-bg:#fff; --hover-line:#b9c1ce; --row-hover:#f7f9fe; --sel:#cfdcf6;
   --topbar:rgba(255,255,255,.86); --topbar-line:rgba(214,219,228,.9);
   --tile-a:#fff; --tile-b:#fafbfc; --help-a:#f8fbff;
   --seg-hover:rgba(255,255,255,.6); --seg-on:#fff; --dot-line:rgba(0,0,0,.25);
   /* solid fills carrying white text. In light they ARE the accent / good
      colours; in dark the text colours turn pale, and white on pale is unreadable */
   --fill:#2b5fc4; --fill-2:#224ea5; --on-fill:#fff; --on-fill-dim:#dbe6f6; --fill-off:#b3bbc7;
   --fill-good:#23793a; --fill-good-2:#1d6630; --fill-warn:#8a5a12; --fill-bad:#b23a3a;
   --bad-ico:#a04848; --bad-ink:#7a2f2f; --bad-ink-2:#8d3b3b; --bad-line-2:#e3b3b3;
   --good-ink:#1f5a2c;
   --warn-ico:#b07a1e; --warn-ink:#6f470c; --warn-hover:#fdeccd; --warn-hover-line:#e6c48f;
   /* dash-theme tokens (PALETTES); these are the fallbacks */
   --needle:var(--accent); --logo:linear-gradient(135deg,#3a6fd8,#1f3f8f); --digits:var(--ink);
   --glow:0 0 0 transparent; --glow-text:none; --chart:#2e7d32;
 }
 /* Dark. The theme is resolved before the page paints (THEME_HEAD) to
    data-theme="light" or "dark" on <html>, whether chosen or following
    Windows, so this is the one place the dark values live. Same palette as
    the phone app's dark mode. */
 :root[data-theme=dark]{
   color-scheme:dark;
   --bg:#0f1218; --card:#181c24;
   --ink:#e8ebf1; --ink-2:#c3c9d4; --dim:#9aa3b2; --faint:#6b7383;
   --line:#2a303b; --line-2:#222833; --field:#3a4250; --head:#1d222b;
   --accent:#7ea6f4; --accent-2:#a3c0f8; --accent-soft:#1d2a44; --accent-line:#34507f;
   --good:#5cc28c; --good-soft:#16291f; --good-line:#2b5a3f;
   --bad:#f08a80; --bad-soft:#331a18; --bad-line:#6b3430;
   --warn:#e7b45a; --warn-soft:#2f2614; --warn-line:#5e4a22;
   --mine:#f0b429;
   --shadow:0 1px 2px rgba(0,0,0,.35);
   --shadow-lg:0 12px 32px -12px rgba(0,0,0,.75);
   --ring:0 0 0 3px rgba(126,166,244,.32);
   --field-bg:#1d222b; --hover-line:#4c5566; --row-hover:#1e2431; --sel:#2d4a80;
   --topbar:rgba(24,28,36,.84); --topbar-line:rgba(58,66,80,.9);
   --tile-a:#1b2029; --tile-b:#181c24; --help-a:#1a2233;
   --seg-hover:rgba(255,255,255,.06); --seg-on:#2c3340; --dot-line:rgba(255,255,255,.35);
   --fill:#3d6fd6; --fill-2:#4c7de2; --on-fill:#fff; --on-fill-dim:#dbe6f6; --fill-off:#3a4250;
   --fill-good:#2a8a48; --fill-good-2:#319a53; --fill-warn:#9a6a1c; --fill-bad:#c0453f;
   --bad-ico:#f08a80; --bad-ink:#f4a39b; --bad-ink-2:#f08a80; --bad-line-2:#8a4440;
   --good-ink:#7fd1a4;
   --warn-ico:#e7b45a; --warn-ink:#f0c27a; --warn-hover:#3a2f17; --warn-hover-line:#7a6030;
   --chart:#5cc28c;
 }
 /* the dash themes: each recolours light and dark (PALETTES) */
 __PALETTES__
 __CHART_DARK__
 *,*::before,*::after{box-sizing:border-box}
 html{scroll-padding-top:var(--navh)}
 body,button,select,input,textarea{font-family:var(--ui)}
 body{font-size:14px;line-height:1.45;margin:0;background:var(--bg);color:var(--ink);
   -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
 ::selection{background:var(--sel)}
 .wrap{max-width:1240px;margin:0 auto;padding:12px 20px 56px}
 table,.tile b,.num,.v{font-variant-numeric:tabular-nums}

 /* ---- header: floats, and stays put while the page scrolls ---- */
 .topbar{position:sticky;top:10px;z-index:60;display:flex;align-items:center;gap:14px;
   background:var(--topbar);-webkit-backdrop-filter:saturate(1.6) blur(14px);
   backdrop-filter:saturate(1.6) blur(14px);border:1px solid var(--topbar-line);
   border-radius:var(--r);padding:7px 8px 7px 10px;margin-bottom:22px;
   box-shadow:0 1px 2px rgba(16,24,40,.04),0 6px 20px -10px rgba(16,24,40,.18)}
 .brand{display:flex;align-items:center;gap:9px;white-space:nowrap;padding-right:6px}
 .brand .logo{width:30px;height:30px;border-radius:9px;flex:none;display:grid;place-items:center;
   color:#fff;background:var(--logo);
   box-shadow:inset 0 1px 0 rgba(255,255,255,.25),0 1px 2px rgba(0,0,0,.3),var(--glow)}
 .brand .logo .ico{vertical-align:0}
 .brand .bt{display:flex;flex-direction:column;line-height:1.15}
 .brand b{font-size:13.5px;font-weight:650;color:var(--ink);letter-spacing:-.01em}
 .brand small{font-size:11px;color:var(--dim);cursor:help}
 .ico{flex:none;vertical-align:-3px}
 .navtabs{display:flex;gap:2px;flex:1;min-width:0}
 .navtab{display:inline-flex;align-items:center;gap:7px;height:34px;padding:0 12px;
   border-radius:var(--r-sm);font-size:13px;text-decoration:none;color:var(--ink-2);
   white-space:nowrap;transition:background .15s,color .15s}
 .navtab .ico{color:var(--faint);transition:color .15s}
 .navtab:hover{background:var(--line-2);color:var(--ink)}
 .navtab:hover .ico{color:var(--dim)}
 .navtab.on{background:var(--accent-soft);color:var(--accent-2);font-weight:600;
   box-shadow:inset 0 -2px 0 var(--needle)}
 .navtab.on .ico{color:var(--accent)}
 .navacts,.pageacts{display:flex;gap:6px;align-items:center;flex:none}
 .navbtn{position:relative;display:inline-flex;align-items:center;gap:6px;height:34px;
   min-width:0;padding:0 12px;border-radius:var(--r-sm);font-size:12.5px;cursor:pointer;
   background:var(--field-bg);border:1px solid var(--line);color:var(--ink-2);white-space:nowrap;
   transition:background .15s,border-color .15s,color .15s,box-shadow .15s}
 .navbtn .ico{color:var(--dim)}
 .navbtn:hover{background:var(--head);border-color:var(--field);color:var(--ink)}
 .navbtn:focus-visible{outline:0;box-shadow:var(--ring);border-color:var(--accent)}
 .navbtn:disabled{opacity:.55;cursor:default}
 .navbtn.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent-2)}
 .navbtn.on .ico{color:var(--accent)}
 /* theme, help and shut down: icons, named by their tooltips */
 #helpBtn span,#quitBtn span{display:none}
 #helpBtn,#quitBtn,#themeBtn{width:34px;padding:0;justify-content:center}
 .navbtn.danger .ico{color:var(--bad-ico)}
 .navbtn.danger:hover{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad-ink)}
 /* the theme switch shows the CHOICE (screen = following Windows), and
    opens a three-item menu */
 .themewrap{position:relative;display:inline-flex}
 #themeBtn .ico{display:none}
 :root:not([data-theme-pref=light]):not([data-theme-pref=dark]) #themeBtn .ti-auto,
 [data-theme-pref=light] #themeBtn .ti-light,[data-theme-pref=dark] #themeBtn .ti-dark{display:inline}
 .thememenu{position:absolute;top:calc(100% + 6px);right:0;z-index:80;min-width:178px;padding:4px;
   background:var(--card);border:1px solid var(--line);border-radius:var(--r-sm);box-shadow:var(--shadow-lg)}
 .thememenu[hidden]{display:none}
 .thememenu button{display:flex;width:100%;justify-content:flex-start;gap:9px;height:32px;min-width:0;
   padding:0 10px;border:0;border-radius:7px;background:none;font-size:13px;color:var(--ink-2)}
 .thememenu button:hover:not(:disabled){background:var(--line-2);border:0;color:var(--ink)}
 .thememenu button .ico{color:var(--dim)}
 .thememenu button.on{color:var(--accent-2);font-weight:600}
 .thememenu button.on .ico{color:var(--accent)}
 .thememenu button.on::after{content:"✓";margin-left:auto}
 .thememenu .tmh{font-size:11px;font-weight:600;color:var(--faint);text-transform:uppercase;
   letter-spacing:.06em;padding:8px 10px 3px;border-top:1px solid var(--line-2);margin-top:4px}
 .thememenu button.pal{height:auto;min-height:36px;padding:5px 10px;align-items:center}
 .thememenu .pal small{display:block;font-size:11px;font-weight:400;color:var(--dim);line-height:1.2}
 .thememenu .pal span{text-align:left;line-height:1.25}
 .thememenu .sw{width:20px;height:20px;border-radius:6px;flex:none;border:1px solid var(--line)}
 .navsep{width:1px;height:22px;background:var(--line);margin:0 2px}
 .navbtn.busy{opacity:.6;cursor:progress}
 /* workbook build in progress: the button fills as it goes */
 .navbtn.building,.navbtn.building:disabled{opacity:1;cursor:progress;
   border-color:var(--accent-line);color:var(--accent-2);
   background:linear-gradient(90deg,var(--accent-soft) var(--p,0%),var(--field-bg) var(--p,0%))}
 .navbtn.built{border-color:var(--good-line);color:var(--good)}
 a.navbtn{text-decoration:none}
 .navbtn[hidden]{display:none}
 /* possible duplicates: amber, because it asks for a decision */
 .navbtn.dup{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)}
 .navbtn.dup .ico{color:var(--warn-ico)}
 .navbtn.dup:hover{background:var(--warn-hover);border-color:var(--warn-hover-line);color:var(--warn-ink)}
 /* The workbook is a file, so it goes stale. That is said ON the button that
    fixes it, as a dot that takes no space. */
 .navbtn.stale::after{content:"";position:absolute;top:-3px;right:-3px;width:9px;
   height:9px;border-radius:50%;background:#e39a2b;border:2px solid var(--card)}
 @media (max-width:760px){.wrap{padding:10px 12px 40px}:is(.card,.findbar){padding:16px}
   .topbar{margin-bottom:16px}.pagetitle{font-size:21px}}
 @media (max-width:1120px){.navtab span,.navbtn span,.brand .bt{display:none}
   .navtab,.navbtn{padding:0 10px}}

 /* ---- page header: title + purpose left, this page's own actions right ---- */
 .pagehead{display:flex;align-items:flex-end;gap:16px;margin:0 2px 16px}
 .pagehead>div:first-child{flex:1;min-width:0}
 .pagetitle{font-family:var(--ui-d);font-size:24px;line-height:1.2;margin:0;font-weight:650;
   letter-spacing:-.02em}
 .sub{color:var(--dim);font-size:13px;margin-top:4px}
 .dbstat{display:none}

 /* ---- mode switch (Add data): one tab, three ways of working ---- */
 .modes{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 16px}
 .mode{display:flex;gap:11px;align-items:center;padding:11px 14px;border-radius:var(--r);
   border:1px solid var(--line);background:var(--card);text-decoration:none;color:var(--ink-2);
   box-shadow:var(--shadow);transition:border-color .15s,background .15s,box-shadow .15s}
 .mode .ico{color:var(--faint);width:18px;height:18px}
 .mode:hover{border-color:var(--accent-line);box-shadow:var(--shadow-lg)}
 .mode-t{display:flex;flex-direction:column;min-width:0}
 .mode-t b{font-size:13.5px;font-weight:600;color:var(--ink)}
 .mode-t span{font-size:12px;color:var(--dim);line-height:1.35;margin-top:1px}
 .mode.on{border-color:var(--accent);background:var(--accent-soft);
   box-shadow:inset 0 0 0 1px var(--accent)}
 .mode.on .ico,.mode.on b{color:var(--accent-2)}
 @media (max-width:760px){.mode-t span{display:none}}

 /* ---- cards ---- */
 @keyframes vpa-rise{from{opacity:0;transform:translateY(5px)}}
 .card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
   padding:18px 20px;margin-bottom:16px;box-shadow:var(--shadow);transition:opacity .2s;
   animation:vpa-rise .3s ease-out backwards}
 .ptitle{font-size:14px;color:var(--ink);font-weight:600;margin-bottom:12px;
   letter-spacing:-.005em}
 .ptitle::first-letter,label::first-letter{text-transform:uppercase}
 .ptitle>span:not([class]),.ptitle span[id]{font-weight:400;color:var(--dim);font-size:12.5px}
 .cardhead,.hdr2{display:flex;align-items:center;gap:10px;margin-bottom:12px}
 .cardhead .ptitle,.hdr2 .ptitle{flex:1;margin:0}
 .mini-h{font-size:12px;color:var(--ink-2);font-weight:600;margin-bottom:6px}
 .act,.cols,.grid2{display:flex;gap:32px;flex-wrap:wrap;align-items:flex-start}
 .cols>div{flex:1 1 300px;min-width:260px}
 .panel{flex:1 1 340px;min-width:300px}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}

 /* ---- explanations on demand (vpaNotes): a note collapses to a toggle ---- */
 button.notetog{display:flex;align-items:center;gap:6px;height:auto;min-width:0;padding:0;
   margin-top:10px;border:0;background:none;color:var(--dim);font-size:12px;cursor:pointer}
 button.notetog:hover:not(:disabled),button.notetog.on{background:none;color:var(--accent-2)}
 button.notetog .i{display:inline-grid;place-items:center;width:16px;height:16px;
   border-radius:50%;border:1px solid currentColor;font:italic 600 10px/1 Georgia,serif}
 .hint.note.tucked{display:none}
 .hint.note{background:var(--head);border-radius:var(--r-sm);padding:9px 12px;
   border:1px solid var(--line-2);margin-top:6px}

 /* ---- form controls ---- */
 label{font-size:12px;color:var(--dim);display:block;margin-bottom:5px}
 :where(input:not([type=checkbox]):not([type=radio]):not([type=file]),select,textarea){
   height:36px;padding:0 10px;border:1px solid var(--field);border-radius:var(--r-sm);
   font-size:13px;background:var(--field-bg);color:var(--ink);
   transition:border-color .15s,box-shadow .15s}
 :where(textarea){height:auto;padding:9px 11px;line-height:1.5}
 :where(input,select,textarea):hover:not(:disabled):not(:focus){border-color:var(--hover-line)}
 :where(input,select,textarea):focus{outline:0;box-shadow:var(--ring);border-color:var(--accent)}
 :where(input,select,textarea):disabled{background:var(--head);color:var(--dim)}
 ::placeholder{color:var(--faint)}
 input[type=checkbox]{accent-color:var(--accent)}

 /* ---- buttons: one family. Neutral is outlined; colour means something ---- */
 :where(button){display:inline-flex;align-items:center;justify-content:center;gap:6px;
   height:36px;min-width:92px;padding:0 15px;border:1px solid var(--field);
   border-radius:var(--r-sm);background:var(--field-bg);color:var(--ink-2);font-size:13px;
   cursor:pointer;white-space:nowrap;
   transition:background .15s,border-color .15s,color .15s,box-shadow .15s,transform .05s}
 :where(button):hover:not(:disabled){background:var(--head);border-color:var(--hover-line);color:var(--ink)}
 :where(button):active:not(:disabled){transform:translateY(1px)}
 :where(button):focus-visible{outline:0;box-shadow:var(--ring);border-color:var(--accent)}
 :where(button):disabled{opacity:.5;cursor:default}
 button.primary{background:var(--fill);border-color:var(--fill);color:var(--on-fill);
   box-shadow:0 1px 2px rgba(34,78,165,.3),inset 0 1px 0 rgba(255,255,255,.12)}
 button.primary:hover:not(:disabled){background:var(--fill-2);border-color:var(--fill-2);color:var(--on-fill)}
 button.commit{background:var(--fill-good);border-color:var(--fill-good);color:var(--on-fill);
   box-shadow:0 1px 2px rgba(35,121,58,.3),inset 0 1px 0 rgba(255,255,255,.12)}
 button.commit:hover:not(:disabled){background:var(--fill-good-2);border-color:var(--fill-good-2);color:var(--on-fill)}
 button.primary:disabled,button.commit:disabled{background:var(--fill-off);border-color:var(--fill-off);
   box-shadow:none;opacity:1}
 button.danger{color:var(--bad-ink-2);border-color:var(--bad-line);background:var(--field-bg)}
 button.danger:hover:not(:disabled){background:var(--bad-soft);color:var(--bad-ink);border-color:var(--bad-line-2)}
 button.mini,a.mini{height:30px;min-width:0;padding:0 11px;font-size:12.5px}
 a.mini{display:inline-flex;align-items:center;text-decoration:none;color:var(--ink-2);
   border:1px solid var(--field);border-radius:var(--r-sm);background:var(--field-bg);transition:background .15s}
 a.mini:hover{background:var(--head)}
 button.x{min-width:0;width:22px;height:22px;padding:0;border:0;border-radius:50%;
   background:var(--line-2);color:var(--dim);font-size:14px;line-height:1}
 button.x:hover:not(:disabled){background:var(--bad-soft);color:var(--bad)}
 /* segmented toggle */
 .seg{display:inline-flex;padding:2px;gap:2px;background:var(--line-2);
   border-radius:var(--r-sm);border:1px solid var(--line)}
 .seg button{height:28px;min-width:0;padding:0 12px;font-size:12px;border:0;border-radius:7px;
   background:transparent;color:var(--ink-2)}
 .seg button:hover:not(:disabled){background:var(--seg-hover);border:0}
 .seg button.on,.seg button.on:hover{background:var(--seg-on);color:var(--accent-2);font-weight:600;
   box-shadow:0 1px 2px rgba(16,24,40,.12)}
 /* in-card tabs */
 .tabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:-4px 0 16px}
 button.tab{height:36px;min-width:0;padding:0 14px;background:transparent;border:0;
   border-bottom:2px solid transparent;border-radius:var(--r-sm) var(--r-sm) 0 0;
   margin-bottom:-1px;font-size:13px;color:var(--dim)}
 button.tab:hover:not(:disabled){background:transparent;color:var(--ink);border-color:transparent}
 button.tab.on,button.tab.on:hover{color:var(--accent-2);font-weight:600;
   border-bottom:2px solid var(--accent);background:transparent}
 /* pickable chips */
 .chip{display:inline-flex;align-items:baseline;gap:6px;border:1px solid var(--line);
   background:var(--field-bg);border-radius:999px;padding:4px 12px;margin:0 6px 7px 0;font-size:12.5px;
   cursor:pointer;color:var(--ink);transition:background .15s,border-color .15s}
 .chip .flat{font-size:11.5px}
 .chip:hover{background:var(--accent-soft);border-color:var(--accent-line)}
 .chip.on{background:var(--fill);border-color:var(--fill);color:var(--on-fill)}
 .chip.on .flat{color:var(--on-fill-dim)}
 .chip.more{border-style:dashed;color:var(--accent);background:transparent}

 /* ---- tables: quiet headers, air between rows ---- */
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th{background:var(--card);color:var(--dim);text-align:left;font-weight:600;font-size:11px;
   text-transform:uppercase;letter-spacing:.04em;padding:8px 10px;white-space:nowrap;
   border-bottom:1px solid var(--line)}
 th.s{cursor:pointer}th.s:hover{color:var(--ink)}
 td{border-top:1px solid var(--line-2);padding:7px 10px;vertical-align:middle}
 tbody tr{transition:background .1s}
 tbody tr:hover td{background:var(--row-hover)}
 .num,th.num{text-align:right}
 .scroll{max-height:56vh;overflow:auto;border:1px solid var(--line);border-radius:var(--r-sm);
   scrollbar-width:thin}
 .scroll th{position:sticky;top:0;z-index:2}

 /* ---- figures ---- */
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:10px}
 .tile{background:linear-gradient(180deg,var(--tile-a),var(--tile-b));border:1px solid var(--line);
   border-radius:12px;padding:12px 14px;min-width:0}
 .tile b{display:block;font-family:var(--ui-d);font-size:22px;font-weight:650;line-height:1.2;
   letter-spacing:-.02em;color:var(--digits);text-shadow:var(--glow-text)}
 .tile b.rng{font-size:15px;line-height:1.5;white-space:nowrap}
 .tile span{display:block;font-size:12px;color:var(--dim);margin-top:2px}
 .tile.warn b{color:var(--warn)}
 .ax{font-size:10.5px;fill:#8a92a0;font-family:var(--ui)}
 .lgd{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--dim);margin-top:8px}
 .lgd span{display:inline-flex;align-items:center;gap:5px}
 .lgd i{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}
 .swatch{width:10px;height:10px;border-radius:3px;display:inline-block;flex:none}

 /* ---- key/value lists ---- */
 .lst,.kv{font-size:12.5px}
 .lst>div,.kv>div{display:flex;gap:10px;align-items:baseline;padding:6px 0;
   border-top:1px solid var(--line-2)}
 .lst>div:first-child,.kv>div:first-child{border-top:0}
 .lst .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .kv .k{flex:1;color:var(--ink-2)}

 /* ---- meaning: these are the only places colour carries information ---- */
 .up,.ok{color:var(--good)} .up{font-weight:600}
 .down,.warn{color:var(--bad)} .down{font-weight:600}
 .flat,.muted,.na{color:var(--dim)}
 .tag,.flag{display:inline-block;background:var(--line-2);color:var(--ink-2);border-radius:999px;
   padding:0 8px;font-size:11px;line-height:19px;white-space:nowrap;vertical-align:1px}
 .tag{margin-left:4px}
 .tag.warn,.flag{background:var(--bad-soft);color:var(--bad)}
 .tag.good{background:var(--good-soft);color:var(--good)}
 .flag.info{background:var(--accent-soft);color:var(--accent-2)}
 .hint{font-size:12px;color:var(--dim);margin-top:12px;line-height:1.55}
 .empty{text-align:center;color:var(--dim);padding:26px 10px;font-size:13px}
 .warnbox,#stale{background:var(--warn-soft);border:1px solid var(--warn-line);color:var(--warn);
   border-radius:var(--r-sm);padding:10px 13px;font-size:12.5px;margin-top:10px}
 #stale{display:none;margin:0 0 14px}
 .okbox{background:var(--good-soft);border:1px solid var(--good-line);color:var(--good-ink);
   border-radius:var(--r-sm);padding:10px 13px;font-size:12.5px}
 a.ext{color:var(--accent);text-decoration:none}a.ext:hover{text-decoration:underline}
 a.ext.muted{color:var(--dim)}
 .adlinks{white-space:nowrap}

 /* ---- listing photos (vpa/photos.py): a thumbnail, and a larger copy on
    hover. A car with no stored photo gets a quiet blank of the same size,
    so rows still line up. ---- */
 .vpa-th{display:inline-block;width:64px;height:43px;border-radius:6px;object-fit:cover;
   background:var(--line-2);vertical-align:middle;flex:none}
 .vpa-th.none,.vpa-th.gone{background:var(--line-2) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23b6bcc8' stroke-width='1.6'%3E%3Cpath d='M5 15l1.5-4.5A2 2 0 0 1 8.4 9h7.2a2 2 0 0 1 1.9 1.5L19 15M4 15h16v3H4zM7 18v1.5M17 18v1.5'/%3E%3C/svg%3E") center/22px no-repeat}
 img.vpa-th[data-ph]{cursor:zoom-in}
 img.vpa-th.gone{font-size:0;color:transparent}
 #vpaphpop{position:fixed;z-index:90;pointer-events:none;width:360px;height:240px;border-radius:10px;
   background:var(--line-2);box-shadow:var(--shadow-lg);border:3px solid var(--card);display:none;object-fit:cover}
 .vpa-gal{display:flex;flex-direction:column;gap:6px;width:300px;flex:none}
 .vpa-gal .main{display:block;width:300px;height:200px;border-radius:10px;object-fit:cover;
   background:var(--line-2)}
 .vpa-gal .strip{display:flex;gap:5px;flex-wrap:wrap}
 .vpa-gal .strip img{width:56px;height:37px;border-radius:5px;object-fit:cover;cursor:pointer;
   opacity:.75;border:2px solid transparent;background:var(--line-2)}
 .vpa-gal .strip img.on,.vpa-gal .strip img:hover{opacity:1;border-color:var(--accent)}
 .vpa-gal .cap{font-size:11px;color:var(--faint)}
 .vpa-hero{display:flex;gap:18px;align-items:flex-start}
 .vpa-hero>.vpa-txt{flex:1;min-width:0}
 @media (max-width:760px){.vpa-hero{flex-direction:column}.vpa-gal,.vpa-gal .main{width:100%}
   .vpa-gal .main{height:auto;aspect-ratio:3/2}}
 code{background:var(--line-2);border-radius:5px;padding:1px 5px;font-size:12px}
 kbd{background:var(--field-bg);border:1px solid var(--field);border-bottom-width:2px;border-radius:5px;
   padding:0 5px;font-size:11px;font-family:inherit}

 /* ---- working: a thin bar across the top, and the results it will replace
    faded, so a click visibly did something and stale numbers are never
    mistaken for the new ones. Shown only after 150 ms - a fast answer
    should not flicker. ---- */
 #vpaload{position:fixed;top:0;left:0;right:0;height:3px;z-index:100;overflow:hidden;
   opacity:0;transition:opacity .12s;pointer-events:none}
 #vpaload i{position:absolute;top:0;bottom:0;left:-35%;width:35%;
   background:linear-gradient(90deg,transparent,var(--accent),transparent);
   animation:vpaslide 1s ease-in-out infinite}
 body.vpa-busy #vpaload{opacity:1}
 body.vpa-busy{cursor:progress}
 body.vpa-busy .card:not(.keep){opacity:.55;transition:opacity .12s}
 @keyframes vpaslide{0%{left:-35%}100%{left:100%}}
 @media (prefers-reduced-motion:reduce){#vpaload i{animation:none;left:0;width:100%;opacity:.6}
   .card,.findbar{animation:none}*{transition:none!important}}

 /* ---- help panel, identical on every page ---- */
 #help{border-color:var(--accent-line);background:linear-gradient(180deg,var(--help-a),var(--card) 120px)}
 #help>p,#help li,#help details p{font-size:13px;color:var(--ink-2)}
 #help ul{padding-left:20px;margin:8px 0}
 #help li{margin:4px 0}
 #help details{margin:8px 0;border:1px solid var(--line);border-radius:var(--r-sm);
   padding:8px 13px;background:var(--card)}
 #help summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--ink)}
"""


#: Behaviour that belongs to the chrome, not to any one page: help, shut
#: down, the Excel viewer button, and the database line in the page header.
#: The functions are declared at the top level of the page's script, so page
#: code can call vpaStatus() / vpaApplyStatus() / vpaTicks() / vpaAxMoney().
CHROME_JS = r"""
/* the database line and the workbook's freshness - on every page the same */
function vpaApplyStatus(s){
  if(!s)return s;
  var d=document.getElementById("navstat");
  if(d&&s.vehicles!=null){
    d.textContent=Number(s.vehicles).toLocaleString()+" cars · "
      +Number(s.observations).toLocaleString()+" prices";
    d.title=Number(s.vehicles).toLocaleString()+" vehicles, "
      +Number(s.observations).toLocaleString()+" price observations\ndatabase: "+s.db;
  }
  // possible duplicates: shown only when there is something to review, so
  // an import that re-captures a stored car announces it on every page
  var db=document.getElementById("dupBtn");
  if(db&&s.dupes){
    var n=(s.dupes.proven||0)+(s.dupes.review||0);
    db.hidden=!n;
    document.getElementById("dupTxt").textContent=n+" duplicate"+(n===1?"":"s");
    db.title=(s.dupes.proven?s.dupes.proven+" proven (safe to merge), ":"")
      +(s.dupes.review||0)+" for your judgement - the same car stored as two identities. Click to review.";
  }
  var xb=document.getElementById("xlsxBtn");
  if(xb&&s.workbook){
    var st=!!s.workbook.stale;
    xb.classList.toggle("stale",st);
    if(!_vpaXl.polling)xb.title=st?"The Excel workbook does not show your current data (built "
      +(s.workbook.built||"never")+"). Click to rebuild it and open it in Excel."
      :"viewer.xlsx is up to date (built "+s.workbook.built+"). Click to rebuild and open it in Excel.";
    // a build started from another tab shows its progress here too
    var j=s.workbook.job;
    if(j&&j.running&&!_vpaXl.polling)vpaXlPoll(false);
  }
  return s;
}
/* Excel workbook build: a background job on the server. Every workbook
   button on the page (the header's, and Overview's Rebuild) shows the one
   job's progress; only the tab that asked reports the outcome. */
var _vpaXl={polling:false,mine:false};
function _vpaXlButtons(){
  return ["xlsxBtn","rebuildBtn"].map(function(id){return document.getElementById(id);})
    .filter(Boolean);
}
function _vpaXlShow(j){
  _vpaXlButtons().forEach(function(b){
    var sp=b.querySelector("span");
    if(!b.dataset.label&&sp)b.dataset.label=sp.textContent;
    if(j&&j.running){
      var pc=Math.round((j.frac||0)*100);
      b.disabled=true;b.classList.add("building");b.classList.remove("built");
      b.style.setProperty("--p",pc+"%");
      if(sp)sp.textContent="Building "+pc+"%";
      b.title=(j.text||"Building")+" \u00b7 "+(j.elapsed||0)+"s so far";
    }else{
      b.disabled=false;b.classList.remove("building");b.style.removeProperty("--p");
      if(sp)sp.textContent=b.dataset.label||sp.textContent;
    }
  });
}
function _vpaXlDone(j){
  _vpaXlShow(null);
  if(!_vpaXl.mine||!j)return;
  _vpaXl.mine=false;
  if(!j.ok){alert(j.error||"the workbook build failed");return;}
  // say it worked: a rebuild that opens nothing is otherwise invisible
  _vpaXlButtons().forEach(function(b){
    var sp=b.querySelector("span");
    b.classList.add("built");
    if(sp)sp.textContent=j.opened?"Opening Excel\u2026":"Rebuilt \u2713 "+Math.round(j.elapsed)+"s";
    setTimeout(function(){b.classList.remove("built");
      if(sp&&!b.classList.contains("building"))sp.textContent=b.dataset.label||sp.textContent;},2600);
  });
}
function vpaXlPoll(mine){
  if(mine)_vpaXl.mine=true;
  if(_vpaXl.polling)return;
  _vpaXl.polling=true;
  (function tick(){
    fetch("/api/viewer/progress").then(function(r){return r.json();}).then(function(res){
      var j=res.job;
      if(j&&j.running){_vpaXlShow(j);setTimeout(tick,350);return;}
      _vpaXl.polling=false;_vpaXlDone(j);vpaStatus();
    }).catch(function(){setTimeout(tick,1000);});
  })();
}
function vpaWorkbook(open){
  _vpaXlShow({running:true,frac:0,text:"Starting",elapsed:0});
  fetch(open?"/api/viewer/open":"/api/viewer/build",{method:"POST",body:"{}"})
    .then(function(r){return r.json();})
    .then(function(res){
      if(!res.ok||!res.job){
        _vpaXlShow(null);
        alert(res.stale||String(res.error).indexOf("unknown endpoint")>=0
          ?"The app is running an older version - click Shut down, then reopen VPA."
          :(res.error||"could not start the workbook build"));
        return;
      }
      vpaXlPoll(true);
    }).catch(function(e){_vpaXlShow(null);alert("could not update the workbook: "+e);});
}
function vpaStatus(){
  return fetch("/api/status").then(function(r){return r.json();})
    .then(vpaApplyStatus).catch(function(){return null;});
}
/* Busy state, counted so overlapping requests do not clear each other's bar. */
var _vpaBusyN=0,_vpaBusyT=null;
function vpaBusy(on){
  _vpaBusyN=Math.max(0,_vpaBusyN+(on?1:-1));
  if(_vpaBusyN&&!_vpaBusyT){
    _vpaBusyT=setTimeout(function(){document.body.classList.add("vpa-busy");},150);
  }else if(!_vpaBusyN){
    clearTimeout(_vpaBusyT);_vpaBusyT=null;document.body.classList.remove("vpa-busy");
  }
}
/* Fetch JSON for one channel, latest request wins. The server answers in
   parallel, so a quick second click could otherwise be overwritten by the
   first click's slower answer - the page showing trim A under trim B's name.
   A newer call aborts the older one; a superseded call resolves to null and
   the caller renders nothing. */
var _vpaChan={};
function vpaLatest(channel,url){
  if(_vpaChan[channel])_vpaChan[channel].abort();
  var ctl=new AbortController();_vpaChan[channel]=ctl;
  vpaBusy(true);
  return fetch(url,{signal:ctl.signal}).then(function(r){return r.json();})
    .then(function(j){return _vpaChan[channel]===ctl?j:null;})
    .catch(function(e){if(e&&e.name==="AbortError")return null;throw e;})
    .finally(function(){vpaBusy(false);if(_vpaChan[channel]===ctl)delete _vpaChan[channel];});
}
/* The two links from a car to its advert: the site itself, and the Internet
   Archive's copies of the same address. The server sends the full address
   (vpa/links.py); a car with none gets no links. `gone` marks a car no
   longer listed - a site usually takes a sold car's page down, so the
   archive link is the one to try first there. VPA_SITE comes from your
   private site file (vpa/sites.py): the site a search is limited to, and the
   photo server. */
var VPA_SITE=__VPA_SITE__;
function vpaAdLinks(url,gone,car){
  var e=function(s){return String(s).replace(/[&<>"']/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});};
  if(!url){
    // No advert address was ever recorded for this car - it came from pasted
    // text, a screenshot or a typed entry, which carry no ad id. Rather than
    // leave a blank that looks like a fault, offer a web search for it,
    // labelled as a search: it may find the ad, a different car, or nothing.
    if(!car)return "";
    var name=car.title||[car.year,car.make,car.model,car.variant].filter(Boolean).join(" ");
    if(!name)return "";
    var q=(VPA_SITE.search?'site:'+VPA_SITE.search+' ':'')+'"'+name+'"'
      +(car.odometer_km?' "'+Math.round(car.odometer_km).toLocaleString("en-AU")+' km"':"");
    return '<span class="adlinks"><a class="ext muted" href="https://www.google.com/search?q='
      +encodeURIComponent(q)+'" target="_blank" rel="noreferrer noopener" title="no advert '
      +'was recorded for this car (it came from pasted text, a screenshot or a typed entry). '
      +'This is a web search for it - it may find the ad, a different car, or nothing.">search &#8599;</a></span>';
  }
  var site="ad";
  try{site=new URL(url).hostname.replace(/^www\./,"").split(".")[0];}catch(err){}
  var live='<a class="ext" href="'+e(url)+'" target="_blank" rel="noreferrer noopener" title="'
    +(gone?"the advert on "+e(site)+" - a sold car's page is usually taken down"
          :"the advert on "+e(site))+'">'+e(site)+' &#8599;</a>';
  var arch='<a class="ext" href="https://web.archive.org/web/*/'+e(url)
    +'" target="_blank" rel="noreferrer noopener" title="copies of this advert saved by the Internet Archive, if it saved any">archive &#8599;</a>';
  return '<span class="adlinks">'+(gone?arch+" "+live:live+" "+arch)+"</span>";
}
/* Listing photos. The server sends stored photo ADDRESSES only (vpa/
   photos.py, never the database); the browser loads one from the site's
   photo server when it scrolls into view - nothing is fetched ahead. The
   same shape check as the server, so nothing but that server is ever asked. */
var VPA_PH=/^[a-z]{3,20}\/[a-z]{2,20}\/[a-z]{2,20}\/[a-z0-9]{10,64}\.(jpg|jpeg|png|webp)$/;
function vpaPhotoUrl(p,w,h){
  var ph=VPA_SITE.photos||{};
  if(!p||!ph.server||!VPA_PH.test(p))return null;
  var q=String(ph.size||"").split("{w}").join(w).split("{h}").join(h);
  return "https://"+p.split("/")[0]+"."+ph.server+"/"+p+(q?"?"+q:"");
}
function vpaThumb(p){
  var u=vpaPhotoUrl(p,160,107);
  return u?'<img class="vpa-th" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="" src="'
    +u+'" data-ph="'+p+'" onerror="this.classList.add(\'gone\');this.removeAttribute(\'data-ph\')">'
    :'<span class="vpa-th none" title="no photo stored for this car"></span>';
}
/* A car's photos: one large, the rest as a strip to switch between. The
   large one opens the advert when there is one. */
function vpaGallery(ps,url){
  ps=(ps||[]).filter(function(p){return VPA_PH.test(p);});
  if(!ps.length)return "";
  var big='<img class="main" referrerpolicy="no-referrer" alt="" src="'+vpaPhotoUrl(ps[0],600,400)+'">';
  return '<div class="vpa-gal">'+(url?'<a href="'+String(url).replace(/"/g,"&quot;")
      +'" target="_blank" rel="noreferrer noopener" title="open the advert">'+big+'</a>':big)
    +(ps.length>1?'<div class="strip">'+ps.map(function(p,i){
      return '<img loading="lazy" referrerpolicy="no-referrer" alt="" class="'+(i?"":"on")
        +'" data-big="'+vpaPhotoUrl(p,600,400)+'" src="'+vpaPhotoUrl(p,160,107)+'">';}).join("")+'</div>':"")
    +'<div class="cap">photos from the advert, as it was captured</div></div>';
}
document.addEventListener("click",function(e){
  var t=e.target.closest&&e.target.closest(".vpa-gal .strip img");
  if(!t)return;
  var g=t.closest(".vpa-gal");
  g.querySelector(".main").src=t.dataset.big;
  g.querySelectorAll(".strip img").forEach(function(x){x.classList.toggle("on",x===t);});
});
(function(){
  var pop=null;
  function hide(){if(pop)pop.style.display="none";}
  document.addEventListener("mouseover",function(e){
    var t=e.target;
    if(!(t&&t.tagName==="IMG"&&t.classList.contains("vpa-th")&&t.dataset.ph))return;
    if(!pop){pop=document.createElement("img");pop.id="vpaphpop";pop.alt="";
      pop.referrerPolicy="no-referrer";document.body.appendChild(pop);}
    pop.src=vpaPhotoUrl(t.dataset.ph,480,320);
    var r=t.getBoundingClientRect(),x=r.right+12,y=r.top-90;
    if(x+366>innerWidth)x=r.left-372;
    pop.style.left=Math.max(6,x)+"px";
    pop.style.top=Math.max(6,Math.min(innerHeight-252,y))+"px";
    pop.style.display="block";
  });
  document.addEventListener("mouseout",function(e){
    if(e.target&&e.target.classList&&e.target.classList.contains("vpa-th"))hide();});
  window.addEventListener("scroll",hide,true);
})();
/* Bring a card into view with its START showing. scrollIntoView's
   "nearest" lines a card taller than the window up by its BOTTOM edge when
   it lies above the view, so selecting a car in the table below landed part
   way down its card. Here the card's top goes just under any sticky bar at
   the top of the page; a card already wholly in view is left where it is. */
function vpaReveal(el){
  if(!el)return;
  var r=el.getBoundingClientRect(),top=8;
  document.querySelectorAll("[data-sticky]").forEach(function(s){
    var b=s.getBoundingClientRect(),cs=getComputedStyle(s);
    var at=cs.position==="sticky"||cs.position==="fixed"?parseFloat(cs.top)||0:0;
    if(b.height&&b.top<=at+1)top=Math.max(top,b.bottom+8);});
  if(r.top>=top&&r.bottom<=window.innerHeight)return;
  window.scrollTo({top:Math.max(0,window.scrollY+r.top-top),behavior:"smooth"});
}
/* Explanations on demand. A paragraph marked .hint.note collapses, where it
   stands, to a small "(i) How to read this" toggle - a card leads with its
   numbers, and the reasoning is one click away. data-label names the toggle.
   Notes rendered later are wired as they appear (the observer below). */
function vpaNotes(root){
  (root||document).querySelectorAll(".hint.note:not([data-wired])").forEach(function(n){
    n.dataset.wired="1";
    var b=document.createElement("button");
    b.type="button";b.className="notetog";b.setAttribute("aria-expanded","false");
    b.innerHTML='<span class="i">i</span>'+(n.dataset.label||"How to read this");
    n.classList.add("tucked");
    b.onclick=function(e){e.stopPropagation();
      var open=n.classList.toggle("tucked")===false;
      b.classList.toggle("on",open);b.setAttribute("aria-expanded",String(open));};
    n.parentNode.insertBefore(b,n);
  });
}
/* Axis ticks on round numbers. The charts used to label 0/25/50/75/100% of
   the data range, which gave axes reading $30,794 / $53,627 / $76,460. */
function vpaTicks(lo,hi,n){
  n=n||5;
  if(!(hi>lo))return [lo];
  var raw=(hi-lo)/(n-1),mag=Math.pow(10,Math.floor(Math.log10(raw))),f=raw/mag;
  var step=(f<1.5?1:f<3?2:f<7?5:10)*mag,out=[];
  for(var v=Math.ceil(lo/step-1e-9)*step;v<=hi+step*1e-9;v+=step)out.push(+v.toPrecision(12));
  return out;
}
/* compact money for axes only - tooltips and tables keep the full figure */
function vpaAxMoney(v){
  var a=Math.abs(v),s=v<0?"-$":"$";
  if(a>=1e6)return s+(+(a/1e6).toFixed(2))+"m";
  if(a>=1e3)return s+(+(a/1e3).toFixed(a<1e4?1:0))+"k";
  return s+Math.round(a);
}
(function(){
  var help=document.getElementById("help"),hb=document.getElementById("helpBtn");
  if(hb&&help){
    if(!help.style.display)help.style.display="none";
    hb.onclick=function(){
      var open=help.style.display!=="none";
      help.style.display=open?"none":"block";
      hb.classList.toggle("on",!open);
      if(!open)vpaReveal(help);
    };
  }
  /* Excel viewer: one button, one place, every page. While it builds, the
     button fills left to right and says how far it is (the icon stays; the
     text span is what changes). */
  var xb=document.getElementById("xlsxBtn");
  if(xb)xb.onclick=function(){vpaWorkbook(true);};
  /* Light or dark: the choice is kept in this browser (it is how the app
     looks, not data), applied to every open VPA tab at once. */
  var tb=document.getElementById("themeBtn"),tm=document.getElementById("themeMenu");
  if(tb&&tm&&window.vpaTheme){
    var NAMES={auto:"matching Windows",light:"light",dark:"dark"};
    var mark=function(){var p=vpaTheme(),d=window.vpaPalette?vpaPalette():"",
        dn=tm.querySelector('[data-palette-set="'+d+'"] span');
      tb.title="Theme - "+NAMES[p]+(p==="auto"?" ("+vpaThemeNow()+")":"")
        +(dn?", "+dn.firstChild.textContent+" dash":"");
      tm.querySelectorAll("[data-theme-set]").forEach(function(b){
        var on=b.dataset.themeSet===p;b.classList.toggle("on",on);b.setAttribute("aria-checked",String(on));});
      tm.querySelectorAll("[data-palette-set]").forEach(function(b){
        var on=b.dataset.paletteSet===d;b.classList.toggle("on",on);b.setAttribute("aria-checked",String(on));});};
    var shut=function(){tm.hidden=true;tb.setAttribute("aria-expanded","false");tb.classList.remove("on");};
    tb.onclick=function(e){e.stopPropagation();
      if(!tm.hidden){shut();return;}
      mark();tm.hidden=false;tb.setAttribute("aria-expanded","true");tb.classList.add("on");};
    tm.onclick=function(e){var b=e.target.closest("[data-theme-set]"),
        q=e.target.closest("[data-palette-set]");
      if(q&&window.vpaPalette){vpaPalette(q.dataset.paletteSet);mark();shut();return;}
      if(!b)return;vpaTheme(b.dataset.themeSet);mark();shut();};
    document.addEventListener("click",function(e){if(!tm.hidden&&!e.target.closest(".themewrap"))shut();});
    document.addEventListener("keydown",function(e){if(e.key==="Escape"&&!tm.hidden){shut();tb.focus();}});
    window.addEventListener("vpa-theme",mark);
    mark();
  }
  var qb=document.getElementById("quitBtn");
  if(qb)qb.onclick=function(){
    // Confirm: it used to be one click, sitting in a row of identical grey
    // buttons next to Quick add.
    if(!confirm("Stop the app?\n\nYour data is already saved - nothing is "
                +"lost. Launch it again from the desktop shortcut."))return;
    qb.disabled=true;qb.classList.add("busy");
    fetch("/api/shutdown",{method:"POST",body:"{}"}).then(function(){
      document.body.innerHTML='<div style="max-width:520px;margin:16vh auto;'
        +'text-align:center;font:15px/1.6 system-ui,sans-serif;color:var(--ink-2)">'
        +'<div style="font-size:34px">&#9679;</div><h2 style="font-weight:600">'
        +'The app has stopped</h2><p>Your data is saved. Close this tab, and '
        +'launch again from the desktop shortcut when you need it.</p></div>';
    }).catch(function(){});
  };
  vpaNotes();
  var _nq=false;
  new MutationObserver(function(){
    if(_nq)return;_nq=true;
    requestAnimationFrame(function(){_nq=false;vpaNotes();});
  }).observe(document.body,{childList:true,subtree:true});
  vpaStatus();
  window.addEventListener("focus",vpaStatus);
  setInterval(vpaStatus,20000);
})();
/* Australian dates on screen. The database, the API, keys, exports and
   every comparison keep ISO dates (2026-09-19); only what is SHOWN is
   rewritten, as it appears: a day as 19/09/2026, a month as Sep 2026.
   Inputs, scripts and styles are never touched. */
var VPA_MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
var VPA_DAY_RE=/(^|[^\d\/-])((?:19|20)\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\d-])/g;
var VPA_MONTH_RE=/(^|[^\d\/-])((?:19|20)\d\d)-(0[1-9]|1[0-2])(?![\d\/-])/g;
function vpaAu(s){
  if(typeof s!=="string"||s.indexOf("-")<0)return s;
  return s.replace(VPA_DAY_RE,function(m,a,y,mo,d){return a+d+"/"+mo+"/"+y;})
          .replace(VPA_MONTH_RE,function(m,a,y,mo){return a+VPA_MON[+mo-1]+" "+y;});
}
var VPA_AU_SKIP={SCRIPT:1,STYLE:1,TEXTAREA:1,INPUT:1,NOSCRIPT:1};
function vpaAuNode(n){
  if(n.nodeType===3){
    var p=n.parentNode;
    if(p&&!VPA_AU_SKIP[p.nodeName]&&!p.isContentEditable){var t=vpaAu(n.nodeValue);if(t!==n.nodeValue)n.nodeValue=t;}
    return;
  }
  if(n.nodeType!==1||VPA_AU_SKIP[n.nodeName]||n.isContentEditable)return;
  ["title","aria-label","placeholder"].forEach(function(a){
    var v=n.getAttribute&&n.getAttribute(a);if(v){var t=vpaAu(v);if(t!==v)n.setAttribute(a,t);}});
  for(var c=n.firstChild;c;c=c.nextSibling)vpaAuNode(c);
}
(function(){
  if(!document.body)return;
  vpaAuNode(document.body);
  new MutationObserver(function(ms){ms.forEach(function(m){
    if(m.type==="characterData")vpaAuNode(m.target);
    else if(m.type==="attributes")vpaAuNode(m.target);
    else m.addedNodes.forEach(vpaAuNode);
  });}).observe(document.body,{childList:true,subtree:true,characterData:true,
                               attributes:true,attributeFilter:["title","aria-label","placeholder"]});
})();
"""


#: The app icon (assets/vpa.*, drawn by tools/make_launcher.py and served by
#: importui) - the same mark as the nav logo, so the tab, the taskbar and the
#: desktop shortcut all look like one app.
HEAD_LINKS = ('<link rel="icon" href="/icon.svg" type="image/svg+xml">'
              '<link rel="alternate icon" href="/favicon.ico" sizes="any">'
              '<meta name="theme-color" content="#2b5fc4">')

#: Light or dark, settled in <head> before anything is drawn, so a dark page
#: never flashes white first. The choice ("auto", "light", "dark") lives in
#: this browser's localStorage - how the app looks, not data - and "auto"
#: follows Windows, live. Every page and every open tab read the same key.
#: CSS sees only the result: data-theme="light"|"dark" on <html>.
THEME_HEAD = r"""<script>
(function(){
  var K="vpa-theme",d=document.documentElement,p="auto",
      m=window.matchMedia?matchMedia("(prefers-color-scheme: dark)"):null;
  try{p=localStorage.getItem(K)||"auto";}catch(e){}
  function ok(x){return x==="light"||x==="dark"?x:"auto";}
  function now(){return p==="auto"?(m&&m.matches?"dark":"light"):p;}
  function apply(){
    p=ok(p);d.setAttribute("data-theme",now());d.setAttribute("data-theme-pref",p);
    try{window.dispatchEvent(new Event("vpa-theme"));}catch(e){}
  }
  window.vpaTheme=function(np){
    if(np){p=ok(np);
      try{p==="auto"?localStorage.removeItem(K):localStorage.setItem(K,p);}catch(e){}
      apply();}
    return p;};
  window.vpaThemeNow=now;
  if(m){var f=function(){if(p==="auto")apply();};
    m.addEventListener?m.addEventListener("change",f):m.addListener&&m.addListener(f);}
  window.addEventListener("storage",function(e){if(e.key===K){p=e.newValue||"auto";apply();}});
  apply();
  /* the dash theme: a colour set for light and dark alike */
  var PK="vpa-palette",PS=__PALETTE_NAMES__,pal=PS[0];
  try{pal=localStorage.getItem(PK)||PS[0];}catch(e){}
  function pok(x){return PS.indexOf(x)>=0?x:PS[0];}
  function papply(){pal=pok(pal);d.setAttribute("data-palette",pal);
    try{window.dispatchEvent(new Event("vpa-theme"));}catch(e){}}
  window.vpaPalette=function(np){
    if(np){pal=pok(np);try{localStorage.setItem(PK,pal);}catch(e){}papply();}
    return pal;};
  window.addEventListener("storage",function(e){if(e.key===PK){pal=e.newValue||PS[0];papply();}});
  papply();
})();
</script>"""

#: Chart colours in dark. The charts are drawn in page script with literal
#: colours (fill="#c04040", style="background:#3060a0" on a legend swatch),
#: tuned for a white card. Rather than thread a theme through ~60 drawing
#: calls, each literal is mapped to a dark-card equivalent here, by CSS: a
#: stylesheet rule outranks an SVG presentation attribute, and !important
#: outranks an inline style. Light is untouched - these apply in dark only.
#: Same hue, lifted for a dark background; gridlines sink instead of glowing.
CHART_DARK = {
    # gridlines, axes, whiskers, neutral marks
    "#eef0f4": "#262c37", "#edeff3": "#262c37", "#e3e7ee": "#2c333f",
    "#d4d8e0": "#3d4452", "#c3c8d2": "#4a5262", "#b9c2d0": "#566072",
    "#b6bdc9": "#566072", "#a9b2c1": "#5d6779", "#8792a5": "#8f9aae",
    "#8a92a2": "#8f9aae",
    # series
    "#c04040": "#ec7070", "#a03030": "#f08a80", "#c56b6b": "#e07b7b",
    "#3060a0": "#6f9fe6", "#5b87c4": "#6f9fe6",
    "#2e7d32": "#5cc28c", "#88aa88": "#7fb27f", "#b7cbb0": "#4e6b52",
    "#a06010": "#e0a24a", "#7048a0": "#b394e8", "#0d7a80": "#4fc3c9",
    # mid-tones that already read on a dark card: listed so every literal
    # colour is accounted for (check_importui looks for unmapped ones)
    "#7d9bc1": "#7d9bc1", "#7d9e7d": "#7d9e7d",
}


def _chart_dark_css() -> str:
    out = []
    for lo, hi in CHART_DARK.items():
        out.append(f'[data-theme=dark] [fill="{lo}" i]{{fill:{hi}}}'
                   f'[data-theme=dark] [stroke="{lo}" i]{{stroke:{hi}}}'
                   f'[data-theme=dark] [style*="background:{lo}" i]{{background:{hi}!important}}'
                   f'[data-theme=dark] [style*="fill:{lo}" i]{{fill:{hi}!important}}'
                   f'[data-theme=dark] [style*="color:{lo}" i]{{color:{hi}!important}}')
    # a hollow point is "#fff" inside: it takes the card's colour
    out.append('[data-theme=dark] svg [fill="#fff" i]{fill:var(--card)}')
    return "\n ".join(out)


def chrome(page: str, active: str, actions: str = "", mode: str = "") -> str:
    """Substitute the shared CSS, nav, page actions, mode switch and
    behaviour into a page template. `actions` lands in the page header,
    beside the title; `mode` marks which Add data mode this page is."""
    page = page.replace('<meta charset="utf-8">',
                        '<meta charset="utf-8">\n' + HEAD_LINKS + "\n"
                        + THEME_HEAD.replace("__PALETTE_NAMES__", json.dumps(list(PALETTES))), 1)
    return (page.replace("__CHROME_CSS__", CHROME_CSS.replace("__CHART_DARK__", _chart_dark_css())
                                                     .replace("__PALETTES__", palette_css()))
                .replace("__NAV__", nav(active))
                .replace("__MODES__", modes(mode) if mode else "")
                .replace("__ACTIONS__",
                         f'<div class="pageacts">{actions}</div>' if actions else "")
                .replace("__CHROME_JS__", CHROME_JS.replace("__VPA_SITE__", _site_js())))


def _site_js() -> str:
    """The site facts the shared page script needs, from the private site
    file - safe to drop inside a <script>."""
    import json
    from . import sites
    s = json.dumps({"search": sites.search_site(), "photos": sites.photos()})
    return s.replace("<", "\\u003c")
