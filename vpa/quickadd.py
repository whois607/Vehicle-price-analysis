"""Quick Add — keyboard-first price jotting for one model at a time.

Served by the import UI process at /quick. Pick (or type) a model, rattle
off asking prices with Enter, watch the stats move, commit the batch.

Definitions (per the user's choices):
  * Live stats (median, mode, highest, lowest, always with n) are computed
    over ALL stored prices for the model — every priced observation from
    every source — PLUS the current batch, recomputed on every Enter.
  * The %-change baseline is the same all-history pool as it stood at the
    model's most recent capture date BEFORE the batch date (any source).
    First-ever data for a model -> "no prior data".
  * Mode is computed on prices bucketed to the nearest $500 (raw asking
    prices rarely repeat). No bucket with 2+ prices -> mode is "none";
    tied buckets -> the lowest tied bucket, flagged as a tie.

This module owns no schema and no parsing: prices go through
schema.parse_price, committed rows through schema.make_record and
Store.ingest exactly like the importer. Each price gets a synthesized
unique listing id (QA-<epoch>-<i>) so five prices jotted the same day are
five listings, not one vehicle's history.
"""
from __future__ import annotations

import statistics
import uuid
from datetime import date
from pathlib import Path

from .schema import make_record, norm_make, norm_model, parse_price
from .store import Store

MODE_BUCKET = 500


# ------------------------------------------------------------------- stats
def price_stats(prices: list[float]) -> dict | None:
    """median/mode/highest/lowest over a price pool. None if pool empty.
    mode: {'value', 'count', 'tie'} on $500 buckets, or None if all unique."""
    if not prices:
        return None
    buckets: dict[float, int] = {}
    for p in prices:
        b = round(p / MODE_BUCKET) * MODE_BUCKET
        buckets[b] = buckets.get(b, 0) + 1
    top = max(buckets.values())
    mode = None
    if top >= 2:
        tied = sorted(b for b, c in buckets.items() if c == top)
        mode = {"value": tied[0], "count": top, "tie": len(tied) > 1}
    return {"n": len(prices), "median": float(statistics.median(prices)),
            "highest": float(max(prices)), "lowest": float(min(prices)),
            "mode": mode}


def pct_change(now: float | None, prev: float | None) -> float | None:
    """(now - prev) / prev * 100, or None when undefined."""
    if now is None or prev is None or prev == 0:
        return None
    return (now - prev) / prev * 100.0


def stat_changes(now: dict | None, baseline: dict | None) -> dict | None:
    """Per-stat signed %; None overall when there is no baseline pool."""
    if not now or not baseline:
        return None
    return {
        "median": pct_change(now["median"], baseline["median"]),
        "highest": pct_change(now["highest"], baseline["highest"]),
        "lowest": pct_change(now["lowest"], baseline["lowest"]),
        "mode": pct_change(now["mode"]["value"] if now["mode"] else None,
                           baseline["mode"]["value"] if baseline["mode"] else None),
    }


# ---------------------------------------------------------------- database
def _stored_prices(db_path: str | Path, make: str, model: str,
                   variant: str | None = None,
                   before: str | None = None) -> list[float]:
    """Every priced observation for the model line, any source.
    With a variant: only that trim's rows (a plain model line pools all)."""
    sql = """SELECT o.price_aud FROM observations o
             JOIN vehicles v ON v.key = o.key
             WHERE v.make = ? AND v.model = ? AND o.price_aud IS NOT NULL"""
    args: list = [make, model]
    if variant:
        sql += " AND v.variant = ?"
        args.append(variant)
    if before:
        sql += " AND o.capture_date < ?"
        args.append(before)
    with Store(db_path) as store:
        return [r[0] for r in store.conn.execute(sql, args)]


