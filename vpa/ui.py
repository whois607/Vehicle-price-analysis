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
    # two overlapping cards - one car stored twice
    "dupes": "M8 8h12v12H8zM16 8V4H4v12h4",
    # circular arrow - rebuild in place, without opening anything
    "rebuild": "M20 12a8 8 0 1 1-2.6-5.9M20 4v4h-4",
}

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
    return f"""<div id="vpaload" aria-hidden="true"><i></i></div>
<header class="topbar">
 <div class="brand"><span class="dot"></span>vehicle prices</div>
 <nav class="navtabs">{tabs}</nav>
 <div class="navacts">
  <a class="navbtn dup" id="dupBtn" href="/duplicates" hidden title="the same car stored as two identities - review">{icon("dupes")}<span id="dupTxt">Duplicates</span></a>
  <button class="navbtn" id="xlsxBtn" title="rebuild viewer.xlsx from the database and open it in Excel">{icon("excel")}<span>Excel viewer</span></button>
  <button class="navbtn" id="helpBtn" title="what this page does">{icon("help")}<span>How to use</span></button>
  <button class="navbtn danger" id="quitBtn" title="stops the app - your data is already saved">{icon("power")}<span>Shut down</span></button>
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
   --ui:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
   --bg:#f3f4f7; --card:#fff;
   --ink:#1d2430; --ink-2:#465164; --dim:#6c7585; --faint:#9aa2b1;
   --line:#e1e5eb; --line-2:#eef1f5; --field:#cbd2dc; --head:#f5f7fa;
   --accent:#2f5f9e; --accent-2:#264f86; --accent-soft:#eaf1fb; --accent-line:#c3d5ee;
   --good:#2e7d32; --good-soft:#e9f4ea; --good-line:#c9e2cd;
   --bad:#b03a3a; --bad-soft:#fcefee; --bad-line:#eccaca;
   --warn:#8a5a12; --warn-soft:#fff7ea; --warn-line:#efd9b5;
   --mine:#e8a317;
   --r:10px; --r-sm:7px; --shadow:0 1px 2px rgba(20,30,60,.05);
 }
 *,*::before,*::after{box-sizing:border-box}
 body,button,select,input,textarea{font-family:var(--ui)}
 body{font-size:14px;line-height:1.45;margin:0;background:var(--bg);color:var(--ink);
   -webkit-font-smoothing:antialiased}
 .wrap{max-width:1240px;margin:0 auto;padding:16px 20px 48px}
 table,.tile b,.num,.v{font-variant-numeric:tabular-nums}

 /* ---- header ---- */
 .topbar{display:flex;align-items:center;gap:14px;
   background:var(--card);border:1px solid var(--line);border-radius:var(--r);
   padding:7px 10px 7px 14px;margin-bottom:18px;box-shadow:var(--shadow)}
 .brand{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:600;
   color:var(--ink);white-space:nowrap}
 .brand .dot{width:9px;height:9px;border-radius:50%;background:#c04040;flex:none}
 .ico{flex:none;vertical-align:-3px}
 .navtabs{display:flex;gap:2px;flex:1;min-width:0}
 .navtab{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 11px;
   border-radius:var(--r-sm);font-size:13px;text-decoration:none;color:var(--ink-2);
   border:1px solid transparent;white-space:nowrap}
 .navtab:hover{background:var(--line-2);color:var(--ink)}
 .navtab.on{background:var(--accent-soft);border-color:var(--accent-line);
   color:var(--accent-2);font-weight:600}
 .navtab.on .ico{color:var(--accent)}
 .navacts,.pageacts{display:flex;gap:6px;align-items:center;flex:none}
 .navbtn{position:relative;display:inline-flex;align-items:center;gap:6px;height:32px;
   min-width:0;padding:0 11px;border-radius:var(--r-sm);font-size:12.5px;cursor:pointer;
   background:#fff;border:1px solid var(--field);color:var(--ink-2);white-space:nowrap}
 .navbtn:hover{background:var(--head);border-color:#b9c2d2}
 .navbtn:disabled{opacity:.55;cursor:default}
 .navbtn.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent-2)}
 .navbtn.danger{color:#8d3b3b;border-color:var(--bad-line)}
 .navbtn.danger:hover{background:var(--bad-soft);color:#7a2f2f}
 .navbtn.busy{opacity:.6;cursor:progress}
 a.navbtn{text-decoration:none}
 .navbtn[hidden]{display:none}
 /* possible duplicates: amber, because it asks for a decision */
 .navbtn.dup{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)}
 .navbtn.dup:hover{background:#fdeed6;border-color:#e6c48f}
 /* The workbook is a file, so it goes stale. That is said ON the button that
    fixes it, as a dot that takes no space - the banner it replaces pushed
    every page down, and on Bulk would have moved every button. */
 .navbtn.stale::after{content:"";position:absolute;top:-4px;right:-4px;width:10px;
   height:10px;border-radius:50%;background:#e39a2b;border:2px solid #fff}
 @media (max-width:1060px){.navtab span,.navbtn span{display:none}
   .navtab,.navbtn{padding:0 9px}}

 /* ---- page header: title + purpose left, this page's own actions right ---- */
 .pagehead{display:flex;align-items:flex-end;gap:16px;margin:0 2px 14px}
 .pagehead>div:first-child{flex:1;min-width:0}
 .pagetitle{font-size:20px;line-height:1.25;margin:0;font-weight:600}
 .sub{color:var(--dim);font-size:12.5px;margin-top:3px}
 .dbstat{white-space:nowrap;cursor:help}
 .dbstat:not(:empty)::before{content:"\\00b7";margin:0 7px;color:var(--faint)}

 /* ---- mode switch (Add data): one tab, three ways of working ---- */
 .modes{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 14px}
 .mode{display:flex;gap:10px;align-items:flex-start;padding:10px 13px;border-radius:var(--r);
   border:1px solid var(--line);background:var(--card);text-decoration:none;color:var(--ink-2);
   box-shadow:var(--shadow)}
 .mode .ico{margin-top:2px;color:var(--dim)}
 .mode:hover{border-color:var(--accent-line);background:#fbfcff}
 .mode-t{display:flex;flex-direction:column;min-width:0}
 .mode-t b{font-size:13px;font-weight:600;color:var(--ink)}
 .mode-t span{font-size:11.5px;color:var(--dim);line-height:1.35;margin-top:1px}
 .mode.on{border-color:var(--accent);background:var(--accent-soft);
   box-shadow:inset 0 0 0 1px var(--accent)}
 .mode.on .ico,.mode.on b{color:var(--accent-2)}
 @media (max-width:760px){.mode-t span{display:none}}

 /* ---- cards ---- */
 .card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
   padding:16px 18px;margin-bottom:14px;box-shadow:var(--shadow);transition:opacity .2s}
 .ptitle{font-size:13px;color:var(--ink);font-weight:600;margin-bottom:10px}
 .ptitle::first-letter,label::first-letter,th::first-letter{text-transform:uppercase}
 .ptitle>span:not([class]),.ptitle span[id]{font-weight:400;color:var(--dim)}
 .cardhead,.hdr2{display:flex;align-items:center;gap:10px;margin-bottom:10px}
 .cardhead .ptitle,.hdr2 .ptitle{flex:1;margin:0}
 .mini-h{font-size:12px;color:var(--ink-2);font-weight:600;margin-bottom:6px}
 .act,.cols,.grid2{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start}
 .cols>div{flex:1 1 300px;min-width:260px}
 .panel{flex:1 1 340px;min-width:300px}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}

 /* ---- form controls ---- */
 label{font-size:11.5px;color:var(--dim);display:block;margin-bottom:4px}
 :where(input:not([type=checkbox]):not([type=radio]):not([type=file]),select,textarea){
   height:34px;padding:0 9px;border:1px solid var(--field);border-radius:var(--r-sm);
   font-size:13px;background:#fff;color:var(--ink)}
 :where(textarea){height:auto;padding:8px 10px;line-height:1.5}
 :where(input,select,textarea):focus{outline:2px solid var(--accent-line);outline-offset:0;
   border-color:var(--accent)}
 :where(input,select,textarea):disabled{background:var(--head);color:var(--dim)}
 ::placeholder{color:var(--faint)}
 input[type=checkbox]{accent-color:var(--accent)}

 /* ---- buttons: one family. Neutral is outlined; colour means something ---- */
 :where(button){display:inline-flex;align-items:center;justify-content:center;gap:6px;
   height:34px;min-width:92px;padding:0 14px;border:1px solid var(--field);
   border-radius:var(--r-sm);background:#fff;color:var(--ink-2);font-size:13px;
   cursor:pointer;white-space:nowrap}
 :where(button):hover:not(:disabled){background:var(--head);border-color:#b9c2d2;color:var(--ink)}
 :where(button):disabled{opacity:.5;cursor:default}
 button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
 button.primary:hover:not(:disabled){background:var(--accent-2);border-color:var(--accent-2);color:#fff}
 button.commit{background:var(--good);border-color:var(--good);color:#fff}
 button.commit:hover:not(:disabled){background:#276b2b;border-color:#276b2b;color:#fff}
 button.primary:disabled,button.commit:disabled{background:#aeb6c2;border-color:#aeb6c2;opacity:1}
 button.danger{color:#8d3b3b;border-color:var(--bad-line);background:#fff}
 button.danger:hover:not(:disabled){background:var(--bad-soft);color:#7a2f2f}
 button.mini,a.mini{height:30px;min-width:0;padding:0 11px;font-size:12.5px}
 a.mini{display:inline-flex;align-items:center;text-decoration:none;color:var(--ink-2);
   border:1px solid var(--field);border-radius:var(--r-sm);background:#fff}
 a.mini:hover{background:var(--head)}
 button.x{min-width:0;width:22px;height:22px;padding:0;border:0;border-radius:50%;
   background:var(--line-2);color:var(--dim);font-size:14px;line-height:1}
 button.x:hover:not(:disabled){background:var(--bad-soft);color:var(--bad)}
 /* segmented toggle */
 .seg{display:inline-flex;border:1px solid var(--field);border-radius:var(--r-sm);overflow:hidden}
 .seg button{height:28px;min-width:0;padding:0 12px;font-size:12px;border:0;border-radius:0;
   background:#fff}
 .seg button+button{border-left:1px solid var(--field)}
 .seg button.on,.seg button.on:hover{background:var(--accent);color:#fff}
 /* in-card tabs */
 .tabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:-4px 0 14px}
 button.tab{height:34px;min-width:0;padding:0 13px;background:transparent;border:0;
   border-bottom:2px solid transparent;border-radius:var(--r-sm) var(--r-sm) 0 0;
   margin-bottom:-1px;font-size:13px;color:var(--ink-2)}
 button.tab:hover:not(:disabled){background:var(--line-2);border-color:transparent}
 button.tab.on,button.tab.on:hover{color:var(--accent);font-weight:600;
   border-bottom:2px solid var(--accent);background:transparent}
 /* pickable chips */
 .chip{display:inline-flex;align-items:baseline;gap:6px;border:1px solid var(--field);
   background:#fff;border-radius:16px;padding:3px 11px;margin:0 5px 6px 0;font-size:12.5px;
   cursor:pointer;color:var(--ink)}
 .chip .flat{font-size:11.5px}
 .chip:hover{background:var(--accent-soft);border-color:var(--accent-line)}
 .chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
 .chip.on .flat{color:#dbe6f6}
 .chip.more{border-style:dashed;color:var(--accent);background:transparent}

 /* ---- tables ---- */
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th{background:var(--head);color:var(--ink-2);text-align:left;font-weight:600;font-size:12px;
   padding:6px 8px;white-space:nowrap;border-bottom:1px solid var(--line)}
 th.s{cursor:pointer}th.s:hover{background:var(--line-2)}
 td{border-top:1px solid var(--line-2);padding:5px 8px;vertical-align:middle}
 tbody tr:hover td{background:#f8faff}
 .num,th.num{text-align:right}
 .scroll{max-height:56vh;overflow:auto;border:1px solid var(--line);border-radius:var(--r-sm)}
 .scroll th{position:sticky;top:0;z-index:2}

 /* ---- figures ---- */
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:10px}
 .tile{background:#fafbfd;border:1px solid var(--line);border-radius:8px;padding:9px 12px;
   min-width:0}
 .tile b{display:block;font-size:18px;font-weight:600;line-height:1.25}
 .tile b.rng{font-size:15px;line-height:1.5;white-space:nowrap}
 .tile span{display:block;font-size:11.5px;color:var(--dim);margin-top:1px}
 .tile.warn b{color:var(--warn)}
 .ax{font-size:10.5px;fill:#858d9c;font-family:var(--ui)}
 .lgd{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--dim);margin-top:6px}
 .lgd span{display:inline-flex;align-items:center;gap:5px}
 .lgd i{display:inline-block;width:10px;height:10px;border-radius:2px;flex:none}
 .swatch{width:10px;height:10px;border-radius:3px;display:inline-block;flex:none}

 /* ---- key/value lists ---- */
 .lst,.kv{font-size:12.5px}
 .lst>div,.kv>div{display:flex;gap:10px;align-items:baseline;padding:5px 0;
   border-top:1px solid var(--line-2)}
 .lst>div:first-child,.kv>div:first-child{border-top:0}
 .lst .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .kv .k{flex:1;color:var(--ink-2)}

 /* ---- meaning: these are the only places colour carries information ---- */
 .up,.ok{color:var(--good)} .up{font-weight:600}
 .down,.warn{color:var(--bad)} .down{font-weight:600}
 .flat,.muted,.na{color:var(--dim)}
 .tag,.flag{display:inline-block;background:var(--line-2);color:var(--ink-2);border-radius:4px;
   padding:0 6px;font-size:11px;line-height:18px;white-space:nowrap;vertical-align:1px}
 .tag{margin-left:4px}
 .tag.warn,.flag{background:var(--bad-soft);color:var(--bad)}
 .tag.good{background:var(--good-soft);color:var(--good)}
 .flag.info{background:var(--accent-soft);color:var(--accent-2)}
 .hint{font-size:12px;color:var(--dim);margin-top:10px;line-height:1.5}
 .empty{text-align:center;color:var(--dim);padding:26px 10px;font-size:13px}
 .warnbox,#stale{background:var(--warn-soft);border:1px solid var(--warn-line);color:var(--warn);
   border-radius:var(--r-sm);padding:9px 12px;font-size:12.5px;margin-top:10px}
 #stale{display:none;margin:0 0 14px}
 .okbox{background:var(--good-soft);border:1px solid var(--good-line);color:#245224;
   border-radius:var(--r-sm);padding:9px 12px;font-size:12.5px}
 a.ext{color:var(--accent);text-decoration:none}a.ext:hover{text-decoration:underline}
 a.ext.muted{color:var(--dim)}
 .adlinks{white-space:nowrap}
 code{background:var(--line-2);border-radius:4px;padding:1px 5px;font-size:12px}
 kbd{background:var(--line-2);border:1px solid var(--field);border-radius:4px;padding:0 5px;
   font-size:11px;font-family:inherit}

 /* ---- working: a thin bar across the top, and the results it will replace
    faded, so a click visibly did something and stale numbers are never
    mistaken for the new ones. Shown only after 150 ms - a fast answer
    should not flicker. ---- */
 #vpaload{position:fixed;top:0;left:0;right:0;height:3px;z-index:100;overflow:hidden;
   opacity:0;transition:opacity .12s;pointer-events:none}
 #vpaload i{position:absolute;top:0;bottom:0;left:-35%;width:35%;background:var(--accent);
   border-radius:2px;animation:vpaslide 1s ease-in-out infinite}
 body.vpa-busy #vpaload{opacity:1}
 body.vpa-busy{cursor:progress}
 body.vpa-busy .card:not(.keep){opacity:.5;transition:opacity .12s}
 @keyframes vpaslide{0%{left:-35%}100%{left:100%}}
 @media (prefers-reduced-motion:reduce){#vpaload i{animation:none;left:0;width:100%;opacity:.6}}

 /* ---- help panel, identical on every page ---- */
 #help{border-color:var(--accent-line)}
 #help>p,#help li,#help details p{font-size:13px;color:var(--ink-2)}
 #help ul{padding-left:20px;margin:8px 0}
 #help li{margin:4px 0}
 #help details{margin:8px 0;border:1px solid var(--line);border-radius:var(--r-sm);
   padding:7px 12px;background:#fafbfd}
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
  var d=document.getElementById("dbstat");
  if(d&&s.vehicles!=null){
    d.textContent=Number(s.vehicles).toLocaleString()+" vehicles · "
      +Number(s.observations).toLocaleString()+" observations";
    d.title="database: "+s.db;
  }
  // possible duplicates: shown only when there is something to review, so
  // an import that re-captures a stored car announces it on every page
  var db=document.getElementById("dupBtn");
  if(db&&s.dupes){
    var n=(s.dupes.proven||0)+(s.dupes.review||0);
    db.hidden=!n;
    document.getElementById("dupTxt").textContent=n+" possible duplicate"+(n===1?"":"s");
    db.title=(s.dupes.proven?s.dupes.proven+" proven (safe to merge), ":"")
      +(s.dupes.review||0)+" for your judgement - the same car stored as two identities. Click to review.";
  }
  var xb=document.getElementById("xlsxBtn");
  if(xb&&s.workbook){
    var st=!!s.workbook.stale;
    xb.classList.toggle("stale",st);
    xb.title=st?"The Excel workbook is older than your latest import (built "
      +(s.workbook.built||"never")+"). Click to rebuild it and open it in Excel."
      :"rebuild viewer.xlsx from the database and open it in Excel";
  }
  return s;
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
   longer listed - carsales usually takes a sold car's page down, so the
   archive link is the one to try first there. */
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
    var q='site:carsales.com.au "'+name+'"'
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
/* Bring a card into view with its START showing. scrollIntoView's
   "nearest" lines a card taller than the window up by its BOTTOM edge when
   it lies above the view, so selecting a car in the table below landed part
   way down its card. Here the card's top goes just under any sticky bar at
   the top of the page; a card already wholly in view is left where it is. */
function vpaReveal(el){
  if(!el)return;
  var r=el.getBoundingClientRect(),top=8;
  document.querySelectorAll("[data-sticky]").forEach(function(s){
    var b=s.getBoundingClientRect();if(b.top<=1&&b.height)top=Math.max(top,b.bottom+8);});
  if(r.top>=top&&r.bottom<=window.innerHeight)return;
  window.scrollTo({top:Math.max(0,window.scrollY+r.top-top),behavior:"smooth"});
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
  /* Excel viewer: one button, one place, every page (it used to exist on
     three of five, each with its own copy of the handler, and two of those
     overwrote the button's icon with "updating..."). The label never
     changes - progress is the button's busy state. */
  var xb=document.getElementById("xlsxBtn");
  if(xb)xb.onclick=function(){
    xb.disabled=true;xb.classList.add("busy");
    fetch("/api/viewer/open",{method:"POST",body:"{}"}).then(function(r){return r.json();})
      .then(function(res){
        if(!res.ok)alert(String(res.error).indexOf("unknown endpoint")>=0
          ?"The app is running an older version - click Shut down, then reopen Vehicle Import."
          :res.error);
      }).catch(function(e){alert("could not update the workbook: "+e);})
      .then(function(){xb.disabled=false;xb.classList.remove("busy");vpaStatus();});
  };
  var qb=document.getElementById("quitBtn");
  if(qb)qb.onclick=function(){
    // Confirm: it used to be one click, sitting in a row of identical grey
    // buttons next to Quick add.
    if(!confirm("Stop the app?\n\nYour data is already saved - nothing is "
                +"lost. Launch it again from the desktop shortcut."))return;
    qb.disabled=true;qb.classList.add("busy");
    fetch("/api/shutdown",{method:"POST",body:"{}"}).then(function(){
      document.body.innerHTML='<div style="max-width:520px;margin:16vh auto;'
        +'text-align:center;font:15px/1.6 system-ui,sans-serif;color:#445">'
        +'<div style="font-size:34px">&#9679;</div><h2 style="font-weight:600">'
        +'The app has stopped</h2><p>Your data is saved. Close this tab, and '
        +'launch again from the desktop shortcut when you need it.</p></div>';
    }).catch(function(){});
  };
  vpaStatus();
  window.addEventListener("focus",vpaStatus);
  setInterval(vpaStatus,20000);
})();
"""


def chrome(page: str, active: str, actions: str = "", mode: str = "") -> str:
    """Substitute the shared CSS, nav, page actions, mode switch and
    behaviour into a page template. `actions` lands in the page header,
    beside the title; `mode` marks which Add data mode this page is."""
    return (page.replace("__CHROME_CSS__", CHROME_CSS)
                .replace("__NAV__", nav(active))
                .replace("__MODES__", modes(mode) if mode else "")
                .replace("__ACTIONS__",
                         f'<div class="pageacts">{actions}</div>' if actions else "")
                .replace("__CHROME_JS__", CHROME_JS))
