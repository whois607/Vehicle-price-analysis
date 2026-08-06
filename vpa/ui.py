"""Shared page chrome: the nav bar, its icons, and the CSS for both.

Every page used to hand-roll its own header, so "Explore" was a link on one
page and a button on another, the current page was never marked, and
`Shut down` looked exactly like `Quick add` - one navigates, the other kills
the app. This is the single place that is now defined.

Pages embed `__CHROME_CSS__` in their <style> and `__NAV__` in their body;
`chrome(page, active)` substitutes both. Icons are inline SVG paths - no
font, no CDN, no network, consistent with every chart in this project.
"""
from __future__ import annotations

#: 16x16 stroke icons on a 24-unit grid, drawn from one visual family:
#: 2-unit round strokes, no fills, so they sit evenly beside 13px text.
ICONS = {
    # tray with an arrow coming down into it - bringing listings in
    "import": "M12 3v10m0 0 4-4m-4 4-4-4M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3",
    # magnifier - looking one vehicle up
    "explore": "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4.2-4.2",
    # two columns side by side - comparing
    "compare": "M4 20V10m5 10V4m5 16v-7m5 7V7",
    # lightning - the fast entry path
    "quick": "M13 3 5 14h6l-1 7 8-11h-6l1-7z",
    # grid - the spreadsheet
    "excel": "M4 5h16v14H4zM4 10h16M4 15h16M10 5v14",
    # question mark in a circle - help
    "help": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM9.5 9.2a2.6 2.6 0 0 1 5 .9c0 1.8-2.5 2.2-2.5 3.9M12 17.2h.01",
    # tick in a circle - the data check
    "check": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8.2 12.2l2.6 2.6 5-5.4",
    # power symbol - stops the app
    "power": "M12 3v9M7.5 6.4a7.5 7.5 0 1 0 9 0",
    # circular arrow - rebuild in place, without opening anything
    "rebuild": "M20 12a8 8 0 1 1-2.6-5.9M20 4v4h-4",
}

#: (slug, href, label, icon) - the pages you can be ON. Order is the order
#: of the workflow: bring data in, look at one, compare many, jot prices.
PAGES = [
    ("import", "/", "Import", "import"),
    ("explore", "/explore", "Explore", "explore"),
    ("compare", "/compare", "Compare", "compare"),
    ("quick", "/quick", "Quick add", "quick"),
]


def icon(name: str, size: int = 16) -> str:
    """One inline SVG icon."""
    return (f'<svg class="ico" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'<path d="{ICONS[name]}"/></svg>')


def nav(active: str, actions: str = "") -> str:
    """The header: page tabs on the left, page-specific actions, then the
    utility cluster on the right. `active` marks the current page so you can
    always tell where you are - which no page previously showed."""
    tabs = "".join(
        f'<a class="navtab{" on" if slug == active else ""}" href="{href}"'
        f'{" aria-current=\"page\"" if slug == active else ""}>'
        f'{icon(ic)}<span>{label}</span></a>'
        for slug, href, label, ic in PAGES)
    return f"""<header class="topbar">
 <div class="brand"><span class="dot"></span>vehicle prices</div>
 <nav class="navtabs">{tabs}</nav>
 <div class="navacts">{actions}
  <button class="navbtn" id="helpBtn" title="what this page does">{icon("help")}<span>How to use</span></button>
  <button class="navbtn danger" id="quitBtn" title="stops the app - your data is already saved">{icon("power")}<span>Shut down</span></button>
 </div>
</header>"""


def action(btn_id: str, label: str, ic: str, title: str = "") -> str:
    """A page-specific action button for nav()'s `actions` slot."""
    return (f'<button class="navbtn" id="{btn_id}" title="{title}">'
            f'{icon(ic)}<span>{label}</span></button>')


CHROME_CSS = """
 .topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
   background:#fff;border:1px solid var(--line);border-radius:10px;
   padding:7px 12px;margin-bottom:14px;box-shadow:0 1px 2px rgba(20,30,60,.05)}
 .brand{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:600;
   color:#334;letter-spacing:.01em;white-space:nowrap}
 .brand .dot{width:9px;height:9px;border-radius:50%;background:#c04040;flex:none}
 .ico{flex:none;vertical-align:-3px}
 /* page tabs: one visual family, and the current page is unmistakable */
 .navtabs{display:flex;gap:2px;flex-wrap:wrap;flex:1}
 .navtab{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 11px;
   border-radius:7px;font-size:13px;text-decoration:none;color:#4a5566;
   border:1px solid transparent;white-space:nowrap}
 .navtab:hover{background:#f1f3f7;color:#223}
 .navtab.on{background:#eaf0fa;border-color:#c3d5ee;color:#24457e;font-weight:600}
 .navtab.on .ico{color:#3060a0}
 /* actions: clearly NOT navigation - outlined, not filled */
 .navacts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
 .navbtn{display:inline-flex;align-items:center;gap:6px;height:32px;min-width:0;
   padding:0 11px;border-radius:7px;font-size:12.5px;cursor:pointer;
   background:#fff;border:1px solid #d3d8e2;color:#44506a}
 .navbtn:hover{background:#f4f6fa;border-color:#b9c2d2}
 .navbtn:disabled{opacity:.55;cursor:default}
 .navbtn.danger{color:#8d3b3b;border-color:#e6cfcf}
 .navbtn.danger:hover{background:#fdf3f3;border-color:#d9b3b3;color:#7a2f2f}
 .navbtn.busy{opacity:.6;cursor:progress}
 @media (max-width:820px){.navtab span,.navbtn span{display:none}
   .navtab,.navbtn{padding:0 9px}}
"""


#: Behaviour that belongs to the chrome, not to any one page. Every page has
#: a `#help` panel and now every page has Shut down, so both live here rather
#: than being re-implemented four times (only the import page used to have a
#: quit button at all, so the other three had no way to stop the app).
CHROME_JS = r"""
(function(){
  var help=document.getElementById("help"),hb=document.getElementById("helpBtn");
  if(hb&&help){
    // start collapsed everywhere; the import page used to open expanded
    if(!help.style.display)help.style.display="none";
    hb.onclick=function(){
      var open=help.style.display!=="none";
      help.style.display=open?"none":"block";
      hb.classList.toggle("on",!open);
      if(!open)help.scrollIntoView({behavior:"smooth",block:"nearest"});
    };
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
        +'text-align:center;font:15px/1.6 system-ui,sans-serif;color:#445">'
        +'<div style="font-size:34px">&#9679;</div><h2 style="font-weight:600">'
        +'The app has stopped</h2><p>Your data is saved. Close this tab, and '
        +'launch again from the desktop shortcut when you need it.</p></div>';
    }).catch(function(){});
  };
})();
"""


def chrome(page: str, active: str, actions: str = "") -> str:
    """Substitute the shared CSS, nav and behaviour into a page template."""
    return (page.replace("__CHROME_CSS__", CHROME_CSS)
                .replace("__NAV__", nav(active, actions))
                .replace("__CHROME_JS__", CHROME_JS))
