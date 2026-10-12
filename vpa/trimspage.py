"""The Trims page: how stored trim names are grouped, and what the rules
do not cover yet.

    /trims            the page
    /api/trims        payload()

Read-only on the database, always: it reads the analysis frame (trims.py
does the grouping) and trim_rules.json. Every section answers "is a car in
the group it belongs to?":

  * per make - cars, trim groups, and how many cars the generation rules
    place (a make with no rules shows the general clean-up only);
  * trim names the rules leave without a generation, most cars first, with
    how they were stored and a title, so a rule can be written for them;
  * every stored name and the group it lands in - searchable, so "where did
    my 'Auto quattro MY18' go?" has an answer.
"""
from __future__ import annotations

from pathlib import Path

from . import trims
from .store import DEFAULT_DB, read_frame


def payload(db_path: str | Path = DEFAULT_DB) -> dict:
    df = read_frame(db_path)
    names = []
    if not df.empty:
        one = df.drop_duplicates("key", keep="last")
        g = (one.assign(_s=one["variant_stored"].fillna(""), _v=one["variant"].fillna(""),
                        _g=one["generation"].fillna(""))
             .groupby(["make", "model", "_s", "_v", "_g"])["key"].nunique())
        for (mk, mo, s, v, gen), n in g.items():
            names.append({"make": str(mk), "model": str(mo), "stored": s or None,
                          "variant": v or None, "generation": gen or None, "cars": int(n)})
        names.sort(key=lambda r: (r["make"], r["model"], r["variant"] or "", -r["cars"]))
    return {"ok": True, "coverage": trims.coverage(df), "unmatched": trims.unmatched(df),
            "names": names, "rules_file": str(trims.RULES_PATH),
            "thresholds": {"model": trims.WORDING_MIN_MODEL, "share": trims.WORDING_SHARE}}


_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Trims &middot; VPA</title>
<style>
__CHROME_CSS__
 .cov td,.cov th{white-space:nowrap}
 .bar{display:inline-block;height:7px;border-radius:4px;background:var(--line);width:90px;
   vertical-align:middle;margin-right:6px;overflow:hidden}
 .bar i{display:block;height:100%;background:var(--accent)}
 .um .ttl{font-size:11.5px;color:var(--dim);max-width:420px;overflow:hidden;
   text-overflow:ellipsis;white-space:nowrap}
 #q{width:100%;max-width:360px}
 .names td{vertical-align:top}
 .names .moved{color:var(--ink-2)}
 .same{color:var(--faint)}
 @media (max-width:700px){.um .ttl{max-width:180px}}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Trims</h1>
 <div class="sub">How stored trim names are grouped, and what the rules don't cover yet. Nothing here changes your data.</div>
</div>__ACTIONS__</div>

<div class="card">
 <div class="ptitle">by make</div>
 <table class="cov"><thead><tr><th>make</th><th class="num">cars</th><th class="num">trim groups</th>
  <th class="num">with 8+ cars</th><th>generation rules</th></tr></thead><tbody id="cov"></tbody></table>
 <div class="hint note">Trim names are grouped when the data is read: gearbox, model-year
  and "Mk II" spellings are folded away, and names that differ only in capitals are one trim.
  A make with <b>generation rules</b> (in <code id="rulesfile"></code>) also has each car placed
  in its generation &mdash; BMW chassis codes, Ford series codes &mdash; which the car check
  uses when a trim has too few cars. Uploads are checked too: a page whose trim names are
  unlike the data's is imported, and flagged on the upload page.</div>
</div>

<div class="card" id="umCard">
 <div class="ptitle">no generation found <span class="flat" id="umspan"></span></div>
 <table class="um"><thead><tr><th>make &amp; model</th><th>trim</th><th class="num">cars</th>
  <th>years</th><th>stored as / a title</th></tr></thead><tbody id="um"></tbody></table>
 <div class="hint">Cars of a make that has generation rules, where none applied &mdash; usually
  a trim written without its code, or years the rules don't list yet.</div>