def models_payload(db_path: str | Path) -> dict:
    """Pickable identities: every model line (pools all trims) PLUS every
    make+model+variant combination in the store - pick the specific trim
    ('Ford Falcon FGX XR6 T') when you're jotting prices for one."""
    with Store(db_path) as store:
        rows = store.conn.execute("""
            SELECT v.make, v.model, v.variant, v.vehicle_type,
                   COUNT(o.id) AS n, MAX(o.capture_date) AS last_seen
            FROM vehicles v JOIN observations o ON o.key = v.key
            WHERE o.price_aud IS NOT NULL
            GROUP BY v.make, v.model, v.variant, v.vehicle_type
            ORDER BY n DESC""").fetchall()
    plain: dict[tuple, dict] = {}
    variants = []
    for r in rows:
        key = (r["make"], r["model"], r["vehicle_type"])
        p = plain.setdefault(key, {
            "make": r["make"], "model": r["model"], "variant": None,
            "vehicle_type": r["vehicle_type"],
            "line": f"{r['make']} {r['model']}", "n": 0, "last_seen": ""})
        p["n"] += r["n"]
        p["last_seen"] = max(p["last_seen"], r["last_seen"] or "")
        if r["variant"]:
            variants.append({
                "make": r["make"], "model": r["model"], "variant": r["variant"],
                "vehicle_type": r["vehicle_type"],
                "line": f"{r['make']} {r['model']} {r['variant']}",
                "n": r["n"], "last_seen": r["last_seen"]})
    models = sorted(plain.values(), key=lambda m: -m["n"]) + variants
    return {"ok": True, "models": models}


def stats_payload(payload: dict, db_path: str | Path) -> dict:
    """Parse the raw batch entries, build pools, compute stats + changes.
    Read-only. Called on every Enter — cheap on a local SQLite."""
    make = norm_make(payload.get("make"))
    model = norm_model(payload.get("model"))
    if not make or not model:
        return {"ok": False, "error": "make and model required"}
    variant = (str(payload["variant"]).strip() or None) if payload.get("variant") else None
    batch_date = payload.get("capture_date") or date.today().isoformat()

    entries = []
    batch_prices: list[float] = []
    for raw in payload.get("prices") or []:
        value, note = parse_price(raw)
        entries.append({"raw": raw, "price": value, "note": note,
                        "ok": value is not None})
        if value is not None:
            batch_prices.append(value)

    stored_all = _stored_prices(db_path, make, model, variant)
    baseline_pool = _stored_prices(db_path, make, model, variant, before=batch_date)
    now = price_stats(stored_all + batch_prices)
    baseline = price_stats(baseline_pool)
    line = " ".join(x for x in (make, model, variant) if x)
    return {"ok": True, "make": make, "model": model, "line": line,
            "entries": entries,
            "batch_n": len(batch_prices),
            "stored_n": len(stored_all),
            "baseline_n": len(baseline_pool),
            "now": now, "baseline": baseline,
            "changes": stat_changes(now, baseline)}


def commit_payload(payload: dict, db_path: str | Path) -> dict:
    """Write the batch through the standard pipeline: make_record + ingest."""
    make = norm_make(payload.get("make"))
    model = norm_model(payload.get("model"))
    if not make or not model:
        return {"ok": False, "error": "make and model required"}
    batch_date = payload.get("capture_date") or date.today().isoformat()
    # uuid, NOT a clock: Windows time_ns ticks in ~15ms quanta, so two fast
    # consecutive commits could share a token -> colliding listing ids ->
    # one batch's prices attaching to the other batch's vehicles
    token = uuid.uuid4().hex[:12]
    records, rejected = [], []
    kept = 0
    for raw_price in payload.get("prices") or []:
        raw = {"make": make, "model": model, "price": raw_price,
               "capture_date": batch_date,
               "listing_id": f"QA-{token}-{kept + len(rejected)}",
               "vehicle_type": payload.get("vehicle_type") or None,
               "year": payload.get("year") or None,
               "variant": payload.get("variant") or None}
        rec = make_record({k: v for k, v in raw.items() if v is not None},
                          source="quick_add")
        if rec is None or rec.price_aud is None:
            rejected.append(str(raw_price))
        else:
            records.append(rec)
            kept += 1
    if not records:
        return {"ok": False, "error": "no valid prices to commit",
                "rejected": rejected}
    with Store(db_path) as store:
        counts = store.ingest(records, source_file="(quick-add)",
                              adapter="quick_add")
    return {"ok": True, "committed": counts["obs_new"],
            "duplicates_skipped": counts["obs_duplicate"],
            "rejected": rejected, "line": f"{make} {model}"}


