"""The laptop's Phone page: turn phone sync on, pair phones, see the copy.

Everything it does goes through phone.py / phonecfg.py; this is only the page.
"""
from __future__ import annotations

from .ui import chrome as _chrome

_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Phone &middot; VPA</title>
<style>
__CHROME_CSS__
 #pstat{display:flex;align-items:center;gap:12px;height:48px;padding:0 16px;
   border:1px solid var(--line-2);border-radius:var(--r-sm);background:var(--head);font-size:13.5px}
 #pstat .dot{width:10px;height:10px;border-radius:50%;background:var(--faint);flex:none}
 #pstat.on{background:var(--good-soft);border-color:var(--good-line)}
 #pstat.on .dot{background:var(--good)}
 #pstat.bad{background:var(--bad-soft);border-color:var(--bad-line)}
 #pstat.bad .dot{background:var(--bad)}
 #ptext{flex:1;min-width:0}
 #codebox{display:none;margin-top:14px;padding:18px;border:1px solid var(--accent-line);
   border-radius:var(--r-sm);background:var(--accent-soft)}
 #code{font-family:var(--ui-d);font-size:40px;font-weight:700;letter-spacing:.18em;
   color:var(--accent-2);font-variant-numeric:tabular-nums;line-height:1.1}
 #codebox ol{margin:10px 0 0;padding-left:20px;font-size:13px;color:var(--ink-2)}
 #codebox code{font-size:12.5px}
 .opt{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line-2)}
 .opt:first-of-type{border-top:0}
 .opt input{margin-top:3px;width:16px;height:16px;flex:none}
 .opt>span{font-size:13.5px;color:var(--ink);text-transform:none}
 .opt b{font-weight:600}.opt .d{font-size:12.5px;color:var(--dim);margin-top:2px}
 #devs td{vertical-align:middle}
 .urls code{display:inline-block;margin:4px 6px 0 0;font-size:13px}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Phone</h1>
 <div class="sub">Carry your data to the car yard: your phone keeps its own copy, searchable with no signal.</div>
</div>__ACTIONS__</div>

<div class="card" id="help" style="display:none">
 <p><b>How it works.</b> This laptop keeps a compact, read-only copy of your data
 (every car, every sighting, the forecasts) and rebuilds it within seconds of any
 change. A paired phone downloads it whenever it can reach this laptop, and only
 when it changed. At a yard the phone uses the copy it already has.</p>
 <ul style="font-size:13px">
  <li><b>Nothing on the phone can change your database.</b> The phone talks to a
   separate listener that only hands out the copy; it has no way to import,
   edit, undo or delete.</li>
  <li><b>Pairing</b>: press <b>Pair a phone</b>, open the address on the phone and
   type the six-digit code. The phone then holds its own key; <b>Remove</b> takes
   it away again.</li>
  <li><b>On the phone</b>: Home, Find (paste an advert link, browse, search a range),
   Price check, Compare and Starred - the same figures as here, read-only, and searchable with
   no signal. It syncs by itself whenever it is opened or brought back to the front.</li>
  <li><b>Home Wi-Fi</b> lets a phone on the same Wi-Fi sync over plain http - good
   for trying it out. Opening VPA with no signal needs the HTTPS address
   (Tailscale), which is the next step.</li>
  <li><b>Keep running when VPA is closed</b> starts a small background listener
   with Windows, so the phone can sync whenever this laptop is awake.</li>
 </ul>
</div>

<div class="card">
 <div class="cardhead"><div class="ptitle">phone sync</div>
  <button class="primary" id="onBtn">Turn on</button></div>
 <div id="pstat"><span class="dot"></span><span id="ptext">checking&hellip;</span></div>
</div>

<div class="card" id="devCard">
 <div class="cardhead"><div class="ptitle">phones <span id="devspan"></span></div>
  <button id="pairBtn" disabled>Pair a phone</button></div>
 <table id="devs"><thead><tr><th>phone</th><th>paired</th><th>last synced</th><th>copy</th><th></th></tr></thead>
  <tbody></tbody></table>
 <div id="nodev" class="flat" style="font-size:13px;padding:8px 2px">No phones paired yet.</div>
 <div id="codebox">
  <div class="flat" style="font-size:12.5px">pairing code &mdash; <span id="left"></span></div>
  <div id="code"></div>
  <ol><li>On the phone, open <span id="where"></span></li>
   <li>Type this code and press <b>Pair</b>.</li></ol>
  <div style="margin-top:10px"><button class="mini" id="cancelPair">Cancel</button></div>
 </div>
