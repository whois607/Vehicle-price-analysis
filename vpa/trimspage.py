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

import json
from pathlib import Path

from . import trims
from .store import DEFAULT_DB, read_frame


def _dismissed_path(db_path) -> Path:
    """Suggestions you said no to - private, next to the database."""
    return Path(db_path).resolve().parent / "trim_dismissed.json"


def dismissed(db_path) -> list[str]:
    try:
        return list(json.loads(_dismissed_path(db_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def dismiss(db_path, sid: str, on: bool = True) -> dict:
    cur = set(dismissed(db_path))
    (cur.add if on else cur.discard)(str(sid))
    p = _dismissed_path(db_path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(sorted(cur), indent=0), encoding="utf-8")
    from .phonecfg import _replace
    _replace(tmp, p)
    return {"ok": True}


def backup_dir(db_path) -> Path:
    return Path(db_path).resolve().parent / "backups"


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
    # the year tables and badge rules, each row with the cars it places -
    # drafted from general knowledge, so laid out to be checked
    tables = []
    one = df.drop_duplicates("key") if not df.empty else df
    for mk, own in sorted(trims.rules().get("makes", {}).items()):
        if not (own.get("years") or own.get("badges")):
            continue                     # none of its own ('same_as' borrows them)
        R = trims.make_rules(mk)
        for mo in sorted(set((R.get("years") or {})) | set((R.get("badges") or {}))):
            line = one[(one["make"] == mk) & (one["model"] == mo)] if not one.empty else one
            gens = line["generation"].value_counts() if not line.empty else {}
            rows = [{"gen": g, "from": lo, "to": hi, "cars": int(gens.get(g, 0))}
                    for g, lo, hi in (R.get("years") or {}).get(mo, [])]
            both = {str(g): int(n) for g, n in (gens.items() if len(line) else [])
                    if "/" in str(g)}
            tables.append({"make": mk, "model": mo, "rows": rows,
                           "badges": [{"pattern": p, "gen": g}
                                      for p, g in (R.get("badges") or {}).get(mo, [])],
                           "overlap": both, "cars": int(len(line)),
                           "unplaced": int(line["generation"].isna().sum()) if len(line) else 0})
    return {"ok": True, "coverage": trims.coverage(df), "unmatched": trims.unmatched(df),
            "tables": tables,
            "suggestions": trims.suggestions(df, dismissed(db_path)),
            "shorthand": trims.rules().get("shorthand", []),
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
 .tblgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-top:10px}
 .tblgrid table td,.tblgrid table th{padding:4px 8px}
 .tblgrid .mini-h{margin:0 0 4px}
 .sug{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:10px;
   display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
 .sug .what{flex:1;min-width:220px}
 .sug .what b.big{font-size:14px}
 .sug .ex{font-size:12px;color:var(--ink-2);margin-top:4px;line-height:1.6}
 .sug .acts{display:flex;gap:6px}
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

<div class="card" id="sugCard">
 <div class="ptitle">suggested shorthand <span class="flat" id="sugspan"></span></div>
 <div class="hint note">Words a page wrote in shorthand, each found only where your data already
  has the long form: run-together codes (<b>FGX</b> = FG X), a code and a digit (<b>FG2</b> =
  FG MkII), and one- or two-letter abbreviations (<b>XR6 T</b> = XR6 Turbo). <b>Approve</b> adds
  it to <code>trim_rules.json</code> (backed up first) and those cars join their proper group
  straight away; nothing in your data changes, and <b>Remove</b> below takes it back out.</div>
 <div id="sug"></div>
 <div id="shCard" style="display:none;margin-top:14px">
  <div class="mini-h">shorthand in use</div>
  <table><tbody id="sh"></tbody></table></div>
</div>

<div class="card" id="tblCard">
 <div class="ptitle">generations by year &mdash; please check these</div>
 <div class="hint note">These year ranges were drafted from general knowledge of Australian
  model years, not from your data, so check them. Each row shows how many of your cars it
  places. A car whose year two generations share (both were on sale) is not guessed: it gets
  both names, e.g. <b>B8/B9</b>. A <b>badge</b> rule decides outright from the trim name
  (an LS460 is always an XF40). To change one, edit <code id="rulesfile2"></code> &mdash; every
  car regroups on the next page load, and nothing in your data changes.</div>
 <div id="tbls" class="tblgrid"></div>
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
function say(html,ok){$("msg").innerHTML=html?`<div class="${ok?"okbox":"warnbox"}" style="margin-top:10px">${html}</div>`:"";}
async function post(url,body){
  vpaBusy(true);
  try{return await fetch(url,{method:"POST",body:JSON.stringify(body)}).then(r=>r.json());}
  catch(e){return {ok:false,error:String(e)};}
  finally{vpaBusy(false);}
}
function renderSug(){
  const S=D.suggestions||[];
  $("sugspan").textContent=S.length?`— ${S.length}`:"— none right now";
  $("sug").innerHTML=S.map((s,i)=>`<div class="sug"><div class="what">
    <b class="big">${esc(s.from)}</b> &rarr; <b class="big">${esc(s.to)}</b>
    <span class="flat">&middot; ${esc(s.make)} ${esc(s.model)} &middot; ${s.cars} car${s.cars===1?"":"s"}</span>
    <div class="ex">${esc(s.why)}<br>${s.examples.map(e=>`${esc(e.now)} &rarr; <b>${esc(e.then)}</b>${e.cars>1?` (${e.cars})`:""}`).join("<br>")}</div></div>
    <div class="acts"><button class="primary mini" data-ok="${i}">Approve</button>
    <button class="mini" data-no="${i}">Not this</button></div></div>`).join("")
    ||'<div class="flat" style="margin-top:8px">Nothing to suggest &mdash; every trim word is already known.</div>';
  const H=D.shorthand||[];
  $("shCard").style.display=H.length?"":"none";
  $("sh").innerHTML=H.map((e,i)=>`<tr><td>${esc(e.make)} ${esc(e.model||"(every model)")}</td>
    <td><b>${esc(e.from)}</b> &rarr; <b>${esc(e.to)}</b></td><td class="flat">${esc(e.approved||"")}</td>
    <td><button class="mini" data-rm="${i}">Remove</button></td></tr>`).join("");
  document.querySelectorAll("[data-ok]").forEach(b=>b.onclick=async()=>{
    const s=S[+b.dataset.ok];
    const r=await post("/api/trims/approve",{make:s.make,model:s.model,from:s.from,to:s.to});
    if(!r.ok){say(esc(r.error));return;}
    await load();say(`Approved: <b>${esc(s.from)}</b> means <b>${esc(s.to)}</b> for ${esc(s.make)} ${esc(s.model)}.
      Rules file backed up first: <code>${esc(r.backup)}</code>`,true);});
  document.querySelectorAll("[data-no]").forEach(b=>b.onclick=async()=>{
    await post("/api/trims/dismiss",{id:S[+b.dataset.no].id,on:true});await load();});
  document.querySelectorAll("[data-rm]").forEach(b=>b.onclick=async()=>{
    const e=H[+b.dataset.rm];
    if(!confirm(`Remove "${e.from} = ${e.to}"? Those cars go back to the names they were stored with.`))return;
    const r=await post("/api/trims/remove",e);
    if(!r.ok){say(esc(r.error));return;}
    await load();say(`Removed. Rules file backed up first: <code>${esc(r.backup)}</code>`,true);});
}
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
  $("rulesfile2").textContent=$("rulesfile").textContent;
  renderSug();
  $("tblCard").style.display=D.tables.length?"":"none";
  $("tbls").innerHTML=D.tables.map(t=>`<div><div class="mini-h">${esc(t.make)} ${esc(t.model)}
    <span class="flat">&middot; ${t.cars} cars</span></div><table><tbody>
    ${t.rows.map(r=>`<tr><td><b>${esc(r.gen)}</b></td><td>${r.from}&ndash;${r.to==null?"now":r.to}</td>
      <td class="num">${r.cars}</td></tr>`).join("")}
    ${t.badges.map(b=>`<tr><td><b>${esc(b.gen)}</b></td><td colspan="2"><span class="flat">badge</span>
      <code>${esc(b.pattern)}</code></td></tr>`).join("")}
    ${Object.entries(t.overlap).map(([g,n])=>`<tr><td><b>${esc(g)}</b></td><td><span class="flat">year shared</span></td>
      <td class="num">${n}</td></tr>`).join("")}
    ${t.unplaced?`<tr><td colspan="2"><span class="flat">not placed</span></td><td class="num">${t.unplaced}</td></tr>`:""}
    </tbody></table></div>`).join("");
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