# -------------------------------------------------------------------- page
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>vpa quick add</title>
<style>
 :root{--ui:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif}
 body,button,select,input,textarea{font-family:var(--ui)}
 body{font-size:14px;line-height:1.45;margin:0;background:#f5f6f7;color:#222}
 .wrap{max-width:980px;margin:0 auto;padding:16px}
 .pagetitle{font-size:19px;margin:2px 0 2px;font-weight:600}
__CHROME_CSS__
 .sub{color:#667;font-size:12px;margin-bottom:14px}
 .card{background:#fff;border:1px solid #dde;border-radius:8px;padding:14px;margin-bottom:14px}
 label{font-size:11px;color:#556;display:block;margin-bottom:2px}
 input,select{padding:7px;border:1px solid #bbc;border-radius:5px;font-size:14px;box-sizing:border-box}
 button{height:34px;min-width:106px;padding:0 16px;border:0;border-radius:6px;
        font-size:13.5px;cursor:pointer;color:#222}
 .commit{background:#2e7d32;color:#fff}.commit:disabled{background:#aeb6c2}
 .ghost{background:#eaedf2}.ghost:hover{background:#dfe4ec}
 .mini,a.mini{height:32px;min-width:100px;padding:0 12px;font-size:12.5px;
        background:#eaedf2;border-radius:6px;text-decoration:none;color:#223;
        display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box}
 #entries .x{min-width:0;height:auto}
 .cols{display:flex;gap:14px;flex-wrap:wrap}
 .col-entry{flex:1;min-width:300px}.col-stats{flex:1;min-width:320px}
 #model{width:100%;font-size:15px}
 #price{width:100%;height:44px;font-size:19px;font-variant-numeric:tabular-nums}
 #newmodel{display:none;background:#fffbe8;border:1px solid #e5d9a0;border-radius:6px;
           padding:10px;margin-top:8px}
 #newmodel .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
 #newmodel input,#newmodel select{font-size:13px;padding:5px}
 #entries{list-style:none;margin:10px 0 0;padding:0;max-height:44vh;overflow:auto}
 #entries li{display:flex;align-items:center;gap:8px;padding:4px 6px;border-top:1px solid #eef;
             font-variant-numeric:tabular-nums;font-size:14px}
 #entries li.bad{color:#a03030;text-decoration:line-through}
 #entries .x{margin-left:auto;background:none;border:0;color:#a03030;cursor:pointer;
             font-size:15px;padding:2px 8px}
 table.stats{border-collapse:collapse;width:100%;font-size:14px}
 .stats th{text-align:left;color:#556;font-weight:500;padding:7px 6px;font-size:12px}
 .stats td{padding:7px 6px;border-top:1px solid #eef}
 .stats td.v{font-variant-numeric:tabular-nums;font-size:16px;text-align:right}
 .stats td.pc{text-align:right;font-size:13px;min-width:110px}
 .up{color:#2e7d32}.down{color:#c04040}.na{color:#889}
 .pool{color:#556;font-size:12px;margin:4px 0 10px}
 #result{white-space:pre-line}
 .ok{color:#2e7d32}.warn{color:#a03030}
 kbd{background:#eef1f5;border:1px solid #ccd;border-radius:3px;padding:0 5px;font-size:11px}
</style></head><body><div class="wrap">
__NAV__
<h1 class="pagetitle">Quick add prices</h1>
<div class="sub" id="dbline">connecting...</div>

<div class="card">
 <label for="model">model or specific trim (type to search, or type a new one)</label>
 <input id="model" list="modelList" placeholder="e.g. Ford Falcon FGX XR6 T - or plain Ford Falcon for all trims" autocomplete="off">
 <datalist id="modelList"></datalist>
 <div id="newmodel">
  <b style="font-size:13px">New model</b>
  <div class="row">
   <span><label>make</label><input id="nmMake" size="12"></span>
   <span><label>model</label><input id="nmModel" size="14"></span>
   <span><label>type</label><select id="nmType"><option value="car">car</option>
     <option value="bike">bike</option></select></span>
   <span><label>year (optional)</label><input id="nmYear" size="6" inputmode="numeric"></span>
   <span><label>trim (optional)</label><input id="nmVariant" size="14"></span>
  </div>
 </div>
</div>

<div class="cols">
 <div class="card col-entry">
  <label for="price">price - <kbd>Enter</kbd> adds, <kbd>Ctrl+Enter</kbd> commits</label>
  <input id="price" placeholder="18990" disabled autocomplete="off">
  <ul id="entries"></ul>
  <div style="margin-top:10px">
   <button class="commit" id="commitBtn" disabled>Commit batch</button>
   <span id="batchinfo" style="margin-left:8px;color:#556;font-size:12.5px"></span>
  </div>
 </div>
 <div class="card col-stats">
  <div id="poolline" class="pool">pick a model to see its stats</div>
  <table class="stats"><thead>
   <tr><th></th><th style="text-align:right">all stored + batch</th>
       <th style="text-align:right">change since last capture</th></tr></thead>
   <tbody id="statrows"></tbody></table>
 </div>
</div>

<div class="card" id="resultCard" style="display:none"><div id="result"></div></div>

<script>
const $=id=>document.getElementById(id);
let MODELS=[], sel=null, rawPrices=[], busy=false;

fetch("/api/status").then(r=>r.json()).then(s=>{
  $("dbline").textContent=`database: ${s.db} - ${s.vehicles} vehicles, ${s.observations} observations. Nothing is written until you commit.`;});
function loadModels(){
  fetch("/api/quick/models").then(r=>r.json()).then(d=>{
    MODELS=d.models;
    $("modelList").innerHTML=MODELS.map(m=>`<option value="${m.line}">`).join("");});
}
loadModels();

function findModel(text){
  return MODELS.find(m=>m.line.toLowerCase()===text.trim().toLowerCase());
}
function selectModel(){
  const text=$("model").value.trim();
  if(!text){sel=null;$("price").disabled=true;return;}
  const hit=findModel(text);
  if(hit){
    sel={make:hit.make,model:hit.model,variant:hit.variant,
         vehicle_type:hit.vehicle_type,isNew:false};
    $("newmodel").style.display="none";
  }else{
    // new model: prefill an editable make/model split (first word = make)
    const words=text.split(/\s+/);
    $("nmMake").value=$("nmMake").value||words[0]||"";
    $("nmModel").value=$("nmModel").value||words.slice(1).join(" ");
    $("newmodel").style.display="block";
    sel={isNew:true};
  }
  rawPrices=[];
  $("price").disabled=false;
  refresh();
}
$("model").addEventListener("change",()=>{ $("nmMake").value="";$("nmModel").value=""; selectModel(); if(sel)$("price").focus(); });
$("model").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();selectModel();if(sel)$("price").focus();}});
["nmMake","nmModel","nmType","nmYear","nmVariant"].forEach(id=>
  $(id).addEventListener("change",refresh));

function current(){
  if(!sel)return null;
  if(sel.isNew)return{make:$("nmMake").value.trim(),model:$("nmModel").value.trim(),
    vehicle_type:$("nmType").value,year:$("nmYear").value.trim(),
    variant:$("nmVariant").value.trim()};
  return{make:sel.make,model:sel.model,variant:sel.variant,
         vehicle_type:sel.vehicle_type};
}

async function refresh(){
  const cur=current();
  if(!cur||!cur.make||!cur.model)return;
  busy=true;
  const res=await fetch("/api/quick/stats",{method:"POST",
    body:JSON.stringify({...cur,prices:rawPrices})}).then(r=>r.json());
  busy=false;
  if(!res.ok){$("poolline").textContent=res.error;return;}
  renderEntries(res.entries);
  renderStats(res);
}

function money(v){return v==null?"-":"$"+Number(v).toLocaleString(undefined,{maximumFractionDigits:0});}
function pc(v){
  if(v==null)return '<span class="na">-</span>';
  const cls=v>0?"up":v<0?"down":"na", arrow=v>0?"&#9650;":v<0?"&#9660;":"&#9644;";
  return `<span class="${cls}">${arrow} ${v>=0?"+":""}${v.toFixed(1)}%</span>`;
}
function renderStats(res){
  const now=res.now, base=res.baseline, ch=res.changes;
  $("poolline").innerHTML=`<b>${res.line}</b> - stats over <b>${now?now.n:0}</b> price(s) `+
    `(${res.stored_n} stored + ${res.batch_n} in this batch). `+
    (base?`Baseline: ${res.baseline_n} price(s) before today.`:
          `<span class="warn">no prior data - changes shown once history exists.</span>`);
  const noPrior='<span class="na">no prior data</span>';
  function modeCell(m){
    if(!m)return '<span class="na">none (all unique)</span>';
    return money(m.value)+` <span class="na">(x${m.count}${m.tie?", tie":""})</span>`;
  }
  const rows=[
    ["median", now?money(now.median):"-",  ch?pc(ch.median):noPrior],
    ["mode ($500 buckets)", now?modeCell(now.mode):"-",
      ch?(ch.mode==null?'<span class="na">-</span>':pc(ch.mode)):noPrior],
    ["highest", now?money(now.highest):"-", ch?pc(ch.highest):noPrior],
    ["lowest", now?money(now.lowest):"-",  ch?pc(ch.lowest):noPrior]];
  $("statrows").innerHTML=rows.map(r=>
    `<tr><th>${r[0]}</th><td class="v">${r[1]}</td><td class="pc">${r[2]}</td></tr>`).join("");
  $("batchinfo").textContent=rawPrices.length?`${res.batch_n} valid of ${rawPrices.length} entered`:"";
  $("commitBtn").disabled=!res.batch_n;
}
function renderEntries(entries){
  $("entries").innerHTML=entries.map((e,i)=>
    `<li class="${e.ok?"":"bad"}"><span>${e.ok?money(e.price):e.raw+" (unparsed)"}</span>`+
    `<button class="x" data-i="${i}" title="remove">x</button></li>`).join("");
  document.querySelectorAll("#entries .x").forEach(b=>b.onclick=()=>{
    rawPrices.splice(Number(b.dataset.i),1);refresh();$("price").focus();});
}

$("price").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&e.ctrlKey){e.preventDefault();commit();return;}
  if(e.key==="Enter"){
    e.preventDefault();
    const v=$("price").value.trim();
    if(v&&!busy){rawPrices.push(v);$("price").value="";refresh();}
  }
  if(e.key==="Escape"){$("price").value="";}
});

async function commit(){
  const cur=current();
  if(!cur||!rawPrices.length)return;
  $("commitBtn").disabled=true;
  const res=await fetch("/api/quick/commit",{method:"POST",
    body:JSON.stringify({...cur,prices:rawPrices})}).then(r=>r.json());
  $("resultCard").style.display="";
  if(!res.ok){$("result").innerHTML=`<span class="warn">${res.error}</span>`;return;}
  let msg=`<span class="ok">Committed ${res.committed} price(s) for ${res.line}.</span>`;
  if(res.duplicates_skipped)msg+=`\n${res.duplicates_skipped} duplicate(s) skipped.`;
  if(res.rejected.length)msg+=`\n<span class="warn">Not committed (unparsable): ${res.rejected.join(", ")}</span>`;
  $("result").innerHTML=msg;
  rawPrices=[];
  refresh();      // stats now include the committed rows from the store
  loadModels();   // a new model/trim just became pickable
  $("price").focus();
}
$("commitBtn").onclick=commit;
$("xlsxBtn").onclick=async()=>{
  $("xlsxBtn").disabled=true;$("xlsxBtn").textContent="updating...";
  try{
    const res=await fetch("/api/viewer/open",{method:"POST",body:"{}"}).then(r=>r.json());
    if(!res.ok)alert(String(res.error).includes("unknown endpoint")
      ?"The app is running an older version - click Shut down, then reopen Vehicle Import."
      :res.error);
  }catch(e){alert("could not update the workbook: "+e);}
  $("xlsxBtn").disabled=false;$("xlsxBtn").textContent="Excel viewer";
};
$("model").focus();
__CHROME_JS__
</script></div></body></html>
"""


from .ui import action as _action, chrome as _chrome   # noqa: E402

PAGE = _chrome(_PAGE, "quick",
               _action("xlsxBtn", "Excel viewer", "excel",
                       "rebuild viewer.xlsx and open it"))