</div>

<div class="card">
 <div class="cardhead"><div class="ptitle">the phone copy <span id="copyspan"></span></div>
  <button class="mini" id="rebuildBtn">Rebuild now</button></div>
 <div class="tiles" id="tiles"></div>
 <div class="hint note">The copy is rebuilt automatically a few seconds after any change
  to the data &mdash; imports, the watch folder, undo, edits and merges &mdash; and only
  when something changed. It holds every car and sighting, and this laptop's own Explore,
  Compare and Overview results for every make, model and trim, so the phone shows the same
  numbers. The first build takes a minute or two; after that only what an import touched is
  recomputed. No seller names or
  contact details are ever captured, so none can be in it.</div>
</div>

<div class="card">
 <div class="ptitle">how the phone reaches this laptop</div>
 <label class="opt"><input type="checkbox" id="optLan"><span><b>Home Wi-Fi</b>
  <div class="d">Phones on the same Wi-Fi can sync over plain http. Windows will ask
   whether to allow Python on networks &mdash; allow <b>Private networks</b> only.
   Good for trying it out; to open VPA with no signal the phone needs the HTTPS
   address (Tailscale), set up next.</div>
  <div class="urls" id="urls"></div></span></label>
 <label class="opt"><input type="checkbox" id="optBg"><span><b>Keep phone sync running when VPA is closed</b>
  <div class="d">Starts a small background listener with Windows (it only hands out
   the copy and rebuilds it when the data changes). Turn off here to remove it.</div></span></label>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let ST=null, CODE=null, CODET=null;