</div>

<div class="card">
 <div class="ptitle">every stored name and its group <span class="flat" id="nspan"></span></div>
 <input type="search" id="q" placeholder="search, e.g. quattro, FG, 420i" autocomplete="off">
 <table class="names" style="margin-top:10px"><thead><tr><th>make &amp; model</th><th>stored as</th>
  <th>grouped as</th><th>generation</th><th class="num">cars</th></tr></thead><tbody id="names"></tbody></table>
 <div class="hint" id="nmore"></div>
</div>
<div id="msg"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let D=null;
const MAXN=400;
function say(html){$("msg").innerHTML=html?`<div class="warnbox" style="margin-top:10px">${html}</div>`:"";}
function bar(n,of){const p=of?Math.round(n/of*100):0;
  return `<span class="bar"><i style="width:${p}%"></i></span>${p}%`;}
function renderNames(){
  const q=$("q").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const hay=r=>[r.make,r.model,r.stored,r.variant,r.generation].join(" ").toLowerCase();
  const rows=D.names.filter(r=>q.every(w=>hay(r).includes(w)));
  $("names").innerHTML=rows.slice(0,MAXN).map(r=>`<tr><td>${esc(r.make)} ${esc(r.model)}</td>
    <td>${esc(r.stored||"(no trim)")}</td>
    <td class="${r.stored===r.variant?"same":"moved"}">${r.stored===r.variant?"same":esc(r.variant||"(no trim)")}</td>
    <td>${esc(r.generation||"")}</td><td class="num">${r.cars}</td></tr>`).join("")
    ||'<tr><td colspan="5" class="flat">nothing matches</td></tr>';
  $("nspan").textContent=`— ${rows.length} of ${D.names.length}`;
  $("nmore").textContent=rows.length>MAXN?`Showing the first ${MAXN} - search to narrow it down.`:"";
}
function render(){
  $("rulesfile").textContent=D.rules_file.split(/[\\/]/).slice(-2).join("/");
  $("cov").innerHTML=D.coverage.map(r=>`<tr><td><b>${esc(r.make)}</b></td><td class="num">${r.cars.toLocaleString()}</td>
    <td class="num">${r.groups}</td><td class="num">${r.groups_8}</td>
    <td>${r.generation_rules?bar(r.with_generation,r.cars)+` <span class="flat">${r.with_generation.toLocaleString()} placed</span>`
      :'<span class="flat">none yet - general clean-up only</span>'}</td></tr>`).join("");
  $("umCard").style.display=D.unmatched.length?"":"none";
  $("umspan").textContent=`— ${D.unmatched.reduce((a,r)=>a+r.cars,0)} cars`;
  $("um").innerHTML=D.unmatched.map(r=>`<tr><td>${esc(r.make)} ${esc(r.model)}</td>
    <td>${esc(r.variant||"(no trim)")}</td><td class="num">${r.cars}</td>
    <td>${r.years?(r.years[0]===r.years[1]?r.years[0]:r.years[0]+"–"+r.years[1]):""}</td>
    <td><div class="ttl" title="${esc((r.stored.concat(r.titles)).join(" | "))}">${esc(r.stored.join(", ")||"")}${r.titles[0]?` &middot; ${esc(r.titles[0])}`:""}</div></td></tr>`).join("");
  renderNames();
}
$("q").oninput=renderNames;
async function load(){
  vpaBusy(true);
  try{D=await fetch("/api/trims").then(r=>r.json());}
  catch(e){say("Could not load: "+esc(e));return;}
  finally{vpaBusy(false);}
  if(!D.ok){say(esc(D.error||"failed"));return;}
  const q=new URLSearchParams(location.search).get("q");
  if(q)$("q").value=q;
  render();
}
load();
__CHROME_JS__
</script></div></body></html>
"""

from .ui import chrome as _chrome   # noqa: E402

PAGE = _chrome(_PAGE, "trims")