const when=t=>{if(!t)return "never";const d=new Date(String(t).replace(" ","T"));
  const s=(Date.now()-d)/1000;
  return s<90?"just now":s<3600?Math.round(s/60)+" min ago":s<86400?Math.round(s/3600)+" h ago"
    :d.toLocaleDateString(undefined,{day:"numeric",month:"short"})+" "+d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"});};
async function api(path,body){
  try{return await fetch(path,body===undefined?{}:{method:"POST",body:JSON.stringify(body)}).then(r=>r.json());}
  catch(e){return {ok:false,error:String(e)};}
}
function addr(){
  if(ST.lan&&ST.lan_urls.length)return ST.lan_urls.map(u=>`<code>${esc(u)}</code>`).join(" or ");
  return `the VPA address &mdash; switch on <b>Home Wi-Fi</b> below to get one for now`;
}
function paint(s){
  ST=s;
  const st=$("pstat"), on=s.enabled;
  $("onBtn").textContent=on?"Turn off":"Turn on";
  $("onBtn").className=on?"":"primary";
  $("pairBtn").disabled=!on;
  const m=s.meta, b=s.build;
  let txt, cls="";
  if(!on){txt="Off. No phone can sync.";}
  else if(s.status==="blocked"||s.status==="error"){txt=s.error||"phone sync could not start";cls="bad";}
  else if(b&&b.error){txt="Could not build the phone copy: "+b.error;cls="bad";}
  else{cls="on";
    const who=s.status==="elsewhere"?"Running in the background":s.status==="here"?"On":"Starting";
    const ways=s.lan?"this laptop and your home Wi-Fi":"this laptop only (HTTPS route not set up yet)";
    txt=`${who} &mdash; paired phones can sync over ${ways}.`;}
  st.className=cls;$("ptext").innerHTML=txt;
  // phones
  const tb=$("devs").querySelector("tbody");
  tb.innerHTML=(s.devices||[]).map(d=>`<tr><td><b>${esc(d.name)}</b></td><td>${esc(when(d.paired))}</td>
    <td>${esc(when(d.last_seen))}</td>
    <td>${!d.last_version?'<span class="flat">not yet</span>':m&&d.last_version===m.version
      ?'<span class="tag good">up to date</span>':'<span class="tag">older copy</span>'}</td>
    <td style="text-align:right"><button class="mini danger" data-rm="${esc(d.id)}">Remove</button></td></tr>`).join("");
  $("devs").style.display=(s.devices||[]).length?"":"none";
  $("nodev").style.display=(s.devices||[]).length?"none":"";
  $("devspan").textContent=(s.devices||[]).length?`${s.devices.length} paired`:"";
  tb.querySelectorAll("[data-rm]").forEach(bt=>bt.onclick=async()=>{
    if(!confirm("Remove this phone? It keeps the copy it has, but cannot sync again until it is paired again."))return;
    paint(Object.assign(ST,await api("/api/phone/remove",{id:bt.dataset.rm})));load();});
  if(!s.pairing&&CODE){CODE=null;}
  $("codebox").style.display=CODE?"block":"none";
  if(CODE)$("where").innerHTML=addr();
  // the copy
  $("copyspan").textContent=s.building?"rebuilding...":m?`built ${when(m.built)} in ${m.seconds}s`
    +(m.counts.last_day?` · latest capture ${m.counts.last_day}`:""):"";
  $("tiles").innerHTML=m?[
    [m.counts.vehicles.toLocaleString(),"cars"],[m.counts.sightings.toLocaleString(),"sightings"],
    [(m.counts.selections||0).toLocaleString(),"makes, models & trims"],[Math.round(m.bytes/1024)+" KB","download"]].map(([v,l])=>`<div class="tile"><b>${esc(v)}</b><span>${l}</span></div>`).join("")
    :`<div class="flat" style="font-size:13px">${on?"Building the first copy&hellip;":"Built once phone sync is on."}</div>`;
  $("optLan").checked=s.lan;$("optBg").checked=s.background;
  $("optLan").disabled=$("optBg").disabled=!on;
  $("urls").innerHTML=s.lan&&s.lan_urls.length?s.lan_urls.map(u=>`<code>${esc(u)}</code>`).join(""):"";
}
async function load(){const s=await api("/api/phone");if(s.ok)paint(s);}
$("onBtn").onclick=async()=>{
  const on=!(ST&&ST.enabled);
  if(!on&&!confirm("Turn phone sync off? Phones keep the copy they have, and stay paired for when you turn it back on."))return;
  $("onBtn").disabled=true;
  const r=await api("/api/phone/options",{enabled:on});
  $("onBtn").disabled=false;
  if(!r.ok){alert(r.error);return;}
  paint(r);setTimeout(load,1500);
};
$("optLan").onchange=async e=>{const r=await api("/api/phone/options",{lan:e.target.checked});
  if(!r.ok)alert(r.error);else paint(r);setTimeout(load,1500);};
$("optBg").onchange=async e=>{const r=await api("/api/phone/options",{background:e.target.checked});
  if(!r.ok){alert(r.error);e.target.checked=!e.target.checked;}else paint(r);setTimeout(load,2500);};
$("pairBtn").onclick=async()=>{
  const r=await api("/api/phone/pair",{});
  if(!r.ok){alert(r.error);return;}
  CODE=r.code;$("code").textContent=r.code.slice(0,3)+" "+r.code.slice(3);
  const end=Date.now()+r.seconds*1000;
  clearInterval(CODET);
  CODET=setInterval(()=>{const s=Math.max(0,Math.round((end-Date.now())/1000));
    $("left").textContent=`valid for ${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
    if(!s){clearInterval(CODET);CODE=null;load();}},500);
  await load();
};
$("cancelPair").onclick=async()=>{CODE=null;clearInterval(CODET);await api("/api/phone/pair/cancel",{});load();};
$("rebuildBtn").onclick=async()=>{
  $("rebuildBtn").disabled=true;$("copyspan").textContent="rebuilding...";
  const r=await api("/api/phone/rebuild",{});
  $("rebuildBtn").disabled=false;
  if(!r.ok)alert((r.build&&r.build.error)||r.error||"rebuild failed");
  load();
};
load();setInterval(load,4000);   // pairing completes on the phone; show it here
__CHROME_JS__
</script></body></html>
"""

PAGE = _chrome(_PAGE, "phone")
