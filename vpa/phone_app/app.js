/* VPA phone - screens, sync and charts.

   Read-only by construction: the phone holds its own copy of the data
   (IndexedDB) and only ever downloads it. There is no route from here to
   anything that changes the laptop's database.

   Sync: whenever the app is opened or brought back to the front, when the
   network comes back, and every five minutes while it is on screen. A copy
   is only downloaded when the laptop's is newer; at a car yard with no
   signal the app simply keeps using the copy it has. (Firefox does not let a
   closed web app run, so "while closed" is not possible - opening it is
   enough.) */
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;

/* ------------------------------------------------------------ formatting */
const AUD = new Intl.NumberFormat("en-AU", { maximumFractionDigits: 0 });
const money = v => v == null ? "–" : (v < 0 ? "-$" : "$") + AUD.format(Math.abs(Math.round(v)));
const moneyK = v => {
  if (v == null) return "–";
  const a = Math.abs(v), s = v < 0 ? "-$" : "$";
  return a >= 1e6 ? s + (+(a / 1e6).toFixed(2)) + "m" : a >= 1e4 ? s + Math.round(a / 1e3) + "k"
    : a >= 1e3 ? s + (+(a / 1e3).toFixed(1)) + "k" : s + Math.round(a);
};
const km = v => v == null ? "–" : AUD.format(Math.round(v)) + " km";
const kmK = v => v == null ? "–" : Math.round(v / 1000) + "k km";
const n0 = v => v == null ? "–" : AUD.format(v);
const pct = (v, sign) => v == null ? "–" : (sign && v > 0 ? "+" : "") + (+v.toFixed(1)) + "%";
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dShort = d => d ? `${+d.slice(8, 10)} ${MON[+d.slice(5, 7) - 1]}` : "–";
const dLong = d => d ? `${+d.slice(8, 10)} ${MON[+d.slice(5, 7) - 1]} ${d.slice(0, 4)}` : "–";
const mShort = m => m ? `${MON[+m.slice(5, 7) - 1]} ${m.slice(2, 4)}` : "–";
function ago(t) {
  if (!t) return "never";
  const s = (Date.now() - new Date(t).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : d + " days ago";
}
const ordinal = n => n + (["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");
const CHEV = '<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>';
/* Listing photos. The copy carries photo ADDRESSES only (vpa/photos.py);
   the phone asks the site's photo server for one when that car is on
   screen, at the size it shows it. Ones already seen are kept by the
   offline worker (sw.js), so they show with no signal too. */
const PH_RE = /^[a-z]{3,20}\/[a-z]{2,20}\/[a-z]{2,20}\/[a-z0-9]{10,64}\.(jpg|jpeg|png|webp)$/;
const photoUrl = (p, w, h) => {
  const ph = (DB && DB.snap.photos) || {};       // the photo server, from the laptop's site file
  if (!p || !ph.server || !PH_RE.test(p)) return null;
  const q = String(ph.size || "").split("{w}").join(w).split("{h}").join(h);
  return "https://" + p.split("/")[0] + "." + ph.server + "/" + p + (q ? "?" + q : "");
};
const thumb = p => { const u = photoUrl(p, 160, 107);
  return u ? `<img class="thumb" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="" src="${u}" onerror="this.className='thumb none'">`
    : '<span class="thumb none"></span>'; };
function gallery(ps, url) {
  ps = (ps || []).filter(p => PH_RE.test(p));
  if (!ps.length) return "";
  const imgs = ps.map((p, i) => `<img ${i ? 'loading="lazy" ' : ""}decoding="async" referrerpolicy="no-referrer" alt="" src="${photoUrl(p, 600, 400)}" onerror="this.style.visibility='hidden'">`).join("");
  return `<div class="gal">${url ? `<a class="gstrip" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${imgs}</a>` : `<div class="gstrip">${imgs}</div>`}
    ${ps.length > 1 ? `<span class="count">1 / ${ps.length}</span>` : ""}</div>`;
}
function toast(msg, ms) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), ms || 2600);
}

/* ------------------------------------------------------------ storage */
const LS = {
  get(k) { try { return localStorage.getItem("vpa." + k); } catch (e) { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem("vpa." + k) : localStorage.setItem("vpa." + k, v); } catch (e) {} },
};
function idb() {
  return new Promise((ok, no) => {
    const r = indexedDB.open("vpa", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("kv");
    r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
  });
}
async function idbGet(k) {
  const db = await idb();
  return new Promise((ok, no) => { const q = db.transaction("kv").objectStore("kv").get(k);
    q.onsuccess = () => ok(q.result); q.onerror = () => no(q.error); });
}
async function idbPut(k, v) {
  const db = await idb();
  return new Promise((ok, no) => { const t = db.transaction("kv", "readwrite"); t.objectStore("kv").put(v, k);
    t.oncomplete = () => ok(); t.onerror = () => no(t.error); });
}

/* ------------------------------------------------------------ the data */
let DB = null;
const TS_LINK = "intent:#Intent;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;" +
  "package=com.tailscale.ipn;S.browser_fallback_url=" +
  encodeURIComponent("https://play.google.com/store/apps/details?id=com.tailscale.ipn") + ";end";
function useSnapshot(snap) { DB = VS.index(snap); DB.snapStars = new Map(DB.stars); applyStarQ(); }

/* ------------------------------------------------------------ stars from this phone
   A tap is a request to the laptop: "star this car" / "unstar it" - a state,
   never a toggle, so a request delivered twice changes nothing. Taps wait in
   STARQ (this phone's storage) until the laptop has applied them, and show at
   once: DB.stars is the copy's stars with the waiting taps laid on top.
     {id, k, on, at, sent, res}  res = what the laptop did: applied | already
                                 | kept (it was starred there after your tap)
                                 | gone (the car left the data)
   A request is let go once the laptop has answered AND the copy shows the
   outcome (or the laptop kept its own state, which the copy already shows). */
const STARQ = {
  load() { try { const q = JSON.parse(LS.get("starq") || "[]"); return Array.isArray(q) ? q : []; } catch (e) { return []; } },
  save(q) { LS.set("starq", q.length ? JSON.stringify(q) : null); },
};
const STAR = { off: null, pending: new Set() };
const snapIso = () => DB ? String(DB.snap.built).replace(" ", "T") : "";
function starSettled(o) {
  if (!o.res) return o.sent && Date.now() - o.at > 60 * 864e5;      // never answered in 60 days
  if (o.res.r === "kept" || o.res.r === "gone") return true;
  return !!DB && (DB.snapStars.has(o.res.key || o.k) === o.on || snapIso() >= String(o.res.decided || "").replace(" ", "T"));
}
function applyStarQ() {
  if (!DB) return;
  let q = STARQ.load();
  const gone = q.filter(starSettled);
  if (gone.length) {
    q = q.filter(o => !gone.includes(o)); STARQ.save(q);
    const kept = gone.filter(o => o.res && o.res.r === "kept").length, lost = gone.filter(o => o.res && o.res.r === "gone").length;
    if (kept || lost) setTimeout(() => toast([kept ? `${kept} unstar${kept > 1 ? "s" : ""} not applied - starred again on the laptop since` : "",
      lost ? `${lost} star${lost > 1 ? "s" : ""} for a car no longer in the data` : ""].filter(Boolean).join(" · "), 5000), 300);
  }
  DB.stars = new Map(DB.snapStars); STAR.pending = new Set();
  for (const o of q) {
    STAR.pending.add(o.k);
    if (o.on) { if (!DB.stars.has(o.k)) DB.stars.set(o.k, { at: new Date(o.at).toISOString(), note: null, mine: true }); }
    else DB.stars.delete(o.k);
  }
}
function tapStar(k) {
  if (!DB) return;
  const on = !DB.stars.has(k), s = DB.stars.get(k);
  if (!on && s && s.note && !confirm(`This star has a note on the laptop:\n\n"${s.note}"\n\nUnstarring removes the note too (the laptop keeps a record of it).`)) return;
  let q = STARQ.load();
  // a tap not yet sent is replaced, or dropped when it would undo itself
  const sentOnly = q.filter(o => o.k === k && o.sent);
  const base = sentOnly.length ? sentOnly[sentOnly.length - 1].on : DB.snapStars.has(k);
  q = q.filter(o => !(o.k === k && !o.sent));
  if (on !== base) q.push({ id: Array.from(crypto.getRandomValues(new Uint8Array(12)), b => (b % 36).toString(36)).join(""),
                            k, on, at: Date.now(), sent: false });
  STARQ.save(q); applyStarQ(); render(true);
  toast(on ? "Starred · goes to the laptop on the next sync" : "Unstarred · goes to the laptop on the next sync");
  if (LS.get("token")) starSync().then(sent => { if (sent) setTimeout(() => sync(false), 12000); });
}
// send waiting taps; ask what became of sent ones. True if any were sent.
async function starSync() {
  const token = LS.get("token");
  let q = STARQ.load();
  if (!token || !q.length) return false;
  const ops = q.filter(o => !o.sent).slice(0, 25), ask = q.filter(o => o.sent && !o.res).map(o => o.id);
  if (!ops.length && !ask.length) { applyStarQ(); return false; }
  try {
    const r = await fetch("/api/stars", { method: "POST", cache: "no-store", signal: withTimeout(8000),
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ ops: ops.map(o => ({ id: o.id, k: o.k, on: o.on, at: o.at })), ask }) });
    const d = await r.json().catch(() => ({}));
    if (r.status === 403 && d.off) { STAR.off = d.error; return false; }
    if (!r.ok || !d.ok) return false;
    STAR.off = null;
    const acc = new Set(d.accepted || []), res = d.results || {};
    q = STARQ.load();                                   // taps made meanwhile are kept
    for (const o of q) { if (acc.has(o.id)) o.sent = true; if (res[o.id]) o.res = res[o.id]; }
    STARQ.save(q); applyStarQ();
    if (location.hash.startsWith("#/starred") || location.hash.startsWith("#/car") || location.hash.startsWith("#/sync")) render(true);
    return acc.size > 0;
  } catch (e) { return false; }                       // no signal: they wait
}

/* ------------------------------------------------------------ sync */
const SYNC = { busy: false, state: LS.get("token") ? "idle" : "unpaired", error: null,
               lastOk: LS.get("lastOk"), lastTry: null };
function withTimeout(ms) { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; }
async function sync(manual) {
  const token = LS.get("token");
  if (!token) { SYNC.state = "unpaired"; pill(); return; }
  if (SYNC.busy) return;
  SYNC.busy = true; SYNC.lastTry = Date.now(); pill();
  const H = { Authorization: "Bearer " + token };
  try {
    const r = await fetch("/api/meta", { headers: H, cache: "no-store", signal: withTimeout(8000) });
    if (r.status === 401) { SYNC.state = "removed"; SYNC.error = null; return; }
    if (r.status === 429) { SYNC.state = "error"; SYNC.error = "the laptop is refusing for ten minutes after too many failed attempts"; return; }
    const meta = await r.json();
    if (!meta.ok) { SYNC.state = "error"; SYNC.error = meta.error; return; }
    await starSync();
    const have = DB && DB.snap.version;
    if (meta.version !== have) {
      SYNC.state = "downloading"; pill();
      const s = await fetch("/api/snapshot", { headers: Object.assign({}, H, have ? { "If-None-Match": `"${have}"` } : {}),
                                               cache: "no-store", signal: withTimeout(120000) });
      if (s.status !== 200) throw new Error("the laptop answered " + s.status);
      const snap = await s.json();
      if (snap.version !== meta.version || snap.counts.vehicles !== meta.counts.vehicles
          || snap.sightings.rows.length !== meta.counts.sightings)
        throw new Error("the copy did not match its description - kept the one you had");
      const before = DB ? DB.snap.counts : null;
      await idbPut("snap", snap);
      useSnapshot(snap);
      LS.set("meta", JSON.stringify(meta));
      if (STARQ.load().some(o => o.sent && !o.res)) starSync();
      const add = before ? snap.counts.sightings - before.sightings : null;
      toast(before ? `Updated: ${n0(snap.counts.vehicles)} cars` + (add > 0 ? ` (+${n0(add)} sightings)` : "")
                   : `Downloaded ${n0(snap.counts.vehicles)} cars`);
      render(true);
    }
    SYNC.state = "ok"; SYNC.error = null; SYNC.lastOk = new Date().toISOString(); LS.set("lastOk", SYNC.lastOk);
    LS.set("device", meta.device || "");
    LS.set("https", meta.https_url || null);
  } catch (e) {
    // no signal at all -> "offline" (normal at a yard, said quietly);
    // signal but no laptop -> "unreachable": Tailscale off, or the laptop asleep
    const net = navigator.onLine === false;
    SYNC.state = net ? "offline" : (e.name === "AbortError" || e instanceof TypeError) ? "unreachable" : "error";
    SYNC.error = e.name === "AbortError" ? "the laptop did not answer in time" : String(e.message || e);
    if (manual) toast(SYNC.state === "error" ? "Sync failed: " + SYNC.error
      : SYNC.state === "unreachable" ? "Can't reach the laptop - is Tailscale on?" : "No signal - using the copy on this phone", 3500);
  } finally {
    SYNC.busy = false; pill();
    if (location.hash.startsWith("#/sync")) render(true);
  }
}
function pill() {
  const p = $("pill"), s = SYNC.state, data = DB ? DB.snap.built : null;
  let cls = "off", txt;
  if (SYNC.busy) { cls = "busy"; txt = SYNC.state === "downloading" ? "Downloading…" : "Syncing…"; }
  else if (s === "unpaired") { cls = "warn"; txt = "Not paired"; }
  else if (s === "removed") { cls = "warn"; txt = "Pair again"; }
  else if (s === "ok") { cls = "ok"; txt = "Synced " + ago(SYNC.lastOk); }
  else if (s === "offline") { cls = "off"; txt = "Offline · " + (data ? dShort(data.slice(0, 10)) : "no data"); }
  else if (s === "unreachable") { cls = "off"; txt = "Not synced · " + (data ? dShort(data.slice(0, 10)) : "no data"); }
  else if (s === "error") { cls = "bad"; txt = "Sync failed"; }
  else txt = data ? "Data " + dShort(data.slice(0, 10)) : "…";
  p.className = "pill " + cls; p.querySelector("span").textContent = txt;
}
let syncTimer = null;
function autoSync() {
  sync(false);
  clearInterval(syncTimer);
  syncTimer = setInterval(() => { if (document.visibilityState === "visible") sync(false); }, 5 * 60 * 1000);
}
// Back in VPA (e.g. from switching Tailscale on): sync now if the last try
// failed - and once more a few seconds later, while the VPN finishes
// connecting. Otherwise at most every 30 s.
let retryT = null;
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  const failed = ["unreachable", "offline", "error"].includes(SYNC.state);
  if (failed || !SYNC.lastTry || Date.now() - SYNC.lastTry > 30000) {
    sync(false);
    clearTimeout(retryT);
    if (failed) retryT = setTimeout(() => { if (SYNC.state !== "ok") sync(false); }, 4000);
  }
});
window.addEventListener("online", () => sync(false));
setInterval(pill, 30000);

/* ------------------------------------------------------------ charts */
const W = 340;
function scale(d0, d1, r0, r1) { const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0); return v => r0 + (v - d0) * k; }
function niceTicks(lo, hi, n) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / (n || 4), mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}
const path = pts => pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
const area = (top, bot) => path(top) + bot.slice().reverse().map(p => "L" + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("") + "Z";
/* x: numeric (day numbers or index); series: [{pts:[[x,y]], cls}], bands:[{pts:[[x,lo,hi]], cls}] */
function chart(o) {
  const H = o.h || 170, L = 42, R = 8, T = 8, B = 20;
  const xs = [], ys = [];
  (o.series || []).forEach(s => s.pts.forEach(p => { xs.push(p[0]); if (p[1] != null) ys.push(p[1]); }));
  (o.bands || []).forEach(b => b.pts.forEach(p => { xs.push(p[0]); ys.push(p[1], p[2]); }));
  (o.dots || []).forEach(d => d.pts.forEach(p => { xs.push(p[0]); ys.push(p[1]); }));
  if (!xs.length) return '<div class="muted" style="font-size:13px">Not enough data to draw.</div>';
  let x0 = Math.min(...xs), x1 = Math.max(...xs); if (x0 === x1) { x0 -= 1; x1 += 1; }
  let y0 = Math.min(...ys), y1 = Math.max(...ys); const pad = (y1 - y0) * .08 || y1 * .1 || 1;
  y0 = Math.max(o.floor0 ? 0 : -Infinity, y0 - pad); y1 += pad;
  const X = scale(x0, x1, L, W - R), Y = scale(y0, y1, H - B, T);
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">`;
  const ticks = niceTicks(y0, y1, 4), step = ticks.length > 1 ? ticks[1] - ticks[0] : 0;
  // as many decimals as the step needs: $12.5k, never two "$13k" labels
  const dec = !step || step % 1000 === 0 ? 0 : step % 100 === 0 ? 1 : 2;
  const yf = o.yfmt || (v => Math.abs(v) >= 1e6 || Math.abs(v) < 1000 ? moneyK(v)
    : (v < 0 ? "-$" : "$") + (Math.abs(v) / 1000).toFixed(dec) + "k");
  for (const t of ticks) s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${L - 5}" y="${Y(t) + 3}" text-anchor="end">${yf(t)}</text>`;
  for (const b of o.bands || []) s += `<path class="${b.cls || "band"}" d="${area(b.pts.map(p => [X(p[0]), Y(p[2])]), b.pts.map(p => [X(p[0]), Y(p[1])]))}"/>`;
  for (const se of o.series || []) {
    const segs = []; let cur = [];
    se.pts.forEach(p => { if (p[1] == null) { if (cur.length) segs.push(cur); cur = []; } else cur.push([X(p[0]), Y(p[1])]); });
    if (cur.length) segs.push(cur);
    segs.forEach(sg => { s += `<path class="${se.cls || "ln"}" d="${path(sg)}"/>`; });
  }
  for (const d of o.dots || []) for (const p of d.pts) s += `<circle class="${d.cls || "dot"}" cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="${d.r || 3}"/>`;
  s += `<line class="ax" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;
  for (const t of o.xticks || []) s += `<text x="${X(t[0]).toFixed(1)}" y="${H - 5}" text-anchor="middle">${esc(t[1])}</text>`;
  return s + "</svg>" + (o.legend ? `<div class="legend">${o.legend}</div>` : "");
}
const dnum = d => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10) || 1) / 86400000;
const mnum = m => +m.slice(0, 4) * 12 + (+m.slice(5, 7) - 1);
function dayTicks(days, n) {
  if (!days.length) return [];
  const step = Math.max(1, Math.ceil(days.length / (n || 4)));
  return days.filter((d, i) => i % step === 0 || i === days.length - 1).map(d => [dnum(d), dShort(d)]);
}
function bars(o) {
  const H = o.h || 120, L = 42, R = 8, T = 8, B = 20, n = o.vals.length;
  const y1 = Math.max(...o.vals.map(v => v.n), 1) * 1.08, Y = scale(0, y1, H - B, T);
  const bw = (W - L - R) / n;
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
  for (const t of niceTicks(0, y1, 3)) s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${L - 5}" y="${Y(t) + 3}" text-anchor="end">${Math.round(t)}</text>`;
  o.vals.forEach((v, i) => { s += `<rect class="${v.cls || "bar"}" x="${(L + i * bw + 1).toFixed(1)}" y="${Y(v.n).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${(H - B - Y(v.n)).toFixed(1)}" rx="1.5"/>`; });
  if (o.curve) s += `<path class="curve" d="${path(o.curve.map(p => [L + p[0] * (W - L - R), Y(p[1])]))}"/>`;
  if (o.mark != null) { const x = L + o.mark * (W - L - R); s += `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" style="stroke:var(--bad);stroke-width:2"/>`; }
  s += `<line class="ax" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;
  (o.xlabels || []).forEach(t => { s += `<text x="${(L + t[0] * (W - L - R)).toFixed(1)}" y="${H - 5}" text-anchor="middle">${esc(t[1])}</text>`; });
  return s + "</svg>";
}
function strip(lo, q25, med, q75, hi, me, fmt) {
  fmt = fmt || moneyK;
  if (lo == null || hi == null || !(hi > lo)) return "";
  const P = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100));
  return `<div class="strip"><div class="rail"></div><div class="iqr" style="left:${P(q25)}%;width:${P(q75) - P(q25)}%"></div>
    <div class="med" style="left:${P(med)}%"></div>${me != null ? `<div class="me" style="left:${P(me)}%"></div>` : ""}</div>
    <div class="strip-l"><span>${fmt(lo)}</span><span>middle half ${fmt(q25)}–${fmt(q75)}</span><span>${fmt(hi)}</span></div>`;
}

/* ------------------------------------------------------------ routing */
function parseHash() {
  const h = location.hash.replace(/^#/, "") || "/";
  const [p, qs] = h.split("?");
  const q = {}; new URLSearchParams(qs || "").forEach((v, k) => { q[k] = v; });
  return { path: p, q };
}
const go = (h, replace) => { if (replace) location.replace(h); else location.hash = h; };
const ROUTES = {};
let lastRoute = null;
function render(keepScroll) {
  const { path: p, q } = parseHash();
  const scr = ROUTES[p] || ROUTES["/"];
  const y = window.scrollY;
  const tab = scr.tab;
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === tab));
  $("back").hidden = !scr.back || history.length < 2;
  let out;
  if (!DB && scr.needsData !== false) out = noData();
  else {
    try { out = scr.fn(q); } catch (e) {
      console.error(e);
      out = `<div class="card"><b>Something went wrong showing this.</b><div class="note">${esc(e.message)}</div></div>`;
    }
  }
  $("title").textContent = scr.titleText || "VPA";
  $("main").innerHTML = out;
  if (scr.after) scr.after(q);
  const same = lastRoute === location.hash;
  window.scrollTo(0, keepScroll && same ? y : 0);
  lastRoute = location.hash;
}
function route(path, opt, fn) { ROUTES[path] = Object.assign({ fn }, opt); }
window.addEventListener("hashchange", () => render(false));
$("back").onclick = () => history.back();
$("pill").onclick = () => go("#/sync");
function setTitle(t) { (ROUTES[parseHash().path] || {}).titleText = t; }

function noData() {
  const paired = !!LS.get("token");
  return `<div class="empty"><b>${paired ? "Getting your data…" : "No data on this phone yet"}</b>
    ${paired ? "Make sure the laptop is on and VPA phone sync is running, then tap the status at the top."
             : "Pair this phone with VPA on your laptop to download a copy of your data."}
    <div style="margin-top:16px"><a class="btn primary" href="#/sync">${paired ? "Sync" : "Pair this phone"}</a></div></div>`;
}

/* ------------------------------------------------------------ shared bits */
function linkBox(id) {
  return `<form class="paste" id="${id}F"><input type="url" id="${id}" inputmode="url" autocomplete="off"
    placeholder="Paste an advert link" aria-label="Advert link"><button>Find</button></form>
    <div id="${id}Msg" class="note" hidden></div>`;
}
function wireLinkBox(id) {
  const f = $(id + "F"), inp = $(id), msg = $(id + "Msg");
  const run = () => {
    const r = VS.findLink(DB, inp.value);
    msg.hidden = false;
    if (!r.ok) { msg.innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
    if (!r.in_data) {
      msg.innerHTML = `That advert${r.found.listing_id ? ` (<b>${esc(r.found.listing_id)}</b>)` : ""} isn't in your data yet.
        <a href="#/check">Price check it</a> by typing its price and kilometres.`;
      return;
    }
    go("#/car?k=" + enc(r.key));
  };
  f.onsubmit = e => { e.preventDefault(); run(); };
  inp.addEventListener("paste", () => setTimeout(run, 0));
}
function selLink(label, text, sub, right) {
  return `<a class="item" href="#/sel?l=${enc(label)}"><div class="main"><div class="t">${esc(text || label)}</div>
    ${sub ? `<div class="s">${sub}</div>` : ""}</div>${right ? `<div class="r">${right}</div>` : ""}${CHEV}</a>`;
}
function carItem(v, extra) {
  const t = v.title || [v.year, v.make, v.model, v.variant].filter(Boolean).join(" ");
  const bits = [v.odometer_km != null ? kmK(v.odometer_km) : null, v.state, v.seller_type, extra].filter(Boolean);
  const own = DB.byKey.get(v.key);
  return `<a class="item car" href="#/car?k=${enc(v.key)}">${thumb(own && own.ph ? own.ph[0] : null)}<div class="main"><div class="t">${DB.stars.has(v.key) ? "★ " : ""}${esc(t)}</div>
    <div class="s">${esc(bits.join(" · "))}</div></div><div class="r"><b>${money(v.price)}</b>
    <span>${v.last_day ? "seen " + dShort(v.last_day) : ""}</span></div>${CHEV}</a>`;
}
function vehicleAsItem(v) {
  const rows = DB.byCar.get(v.k) || [];
  const pr = rows.filter(r => r.p != null), last = pr[pr.length - 1] || rows[rows.length - 1];
  return { key: v.k, title: v.title, year: v.year, make: v.make, model: v.model, variant: v.variant,
           price: last ? last.p : null, odometer_km: last ? last.km : null, state: v.state,
           seller_type: v.seller, last_day: rows.length ? rows[rows.length - 1].d : null };
}
const notes = arr => arr && arr.length ? `<ul class="notes">${arr.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "";

/* ------------------------------------------------------------ Home */
route("/", { tab: "home", titleText: "VPA" }, q => {
  const S = DB.snap, ovAll = S.overview || {}, lvl = q.lv === "trim" ? "trim" : "line", ov = ovAll[lvl] || {};
  const t = ov.totals || {};
  let h = `<div class="card"><div class="spread"><div><div class="big">${n0(S.counts.vehicles)}</div>
    <div class="sub">cars · ${n0(S.counts.sightings)} sightings · ${n0(t.capture_days)} capture days</div></div>
    <div style="text-align:right"><div class="sub">latest capture</div><b>${dShort(S.counts.last_day)}</b></div></div>
    <div class="note">Data as of ${dLong(S.built.slice(0, 10))}, ${S.built.slice(11, 16)}${SYNC.lastOk ? " · checked " + ago(SYNC.lastOk) : ""}</div></div>`;
  h += `<div class="card"><h2>Look up an advert</h2>${linkBox("homeLink")}
    <div class="row" style="margin-top:10px;gap:8px"><a class="btn block" href="#/check">Price check a car</a>
    <a class="btn block" href="#/find">Browse &amp; ranges</a></div></div>`;
  if (ov.latest) {
    const L = ov.latest;
    h += `<div class="card"><h2>Latest capture <small>${dLong(L.day)}</small></h2>
      <div class="sub">${n0(L.rows)} sightings${L.new_vehicles ? `, ${n0(L.new_vehicles)} new cars` : ""}: ${
      (L.models || []).slice(0, 4).map(m => `${esc(m.name)} (${m.n})`).join(", ")}</div></div>`;
  }
  h += `<div class="card"><div class="spread" style="margin-bottom:8px"><h2 style="margin:0">Most tracked</h2>
    <div class="seg" style="width:150px"><button data-lv="line" class="${lvl === "line" ? "on" : ""}">Models</button>
    <button data-lv="trim" class="${lvl === "trim" ? "on" : ""}">Trims</button></div></div><div class="list">`;
  for (const m of ov.models || []) {
    const sp = (m.spark || []).filter(p => p.v != null);
    const ys = sp.map(p => p.v), lo = Math.min(...ys), hi = Math.max(...ys);
    const spark = sp.length > 1 ? `<svg viewBox="0 0 60 22" style="width:60px;height:22px" class="chart"><path class="ln" style="stroke-width:1.6" d="${
      path(sp.map((p, i) => [i / (sp.length - 1) * 58 + 1, hi > lo ? 20 - (p.v - lo) / (hi - lo) * 18 : 11]))}"/></svg>` : "";
    const label = lvl === "trim" ? m.name : `${m.make} ${m.model}`;
    h += `<a class="item" href="#/sel?l=${enc(label)}"><div class="main"><div class="t">${esc(m.name)}</div>
      <div class="s">${n0(m.listings)} sightings · ${m.days} day${m.days === 1 ? "" : "s"}${m.overall != null ? ` · ${pct(m.overall, true)} since first` : ""}</div></div>
      ${spark}<div class="r"><b>${moneyK(m.latest)}</b><span>latest median</span></div>${CHEV}</a>`;
  }
  h += `</div><div class="note">Median asking price on each capture day. A change can be a different mix of cars as much as a moving market.</div></div>`;
  if ((ov.movers || []).length) {
    h += `<div class="card"><h2>Biggest moves <small>last two captures</small></h2><div class="list">`;
    for (const m of ov.movers) h += selLink(m.name, m.name, `${esc(m.basis)} · ${dShort(m.from_day)} → ${dShort(m.to_day)}`,
      `<b class="${m.step < 0 ? "good" : "bad"}">${pct(m.step, true)}</b><span>${moneyK(m.from)} → ${moneyK(m.to)}</span>`);
    h += `</div></div>`;
  }
  if ((ov.thin || []).length) h += `<div class="card flat"><h2>Thin coverage</h2><div class="sub">${
    ov.thin.map(x => `${esc(x.name)} (${x.days} day${x.days === 1 ? "" : "s"})`).join(", ")}${ov.thin_count > ov.thin.length ? ` and ${ov.thin_count - ov.thin.length} more` : ""}</div></div>`;
  return h;
});
ROUTES["/"].after = q => {
  wireLinkBox("homeLink");
  document.querySelectorAll("[data-lv]").forEach(b => b.onclick = () => go("#/?lv=" + b.dataset.lv, true));
};

/* ------------------------------------------------------------ Find */
route("/find", { tab: "find", titleText: "Find" }, q => {
  let h = `<div class="card"><h2>Advert link</h2>${linkBox("findLink")}</div>`;
  h += `<div class="card"><h2>Make, model or trim</h2><input type="search" id="fq" placeholder="e.g. falcon xr6, rs3, hilux"
    value="${esc(q.q || "")}" autocomplete="off"><div class="list" id="fres" style="margin-top:6px"></div></div>`;
  h += `<div class="card"><h2>Browse</h2><div class="list">`;
  for (const mk of DB.snap.picker.makes || [])
    h += `<a class="item" href="#/make?m=${enc(mk.name)}"><div class="main"><div class="t">${esc(mk.name)}</div>
      <div class="s">${n0(mk.vehicles)} cars · ${mk.models.length} model${mk.models.length === 1 ? "" : "s"}</div></div>${CHEV}</a>`;
  h += `</div></div>`;
  h += `<div class="card"><h2>Cars in a range</h2>
    <div class="grid2"><label class="field"><span>Year from</span><input type="number" id="rY0" inputmode="numeric" value="${esc(q.y0 || "")}"></label>
    <label class="field"><span>Year to</span><input type="number" id="rY1" inputmode="numeric" value="${esc(q.y1 || "")}"></label>
    <label class="field"><span>Price from</span><input type="number" id="rP0" inputmode="numeric" value="${esc(q.p0 || "")}"></label>
    <label class="field"><span>Price to</span><input type="number" id="rP1" inputmode="numeric" value="${esc(q.p1 || "")}"></label>
    <label class="field"><span>Kilometres up to</span><input type="number" id="rK" inputmode="numeric" value="${esc(q.k || "")}"></label>
    <label class="field"><span>Words in the title</span><input type="text" id="rW" value="${esc(q.w || "")}" placeholder="e.g. manual"></label></div>
    <label class="field"><span>Model</span><select id="rM"><option value="">Any</option>${
      (DB.snap.picker.makes || []).flatMap(mk => mk.models.map(mo => `${mk.name} ${mo.name}`)).sort()
        .map(l => `<option ${q.m === l ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
    <button class="btn primary block" id="rGo">Show cars</button><div id="rOut"></div></div>`;
  return h;
});
ROUTES["/find"].after = q => {
  wireLinkBox("findLink");
  const all = Object.entries(DB.sel).map(([l, e]) => ({ l, e, low: l.toLowerCase(), n: (e.c && e.c.n) || 0 }));
  const show = () => {
    const words = $("fq").value.toLowerCase().split(/\s+/).filter(Boolean);
    const res = words.length ? all.filter(x => words.every(w => x.low.includes(w)))
      .sort((a, b) => b.n - a.n).slice(0, 25) : [];
    $("fres").innerHTML = words.length && !res.length ? `<div class="item muted">Nothing matches.</div>`
      : res.map(x => selLink(x.l, x.l, `${x.e.lv === "trim" ? "trim" : x.e.lv === "line" ? "model" : "make"} · ${n0(x.e.c.vehicles)} cars`,
        `<b>${moneyK(x.e.c.median)}</b><span>median</span>`)).join("");
  };
  $("fq").oninput = show; show();
  const runRange = () => {
    const y0 = +$("rY0").value || null, y1 = +$("rY1").value || null, p0 = +$("rP0").value || null,
      p1 = +$("rP1").value || null, kmax = +$("rK").value || null, w = $("rW").value.trim().toLowerCase(), m = $("rM").value;
    history.replaceState(null, "", "#/find?" + new URLSearchParams(Object.entries({ y0: y0 || "", y1: y1 || "", p0: p0 || "",
      p1: p1 || "", k: kmax || "", w, m }).filter(x => x[1])).toString());
    const hits = [];
    for (const v of DB.V) {
      if (m && `${v.make} ${v.model}` !== m) continue;
      if (y0 && !(v.year >= y0)) continue;
      if (y1 && !(v.year <= y1)) continue;
      if (w && !String(v.title || "").toLowerCase().includes(w) && !String(v.variant || "").toLowerCase().includes(w)) continue;
      const it = vehicleAsItem(v);
      if (p0 && !(it.price >= p0)) continue;
      if (p1 && !(it.price != null && it.price <= p1)) continue;
      if (kmax && !(it.odometer_km != null && it.odometer_km <= kmax)) continue;
      hits.push(it);
    }
    hits.sort((a, b) => (a.price == null) - (b.price == null) || a.price - b.price);
    const P = hits.map(x => x.price).filter(x => x != null);
    $("rOut").innerHTML = `<div class="note" style="margin:12px 0 4px">${n0(hits.length)} car${hits.length === 1 ? "" : "s"} (each at its latest sighting)${
      P.length ? ` · median ${money(VS.median(P))}, middle half ${moneyK(VS.quantile(P, .25))}–${moneyK(VS.quantile(P, .75))}` : ""}</div>
      <div class="list">${hits.slice(0, 150).map(x => carItem(x)).join("")}</div>${hits.length > 150 ? `<div class="note">Showing the cheapest 150.</div>` : ""}`;
  };
  $("rGo").onclick = runRange;
  if (Object.keys(q).some(k => ["y0", "y1", "p0", "p1", "k", "w", "m"].includes(k))) runRange();
};
route("/make", { tab: "find", back: true }, q => {
  const mk = (DB.snap.picker.makes || []).find(m => m.name === q.m);
  if (!mk) return `<div class="empty">Not in your data.</div>`;
  setTitle(mk.name);
  let h = `<div class="card"><div class="list">${selLink(mk.name, "All " + mk.name, `${n0(mk.vehicles)} cars`, "")}</div></div>`;
  for (const mo of mk.models) {
    h += `<div class="card"><h2>${esc(mo.name)} <small>${n0(mo.vehicles)} cars · median ${moneyK(mo.median)}</small></h2><div class="list">`;
    h += selLink(`${mk.name} ${mo.name}`, "All trims", `${n0(mo.n)} sightings · last ${dShort(mo.last)}`, `<b>${moneyK(mo.median)}</b>`);
    for (const va of mo.variants.slice(0, 60))
      h += selLink(`${mk.name} ${mo.name} ${va.name}`, va.name, `${n0(va.vehicles)} cars · ${km(va.odo)}`, `<b>${moneyK(va.median)}</b><span>${moneyK(va.lo)}–${moneyK(va.hi)}</span>`);
    if (mo.variants.length > 60) h += `<div class="item muted">${mo.variants.length - 60} more trims - search for them on Find</div>`;
    h += `</div></div>`;
  }
  return h;
});

/* ------------------------------------------------------------ a selection */
// a label saved before trims were regrouped -> the label it is now
// (phonebuild._aliases); a current label is itself
const selLabel = l => DB.sel[l] ? l : (DB.snap.alias && DB.snap.alias[l] && DB.sel[DB.snap.alias[l]] ? DB.snap.alias[l] : l);
route("/sel", { tab: "find", back: true }, q => {
  const label = selLabel(q.l), e = DB.sel[label];
  if (!e) return `<div class="empty"><b>${esc(label)}</b>isn't in your data.</div>`;
  setTitle(label);
  const f = { year_min: q.y0, year_max: q.y1, price_min: q.p0, price_max: q.p1, keyword: q.kw };
  const all = VS.rowsOf(DB, e.mk, e.mo, e.va), fr = VS.applyFilters(all, f), rows = fr.rows, F = fr.applied.length > 0;
  const d = e.d, lineE = e.va ? DB.sel[`${e.mk} ${e.mo}`] : null;
  let h = "";
  if (d.empty || !rows.length) return filterCard(q, fr, F) + `<div class="empty">No sightings match${F ? " these filters" : ""}.</div>`;
  const H = F ? VS.head(rows, label, e.lv) : d.head;
  h += `<div class="card"><div class="sub">${e.lv === "trim" ? "trim" : e.lv === "line" ? "model, all trims" : "make, all models"}${
    F ? " · filtered" : ""}</div><h1>${esc(label)}</h1>
    <div class="spread" style="margin-top:6px"><div><div class="big">${money(H.median)}</div><div class="sub">median asking price</div></div>
    <div style="text-align:right"><b class="num">${moneyK(H.q25)}–${moneyK(H.q75)}</b><div class="sub">middle half</div></div></div>
    <div class="grid3" style="margin-top:12px"><div class="stat"><b>${n0(H.vehicles)}</b><span>cars</span></div>
    <div class="stat"><b>${n0(H.observations)}</b><span>sightings</span></div>
    <div class="stat"><b>${kmK(H.median_odo)}</b><span>median km</span></div>
    <div class="stat"><b>${moneyK(H.lo)}</b><span>lowest</span></div><div class="stat"><b>${moneyK(H.hi)}</b><span>highest</span></div>
    <div class="stat"><b>${H.days}</b><span>capture days</span></div></div>
    <div class="note">${dLong(H.first_day)} to ${dLong(H.last_day)}${H.pct_no_odo ? ` · ${H.pct_no_odo}% without odometer` : ""}${
      H.brief ? ` · ${H.brief} price-only` : ""}${H.collisions ? ` · ${H.collisions} car(s) with two prices on one day, not counted as price changes` : ""}</div>
    ${e.va ? `<a class="btn block" style="margin-top:12px" href="#/check?l=${enc(label)}">Price check a car against this trim</a>` : ""}</div>`;
  h += filterCard(q, fr, F);

  // ---- trend + forecast (whole selection only: the laptop computes these)
  if (!F) {
    const fc = d.forecast || {}, bm = d.bymonth || [], adj = d.adjusted || [], pts = fc.points || [];
    const months = [...new Set(bm.map(b => b.d).concat(pts.map(p => p.d)))].sort();
    const last = bm[bm.length - 1];
    const fcLine = last && pts.length ? [[mnum(last.d), last.median]].concat(pts.map(p => [mnum(p.d), p.v])) : [];
    const fcBand = last && pts.length ? [[mnum(last.d), last.median, last.median]].concat(pts.map(p => [mnum(p.d), p.lo, p.hi])) : [];
    const dir = fc.direction === "up" ? '<span class="tag bad">rising</span>' : fc.direction === "down" ? '<span class="tag good">falling</span>'
      : fc.direction === "flat" ? '<span class="tag">flat</span>' : "";
    h += `<div class="card"><h2>Price trend &amp; forecast</h2>${chart({
      series: [{ pts: bm.map(b => [mnum(b.d), b.median]) }, ...(adj.length ? [{ pts: adj.map(a => [mnum(a.d), a.median]), cls: "ln2" }] : []),
               { pts: fcLine, cls: "fc" }],
      bands: [{ pts: bm.map(b => [mnum(b.d), b.q25, b.q75]) }, { pts: fcBand, cls: "band2" }],
      dots: [{ pts: bm.map(b => [mnum(b.d), b.median]) }],
      xticks: months.map(m => [mnum(m), mShort(m)]).filter((t, i, a) => a.length <= 6 || i % Math.ceil(a.length / 6) === 0),
      legend: `<span><i></i>monthly median</span><span><i class="b"></i>middle half</span>${pts.length ? '<span><i class="d"></i>forecast, 80% range</span>' : ""}${adj.length ? '<span><i class="g"></i>at median km</span>' : ""}`})}
      ${pts.length ? `<table class="tbl" style="margin-top:10px"><tr><th>month</th><th>forecast</th><th>80% range</th></tr>${
        pts.map(p => `<tr><td>${mShort(p.d)}</td><td><b>${money(p.v)}</b></td><td>${moneyK(p.lo)}–${moneyK(p.hi)}</td></tr>`).join("")}</table>` : ""}
      <div class="note">${dir} ${esc(fc.verdict || "")}${fc.method ? ` <span class="faint">(${esc(fc.method.replace("_", " "))}, n=${fc.n_total})</span>` : ""}</div></div>`;
  }

  // ---- capture days
  const bd = F ? VS.byday(rows) : d.byday, sup = F ? VS.supply(bd) : d.supply;
  const days = bd.map(b => b.d);
  h += `<div class="card"><h2>Capture days</h2>${chart({ h: 150,
    series: [{ pts: bd.map(b => [dnum(b.d), b.median]) }], bands: [{ pts: bd.map(b => [dnum(b.d), b.q25, b.q75]) }],
    dots: [{ pts: bd.map(b => [dnum(b.d), b.median]), r: 2.6 }], xticks: dayTicks(days, 4),
    legend: '<span><i></i>median that day</span><span><i class="b"></i>middle half</span>' })}
    ${bars({ h: 90, vals: bd.map(b => ({ n: b.vehicles })), xlabels: [] })}
    <div class="note" style="margin-top:2px">Cars seen on each capture day.</div>
    <div class="grid3" style="margin-top:10px"><div class="stat"><b>${n0(sup.now)}</b><span>latest capture</span></div>
    <div class="stat"><b>${sup.avg == null ? "–" : sup.avg}</b><span>average</span></div>
    <div class="stat"><b>${sup.change == null ? "–" : (sup.change > 0 ? "+" : "") + sup.change}</b><span>vs previous</span></div></div>
    <div class="note">A smaller count can mean fewer cars or a smaller capture.</div></div>`;

  // ---- distribution
  const pool = VS.latestPerVehicle(VS.prep(rows));
  const hs = F ? VS.hist(pool.map(r => r.p)) : d.hist;
  if (hs) {
    const lo = hs.bins[0].lo, hi = hs.bins[hs.bins.length - 1].hi, bw = hs.bins[0].hi - hs.bins[0].lo;
    const grid = !F ? VS.curveGrid(d.curve) : null;
    const curve = grid ? grid.x.map((x, i) => [(x - lo) / (hi - lo), grid.y[i] * hs.n * bw]).filter(p => p[0] >= 0 && p[0] <= 1) : null;
    const st = !F ? VS.standouts(DB, rows, d) : null;
    h += `<div class="card"><h2>Price spread <small>one price per car</small></h2>${bars({ vals: hs.bins, curve,
      mark: (hs.median - lo) / (hi - lo), xlabels: [[0, moneyK(lo)], [0.5, moneyK((lo + hi) / 2)], [1, moneyK(hi)]] })}
      <div class="legend"><span><i class="g" style="height:9px;opacity:.6;background:var(--accent)"></i>cars per price band</span>${curve ? '<span><i style="background:var(--bad)"></i>bell curve</span>' : ""}<span><i style="background:var(--bad);width:3px;height:10px"></i>median ${moneyK(hs.median)}</span></div>`;
    if (st && st.rows.length) {
      const cheap = st.rows.filter(s => s.z != null).slice(0, 4), dear = st.rows.filter(s => s.z != null).slice(-4).reverse();
      const it = s => `<a class="item" href="#/car?k=${enc(s.key)}"><div class="main"><div class="t">${esc(s.title || [s.year, s.variant].filter(Boolean).join(" "))}</div>
        <div class="s">${esc([s.odometer_km != null ? kmK(s.odometer_km) : null, s.state, s.live ? "in latest capture" : "seen " + dShort(s.day), s.dup ? "possible duplicate" : null].filter(Boolean).join(" · "))}</div></div>
        <div class="r"><b>${money(s.price)}</b><span>${s.z > 0 ? "+" : ""}${s.z} sd${s.z_adj != null ? ` · ${s.z_adj > 0 ? "+" : ""}${s.z_adj} at its km` : ""}</span></div>${CHEV}</a>`;
      h += `<h2 style="margin-top:14px">Cheapest ${e.lv === "trim" ? "for the trim" : "here"} <small>furthest below the middle</small></h2><div class="list">${cheap.map(it).join("")}</div>
        <h2 style="margin-top:14px">Dearest</h2><div class="list">${dear.map(it).join("")}</div>`;
      const ds = d.distribution || {};
      h += `<div class="note">${n0(ds.pool_n)} cars, each at its latest sighting; ${n0(ds.live_n)} were in the latest capture (${dShort(ds.live_day)}).${
        ds.dup_extra ? ` About ${ds.dup_extra} look like the same car stored twice.` : ""}</div>`;
    }
    h += `</div>`;
  }

  // ---- price vs kilometres
  const sc = VS.scatter(rows, DB.consts.MAX_SCATTER || 1200), M = d.mileage || {};
  if (sc.points.length >= 3) {
    const xs = sc.points.map(p => p.odo), xl = Math.min(...xs), xh = Math.max(...xs);
    const fit = !F && M.usable && M.slope_per_km != null ? Array.from({ length: 30 }, (_, i) => {
      const x = xl + (xh - xl) * i / 29; return [x, M.median_price * Math.exp(M.slope_per_km * (x - M.median_odo))]; }) : [];
    h += `<div class="card"><h2>Price vs kilometres</h2>${chart({ h: 200,
      series: fit.length ? [{ pts: fit, cls: "curve" }] : [], dots: [{ pts: sc.points.map(p => [p.odo, p.price]), cls: "pt", r: 2.4 }],
      xticks: VS.quantile(xs, 0) != null ? [[xl, kmK(xl)], [(xl + xh) / 2, kmK((xl + xh) / 2)], [xh, kmK(xh)]] : [] })}
      <div class="note">${!F && M.usable ? `Each 10,000 km takes about <b>${pct(Math.abs(M.pct_per_10k_km))}</b> off the price (fitted on ${n0(M.n)} listings, centred on ${km(M.median_odo)}).`
        : !F ? `No mileage fit: ${esc(M.reason || "not enough data")}.` : "The fitted curve is for the whole selection; clear the filters to see it."}
        ${sc.total > sc.points.length ? ` Showing the latest ${n0(sc.points.length)} of ${n0(sc.total)}.` : ""}</div></div>`;
  }

  // ---- who / where / which year
  const sp = F ? VS.splits(rows) : d.splits;
  const spTbl = (title, arr) => arr && arr.length ? `<h2 style="margin-top:12px">${title}</h2><table class="tbl"><tr><th></th><th>listings</th><th>median</th><th>km</th></tr>${
    arr.map(s => `<tr><td>${esc(s.name)}</td><td>${n0(s.n)}</td><td><b>${moneyK(s.median)}</b></td><td>${kmK(s.odo)}</td></tr>`).join("")}</table>` : "";
  h += `<details class="card"><summary><h2>Who, where, which year</h2></summary>${spTbl("Seller", sp.seller)}${spTbl("State", sp.state)}${
    spTbl("Model year", sp.year)}${sp.source && sp.source.length > 1 ? spTbl("Source", sp.source) : ""}</details>`;

  // ---- trims
  const sib = e.lv === "line" ? d.siblings : lineE ? lineE.d.siblings : null;
  if (sib && sib.length > 1) {
    const showAll = q.trims === "all", cap = showAll ? sib.length : 20;
    h += `<details class="card" id="trimsCard" ${e.lv === "line" && (sib.length <= 12 || showAll) ? "open" : ""}><summary><h2>Trims of the ${esc(e.mk + " " + e.mo)} <small>${sib.length}</small></h2></summary>
      <div class="hscroll"><table class="tbl"><tr><th>trim</th><th>cars</th><th>median</th><th>range</th><th>now</th></tr>${
      sib.slice(0, cap).map(s => `<tr data-href="#/sel?l=${enc(`${e.mk} ${e.mo} ${s.variant}`)}" class="${s.variant === e.va ? "sel" : ""}">
        <td>${esc(s.variant)}</td><td>${n0(s.vehicles)}</td><td><b>${moneyK(s.median)}</b></td><td>${moneyK(s.lo)}–${moneyK(s.hi)}</td><td>${n0(s.now)}</td></tr>`).join("")}</table></div>
      ${sib.length > cap ? `<button class="more" id="allTrims">Show all ${sib.length} trims</button>` : ""}
      <div class="note">Most-seen first. "now" = cars in that trim's own latest capture. Tap a trim to open it.</div></details>`;
  }

  // ---- turnover (whole selection only)
  const ch = d.churn;
  if (!F && ch) {
    const tc = (e.lv === "line" ? d.trim_churn : lineE ? lineE.d.trim_churn : null) || {};
    h += `<details class="card"><summary><h2>How fast it sells</h2></summary>${ch.usable ? `<div class="grid3">
      <div class="stat"><b>${ch.rate30 == null ? "–" : Math.round(ch.rate30) + "%"}</b><span>leave / 30 days</span></div>
      <div class="stat"><b>${ch.half_life == null ? "–" : Math.round(ch.half_life) + " d"}</b><span>half gone</span></div>
      <div class="stat"><b>${ch.median_days_listed == null ? "–" : Math.round(ch.median_days_listed) + " d"}</b><span>typical listing</span></div>
      <div class="stat"><b>${n0(ch.exits)}</b><span>left view</span></div><div class="stat"><b>${n0(ch.arrivals)}</b><span>arrived</span></div>
      <div class="stat"><b>${n0(ch.still)}</b><span>still up</span></div></div>
      <p class="verdict">${esc(ch.verdict)}</p>` : `<p class="verdict">${esc(ch.reason || ch.verdict || "Not measurable here.")}</p>`}
      ${(tc.rows || []).filter(r => r.usable).length ? `<h2 style="margin-top:12px">By trim</h2><div class="hscroll"><table class="tbl"><tr><th>trim</th><th>leave/30d</th><th>half gone</th><th>listed</th></tr>${
        tc.rows.filter(r => r.usable).slice(0, 30).map(r => `<tr class="${r.variant === e.va ? "sel" : ""}"><td>${esc(r.variant)}</td><td>${Math.round(r.rate30)}%</td><td>${r.half_life == null ? "–" : Math.round(r.half_life) + " d"}</td><td>${r.median_days_listed == null ? "–" : Math.round(r.median_days_listed) + " d"}</td></tr>`).join("")}</table></div>` : ""}
      ${notes(ch.notes)}<div class="note">Only capture days are observed, so "left view" means not seen again - not necessarily sold.</div></details>`;
  }

  // ---- price changes + the cars
  const C = VS.cars(DB, rows);
  if (C.movers.length) {
    h += `<details class="card"><summary><h2>Price changes <small>${C.movers.length} cars</small></h2></summary><div class="list">${
      C.movers.slice(0, 25).map(m => `<a class="item" href="#/car?k=${enc(m.key)}"><div class="main"><div class="t">${esc(m.title || [m.year, m.variant].filter(Boolean).join(" "))}</div>
        <div class="s">${dShort(m.first_day)} → ${dShort(m.last_day)} · ${m.steps.length} prices</div></div>
        <div class="r"><b class="${m.change < 0 ? "good" : "bad"}">${pct(m.change, true)}</b><span>${moneyK(m.first_price)} → ${moneyK(m.price)}</span></div>${CHEV}</a>`).join("")}</div></details>`;
  }
  const sort = q.s || "new";
  const sorted = sort === "new" ? C.vehicles : C.vehicles.slice().sort(
    sort === "cheap" ? (a, b) => (a.price == null) - (b.price == null) || a.price - b.price
    : sort === "dear" ? (a, b) => (b.price || 0) - (a.price || 0)
    : (a, b) => (a.odometer_km == null) - (b.odometer_km == null) || a.odometer_km - b.odometer_km);
  const lim = +q.n || 40;
  h += `<div class="card" id="carsCard"><h2>The cars <small>${n0(C.vehicles.length)}</small></h2>
    <div class="seg" style="margin-bottom:6px">${[["new", "Newest"], ["cheap", "Cheapest"], ["dear", "Dearest"], ["km", "Lowest km"]].map(
      ([k, t]) => `<button data-sort="${k}" class="${sort === k ? "on" : ""}">${t}</button>`).join("")}</div>
    <div class="list">${sorted.slice(0, lim).map(v => carItem(v, v.moved ? (v.change < 0 ? "dropped " : "rose ") + pct(Math.abs(v.change)) : v.sightings > 1 ? v.sightings + " sightings" : "")).join("")}</div>
    ${sorted.length > lim ? `<button class="more" id="moreCars">Show ${Math.min(80, sorted.length - lim)} more</button>` : ""}</div>`;
  if (!F && (d.caveats || []).length) h += `<div class="card flat"><h2>Read with care</h2>${notes(d.caveats)}</div>`;
  return h;
});
function filterCard(q, fr, F) {
  return `<details class="card" ${F ? "open" : ""}><summary><h2>Filter${F ? ` <small>${fr.applied.map(a => esc(a.text)).join(" · ")}</small>` : ""}</h2></summary>
    <div class="grid2"><label class="field"><span>Year from</span><input type="number" inputmode="numeric" id="fy0" value="${esc(q.y0 || "")}"></label>
    <label class="field"><span>Year to</span><input type="number" inputmode="numeric" id="fy1" value="${esc(q.y1 || "")}"></label>
    <label class="field"><span>Price from</span><input type="number" inputmode="numeric" id="fp0" value="${esc(q.p0 || "")}"></label>
    <label class="field"><span>Price to</span><input type="number" inputmode="numeric" id="fp1" value="${esc(q.p1 || "")}"></label></div>
    <label class="field"><span>Word in the trim or title</span><input type="text" id="fkw" value="${esc(q.kw || "")}" placeholder="e.g. manual, sedan"></label>
    <div class="row"><button class="btn primary" id="fGo" style="flex:1">Apply</button>${F ? '<button class="btn" id="fClear">Clear</button>' : ""}</div>
    ${F ? '<div class="note">Filtered: counts and prices are recomputed on this phone. Forecast, bell curve, mileage fit and turnover are for the whole selection, so they are hidden.</div>' : ""}</details>`;
}
ROUTES["/sel"].after = q => {
  const base = { l: q.l };
  const nav = extra => go("#/sel?" + new URLSearchParams(Object.entries(Object.assign({}, base, extra)).filter(x => x[1] != null && x[1] !== "")).toString(), true);
  const cur = { y0: q.y0, y1: q.y1, p0: q.p0, p1: q.p1, kw: q.kw };
  if ($("fGo")) $("fGo").onclick = () => nav({ y0: $("fy0").value, y1: $("fy1").value, p0: $("fp0").value, p1: $("fp1").value, kw: $("fkw").value.trim() });
  if ($("fClear")) $("fClear").onclick = () => nav({});
  document.querySelectorAll("[data-sort]").forEach(b => b.onclick = () => {
    nav(Object.assign({}, cur, { s: b.dataset.sort }));
    setTimeout(() => $("carsCard") && $("carsCard").scrollIntoView(), 30);
  });
  if ($("allTrims")) $("allTrims").onclick = () => { nav(Object.assign({}, cur, { s: q.s, trims: "all" }));
    setTimeout(() => $("trimsCard") && $("trimsCard").scrollIntoView(), 30); };
  if ($("moreCars")) $("moreCars").onclick = () => {
    const y = window.scrollY; nav(Object.assign({}, cur, { s: q.s, n: (+q.n || 40) + 80 })); setTimeout(() => window.scrollTo(0, y), 30); };
  document.querySelectorAll("tr[data-href]").forEach(tr => tr.onclick = () => go(tr.dataset.href));
};

/* ------------------------------------------------------------ one car */
// the expected asking price for typed details (stats.js estimate). `big`:
// it IS the answer (no price typed); otherwise one line in the verdict card.
const EST_BASIS = { mileage: "the trim's mileage curve at that odometer, widened by how far real ads sit from it",
  nearest: "the cars of this trim nearest in kilometres", trim: "every car of this trim (no kilometres given)" };
const EST_BASIS_YEARS = { nearest: "these cars nearest in kilometres", trim: "all of these cars (no kilometres given)" };
function estHtml(e, S, big, yrs) {
  if (!e) return "";
  if (!e.usable) return big ? `<div class="card"><h2>Expected asking price</h2><p class="sub">No range: ${esc(e.reason)}</p></div>` : "";
  if (!big) return `<div class="note">Expected for these details: <b>${money(e.lo)} – ${money(e.hi)}</b> (typical ${money(e.mid)}).</div>`;
  const what = [S.odometer_km != null ? km(S.odometer_km) : null, S.year, S.seller_type, S.state].filter(Boolean).join(" · ");
  return `<div class="card"><h2>Expected asking price${what ? ` <small>${esc(what)}</small>` : ""}</h2>
    <div class="estrng">${money(e.lo)} <span>–</span> ${money(e.hi)}</div>
    <div class="sub">typical <b>${money(e.mid)}</b> · most ads ${money(e.wide_lo)}–${money(e.wide_hi)}</div>
    <div class="note">The middle half of what cars like this ask, from ${esc((yrs ? EST_BASIS_YEARS : EST_BASIS)[e.basis] || e.basis)} (${n0(e.n)} ${e.basis === "mileage" ? "sightings" : "cars"}).
      ${e.why_not_curve ? esc(e.why_not_curve) + ". " : ""}Asking prices, not sale prices; the trim pools model years - see "Year" under Like for like.</div></div>`;
}
function cardHtml(c, v) {
  const S = c.subject, C = DB.consts;
  if (!c.ok) return `<div class="card"><b class="bad">${esc(c.error)}</b></div>`;
  if (c.usable === false && !c.raw) return `<div class="card">${esc(c.reason)}</div>`;
  // not against one trim: said first (explore._fallback_note)
  let h = c.fallback ? `<div class="card"><span class="warn">${esc(c.fallback.note)}</span></div>` : "";
  const R = c.raw, A = c.adjusted, X = c.expected, est = S && !S.stored && S.price == null;
  const yrs = !!c.fallback;        // a wider pool than the trim
  if (est) h += estHtml(c.estimate, S, true, yrs);
  else {
  // the laptop's wording: a percentile only from 20 peers up, a plain count below
  const rankTxt = (r, what) => !r.usable ? `<span class="muted">${esc(r.reason)}</span>`
    : r.percentile != null ? `Dearer than <b>${pyPct(r.percentile)}</b> of ${n0(r.n)} other ${what} ${
        r.percentile < 25 ? '<span class="tag good">cheap end</span>' : r.percentile > 75 ? '<span class="tag bad">dear end</span>' : '<span class="tag">mid-pack</span>'}`
    : `<b>${ordinal(r.rank)}</b> cheapest of ${r.n + 1}`;
  h += `<div class="card"><h2>Against ${esc(c.label)}</h2>
    <div class="verdict">${rankTxt(R, yrs ? "of these cars" : "cars of this trim")}</div>
    ${strip(c.pool.lo, c.pool.q25, c.pool.median, c.pool.q75, c.pool.hi, S.price)}
    <div class="note">${yrs ? "Median" : "Trim median"} ${money(c.pool.median)} over ${n0(c.pool.n)} cars, ${dShort(c.pool.first)} to ${dShort(c.pool.last)}${c.pool.stale ? ` · <span class="warn">latest capture was ${c.pool.days_since} days ago</span>` : ""}.</div>`;
  if (X) {
    h += `<div class="spread" style="margin-top:14px"><div><div class="sub">expected for ${km(S.odometer_km)}</div><div class="big" style="font-size:24px">${money(X.price)}</div></div>
      <div style="text-align:right"><b class="${X.gap < 0 ? "good" : X.gap > 0 ? "bad" : ""}" style="font-size:18px">${X.gap > 0 ? "+" : ""}${money(X.gap)}</b>
      <div class="sub">${pct(X.gap_pct, true)} vs expected</div></div></div>
      <div class="note">${X.above} of ${X.of} listings sit further above their own expected price than this one.</div>`;
  }
  if (A) h += `<div style="margin-top:12px" class="verdict">At the trim's median ${km(A.at_odo)}: ${rankTxt(A.rank, "cars")}</div>
    ${strip(A.pool_lo, A.pool_q25, A.pool_median, A.pool_q75, A.pool_hi, A.price)}`;
  if (c.odo_percentile != null) h += `<div class="note">Its kilometres are higher than ${pyPct(c.odo_percentile)} of the trim's cars.</div>`;
  const lc = c.line_check;
  if (lc && lc.rank && lc.rank.usable) h += `<div class="note">Across every ${esc(lc.label)}: ${lc.rank.percentile != null ? `dearer than ${pyPct(lc.rank.percentile)}` : `${ordinal(lc.rank.rank)} cheapest of ${lc.rank.n + 1}`} (median ${money(lc.median)}).</div>`;
  if (S && !S.stored) h += estHtml(c.estimate, S, false, yrs);
  h += `</div>`;
  }
  if (c.comparables && c.comparables.length) {
    h += `<div class="card"><h2>Nearest ads <small>by ${esc(c.band.basis)}</small></h2><div class="list">${c.comparables.map(x =>
      carItem({ key: x.key, title: x.title, year: x.year, price: x.price, odometer_km: x.odometer_km, state: x.state,
                seller_type: x.seller_type, last_day: x.day }, x.dup ? "possible duplicate" : "")).join("")}</div>
      ${c.band.median ? `<div class="note">Their median is ${money(c.band.median)}${c.band.vs != null ? `; this car is <b>${pct(c.band.vs, true)}</b> against it` : ""}.</div>` : ""}</div>`;
  }
  const sp = c.splits, spRow = (t, s) => `<tr><td>${t}${s.value ? ` <span class="muted">(${esc(s.value)})</span>` : ""}</td><td>${s.median != null ? `<b>${money(s.median)}</b>` : `<span class="muted" style="white-space:normal">${esc(s.reason)}</span>`}</td><td>${s.n || ""}</td></tr>`;
  h += `<div class="card"><h2>Like for like</h2><table class="tbl"><tr><th>same</th><th>median</th><th>n</th></tr>${spRow("Seller", sp.seller)}${spRow("State", sp.state)}${spRow("Year", sp.year)}${sp.gearbox && sp.gearbox.value ? spRow("Gearbox", sp.gearbox) : ""}</table></div>`;
  const fit = c.fit;
  h += `<div class="card flat"><h2>How this was worked out</h2><ul class="notes">${fit.usable ? `<li>Mileage: about ${pct(Math.abs(fit.pct_per_10k_km))} per 10,000 km, fitted on ${n0(fit.n)} other listings${fit.covers ? `, valid ${kmK(fit.covers[0])}–${kmK(fit.covers[1])}` : ""}.</li>` : ""}${
    (c.unavailable || []).map(u => `<li>${esc(u)}</li>`).join("")}${(c.caveats || []).map(u => `<li>${esc(u)}</li>`).join("")}
    <li>${esc(c.basis)}.</li><li>${esc(c.footer)}</li></ul></div>`;
  return h;
}
const pyPct = p => (+p.toFixed(1)) + "%";
function historyHtml(c) {
  const Hh = c.history;
  if (!Hh) return "";
  const bd = Hh.byday || [];
  let h = `<div class="card"><h2>This car over time</h2>`;
  if (bd.length > 1) h += chart({ h: 170, series: [{ pts: bd.map(b => [dnum(b.d), b.median != null ? b.median : null]) }],
    bands: [{ pts: bd.filter(b => b.q25 != null).map(b => [dnum(b.d), b.q25, b.q75]) }],
    dots: [{ pts: bd.filter(b => b.mine != null).map(b => [dnum(b.d), b.mine]), cls: "me", r: 4.2 }],
    xticks: dayTicks(bd.map(b => b.d), 4),
    legend: '<span><i class="r"></i>this car</span><span><i></i>trim median</span><span><i class="b"></i>middle half</span>' });
  h += `<div class="note">Seen on ${Hh.days} capture day${Hh.days === 1 ? "" : "s"}, ${dShort(Hh.first_day)} to ${dShort(Hh.last_day)} - listed at least ${Hh.at_least_days} days.${
    Hh.moved ? ` Price ${Hh.change < 0 ? "dropped" : "rose"} ${pct(Math.abs(Hh.change))}.` : ""}${Hh.clash ? ' <span class="warn">Two prices on one day: this may be two cars stored as one.</span>' : ""}</div>
    <table class="tbl" style="margin-top:8px"><tr><th>day</th><th>price</th><th>km</th><th>rank</th></tr>${
    (Hh.sights || []).slice().reverse().map(s => { const b = bd.find(x => x.d === s.d) || {};
      return `<tr><td>${dShort(s.d)}</td><td><b>${money(s.price)}</b></td><td>${s.odometer_km != null ? kmK(s.odometer_km) : "–"}</td><td>${b.rank ? `${b.rank} of ${b.of}` : "–"}</td></tr>`; }).join("")}</table></div>`;
  return h;
}
document.addEventListener("scroll", e => {
  const s = e.target;
  if (!(s && s.classList && s.classList.contains("gstrip"))) return;
  const c = s.parentNode.querySelector(".count");
  if (c) c.textContent = (Math.round(s.scrollLeft / s.clientWidth) + 1) + " / " + s.children.length;
}, true);
route("/car", { tab: "find", back: true }, q => {
  const v = DB.byKey.get(q.k);
  if (!v) return `<div class="empty"><b>Not in your data</b>That car isn't in the copy on this phone.</div>`;
  const t = v.title || [v.year, v.make, v.model, v.variant].filter(Boolean).join(" ");
  setTitle(t);
  const it = vehicleAsItem(v), star = DB.stars.get(v.k), wait = STAR.pending.has(v.k);
  let h = `<div class="card${v.ph && v.ph.length ? " hasgal" : ""}">${gallery(v.ph, v.url)}${star ? `<span class="tag warn">★ starred${star.note ? ": " + esc(star.note) : ""}</span>` : ""}${wait ? ` <span class="tag">waiting to sync</span>` : ""}
    <h1>${esc(t)}</h1><div class="sub">${esc([v.make, v.model, v.variant].filter(Boolean).join(" · "))}</div>
    <div class="spread" style="margin-top:10px"><div class="big">${money(it.price)}</div><div style="text-align:right"><b>${km(it.odometer_km)}</b><div class="sub">${esc([v.year, v.state, v.seller].filter(Boolean).join(" · "))}</div></div></div>
    <div class="note">Last seen ${dLong(it.last_day)}${v.id ? ` · ${esc(v.id)}` : ""}</div>
    <div class="row" style="margin-top:12px;gap:8px"><button class="btn starbtn${star ? " on" : ""}" id="starBtn" aria-pressed="${!!star}" aria-label="${star ? "Unstar" : "Star"} this car">${star ? "★" : "☆"}</button>
    ${v.url ? `<a class="btn block primary" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">Open advert</a>` : ""}
    ${v.variant ? `<a class="btn block" href="#/sel?l=${enc(VS.labelOf(v.make, v.model, v.variant))}">The trim</a>` : `<a class="btn block" href="#/sel?l=${enc(VS.labelOf(v.make, v.model))}">The model</a>`}</div></div>`;
  const noTrim = !v.variant || !String(v.variant).trim();
  if (noTrim && v.year == null) {
    return h + `<div class="card"><div class="verdict">This car was captured without a trim or a year, so it can't be ranked - pooling every trim of every year would compare different cars.</div></div>`;
  }
  const c = VS.carCard(DB, v.make, v.model, noTrim ? null : v.variant, { key: v.k });
  return h + cardHtml(c, v) + (c.ok ? historyHtml(c) : "");
});

/* ------------------------------------------------------------ price check */
ROUTES["/car"].after = q => { if ($("starBtn")) $("starBtn").onclick = () => tapStar(q.k); };

route("/check", { tab: "check", titleText: "Price check" }, q => {
  const trims = Object.entries(DB.sel).filter(([, e]) => e.lv === "trim").map(([l, e]) => [l, e.c.vehicles || 0])
    .sort((a, b) => b[1] - a[1]);
  let h = `<div class="card"><h2>A car you are looking at</h2>
    <label class="field"><span>Trim</span><input type="search" id="cT" value="${esc(q.l || "")}" placeholder="Start typing, e.g. Falcon XR6" autocomplete="off"></label>
    <div class="list" id="cTs" style="margin:-4px -14px 8px"></div>
    <div class="grid2"><label class="field"><span>Asking price</span><input type="text" inputmode="decimal" id="cP" value="${esc(q.p || "")}" placeholder="$18,990"></label>
    <label class="field"><span>Kilometres</span><input type="text" inputmode="decimal" id="cK" value="${esc(q.k || "")}" placeholder="98,000"></label>
    <label class="field"><span>Year</span><input type="number" inputmode="numeric" id="cY" value="${esc(q.y || "")}"></label>
    <label class="field"><span>State</span><select id="cS"><option value="">–</option>${["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"].map(s => `<option ${q.s === s ? "selected" : ""}>${s}</option>`).join("")}</select></label></div>
    <label class="field"><span>Seller</span><select id="cW"><option value="">–</option><option value="dealer" ${q.w === "dealer" ? "selected" : ""}>Dealer</option><option value="private" ${q.w === "private" ? "selected" : ""}>Private</option></select></label>
    <button class="btn primary block" id="cGo">Check</button>
    <div class="note">Leave the price empty to see what a car with these details is expected to ask.</div></div><div id="cOut"></div>`;
  return h;
});
ROUTES["/check"].after = q => {
  // our own suggestion list: Firefox for Android's <datalist> is unreliable
  // trims first; a whole model too, which is judged within a few years of
  // the year typed (explore.CAR_NEAR_YEARS)
  const trims = Object.entries(DB.sel).filter(([, e]) => e.lv === "trim" || e.lv === "line")
    .map(([l, e]) => ({ l, low: l.toLowerCase(), n: (e.c && e.c.vehicles) || 0, e }));
  $("cT").oninput = () => {
    const w = $("cT").value.toLowerCase().split(/\s+/).filter(Boolean);
    const exact = DB.sel[$("cT").value.trim()];
    const res = w.length && !(exact && exact.lv === "trim")
      ? trims.filter(x => w.every(t => x.low.includes(t))).sort((a, b) => b.n - a.n).slice(0, 8) : [];
    $("cTs").innerHTML = res.map(x => `<button class="item" data-pick="${esc(x.l)}"><div class="main"><div class="t">${esc(x.l)}</div>
      <div class="s">${n0(x.n)} cars · median ${moneyK(x.e.c.median)}</div></div></button>`).join("");
    $("cTs").querySelectorAll("[data-pick]").forEach(b => b.onclick = () => {
      $("cT").value = b.dataset.pick; $("cTs").innerHTML = ""; $("cP").focus(); });
  };
  const run = (push) => {
    const l = selLabel($("cT").value.trim()), e = DB.sel[l];
    const out = $("cOut");
    if (!l) { out.innerHTML = ""; return; }
    if (!e || (e.lv !== "trim" && e.lv !== "line")) { out.innerHTML = `<div class="card"><span class="bad">Pick a trim from the list</span> - a price is judged against cars of one trim (or, given the year, a model's cars of nearby years).</div>`; return; }
    const opts = { price: $("cP").value, odometer: $("cK").value, year: $("cY").value, state: $("cS").value, seller: $("cW").value };
    if (push) history.replaceState(null, "", "#/check?" + new URLSearchParams(Object.entries({ l, p: opts.price, k: opts.odometer, y: opts.year, s: opts.state, w: opts.seller }).filter(x => x[1])).toString());
    const c = VS.carCard(DB, e.mk, e.mo, e.lv === "trim" ? e.va : null, opts);
    const S = c.subject;
    out.innerHTML = c.ok && S ? `<div class="card"><div class="sub">You entered</div><b>${S.price != null ? money(S.price) : "no price"}</b>${S.price_note ? ` <span class="tag">${esc(S.price_note.replace(/_/g, " "))}</span>` : ""} · ${S.odometer_km != null ? km(S.odometer_km) : "no km"}${S.year ? " · " + S.year : ""}${S.state ? " · " + S.state : ""}${S.seller_type ? " · " + S.seller_type : ""}</div>` + cardHtml(c)
      : cardHtml(c);
  };
  $("cGo").onclick = () => run(true);
  // Enter in any field checks (the trim field only once a trim is picked)
  ["cP", "cK", "cY"].forEach(id => $(id).onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); $(id).blur(); run(true); } });
  $("cT").onkeydown = e => { const x = DB.sel[$("cT").value.trim()];
    if (e.key === "Enter" && x && (x.lv === "trim" || x.lv === "line")) { e.preventDefault(); $("cT").blur(); run(true); } };
  if (q.l && (q.p || q.k || q.y)) run(false);
};

/* ------------------------------------------------------------ compare */
route("/compare", { tab: "compare", titleText: "Compare" }, q => {
  const labels = (q.l || "").split("|").map(selLabel).filter(l => DB.sel[l]).slice(0, 4);
  let h = `<div class="card"><h2>Side by side <small>up to 4</small></h2><div class="chips">${labels.map(l =>
    `<span class="chip on">${esc(l)}<button data-rm="${esc(l)}" aria-label="Remove">×</button></span>`).join("")}</div>
    ${labels.length < 4 ? `<input type="search" id="cq" placeholder="Add a make, model or trim" autocomplete="off" style="margin-top:10px"><div class="list" id="cres"></div>` : ""}</div>`;
  if (!labels.length) return h + `<div class="empty">Add two or more selections to compare them on the same statistics as the laptop's Compare page.</div>`;
  const cols = VS.compareCols(DB, labels), meta = DB.snap.cmp.rows || [];
  const colNames = ["#", "A", "B", "C", "D"];
  // monthly medians together
  const months = [...new Set(cols.flatMap(c => (c.months || []).map(m => m.d)))].sort();
  h += `<div class="card"><h2>Monthly median</h2>${chart({ series: cols.map((c, i) => ({ pts: (c.months || []).map(m => [mnum(m.d), m.median]), cls: "ln c" + i })),
    dots: cols.map((c, i) => ({ pts: (c.months || []).map(m => [mnum(m.d), m.median]), r: 2.5, cls: "dot c" + i })),
    xticks: months.map(m => [mnum(m), mShort(m)]).filter((t, i, a) => a.length <= 6 || i % Math.ceil(a.length / 6) === 0) })}
    <div class="legend">${cols.map((c, i) => `<span><i class="sw" style="background:var(--c${i})"></i><b>${colNames[i + 1]}</b> ${esc(c.label)}</span>`).join("")}</div></div>`;
  const fmt = (k, v) => v == null || v === "" ? "–" : k === "money" ? moneyK(v) : k === "pct" ? pct(v, true)
    : k === "km" ? kmK(v) : k === "date" ? dShort(v) : k === "days" ? Math.round(v) + " d" : k === "rate" ? Math.round(v) + "%"
    : k === "year" ? String(Math.round(v)) : k === "int" ? n0(v) : esc(String(v).replace(/_/g, " "));
  let group = null;
  h += `<div class="card"><div class="hscroll"><table class="tbl"><tr><th></th>${cols.map((c, i) => `<th><i class="sw" style="background:var(--c${i})"></i>${colNames[i + 1]}</th>`).join("")}</tr>`;
  for (const r of meta) {
    const key = r.key || r[0], lab = r.label || r[1], kind = r.kind || r[2], grp = r.group || r[3];
    if (key === "turn_basis") continue;
    if (grp) { group = grp; h += `<tr><td colspan="${cols.length + 1}" style="padding-top:12px;font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em">${esc(grp)}</td></tr>`; }
    h += `<tr><td>${esc(lab)}</td>${cols.map(c => `<td>${fmt(kind, c[key])}</td>`).join("")}</tr>`;
  }
  h += `</table></div><div class="note">${esc(DB.snap.cmp.basis || "")}</div></div>`;
  for (const c of cols) if (c.turn_verdict) h += `<details class="card"><summary><h2>${esc(c.label)}: how fast it sells</h2></summary><p class="verdict">${esc(c.turn_verdict)}</p></details>`;
  return h;
});
ROUTES["/compare"].after = q => {
  const cur = (q.l || "").split("|").map(selLabel).filter(l => DB.sel[l]);
  const setL = arr => go("#/compare?l=" + enc(arr.join("|")), true);
  document.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => setL(cur.filter(l => l !== b.dataset.rm)));
  if (!$("cq")) return;
  const all = Object.entries(DB.sel).map(([l, e]) => ({ l, low: l.toLowerCase(), n: (e.c && e.c.n) || 0, e }));
  $("cq").oninput = () => {
    const w = $("cq").value.toLowerCase().split(/\s+/).filter(Boolean);
    const res = w.length ? all.filter(x => w.every(t => x.low.includes(t)) && !cur.includes(x.l)).sort((a, b) => b.n - a.n).slice(0, 12) : [];
    $("cres").innerHTML = res.map(x => `<button class="item" data-add="${esc(x.l)}"><div class="main"><div class="t">${esc(x.l)}</div>
      <div class="s">${n0(x.e.c.vehicles)} cars · median ${moneyK(x.e.c.median)}</div></div></button>`).join("");
    $("cres").querySelectorAll("[data-add]").forEach(b => b.onclick = () => setL(cur.concat([b.dataset.add])));
  };
};

/* ------------------------------------------------------------ starred */
route("/starred", { tab: "starred", titleText: "Starred" }, () => {
  const rows = [...DB.stars.entries()];
  const waiting = STARQ.load().filter(o => !o.res).length;
  const head = waiting ? `<div class="card flat"><div class="sub">${waiting} star change${waiting > 1 ? "s" : ""} waiting to reach the laptop${
    STAR.off ? ` - <span class="warn">${esc(STAR.off)}</span>` : " - sent on the next sync"}.</div></div>` : "";
  if (!rows.length) return head + `<div class="empty"><b>No starred cars</b>Tap ☆ on a car to star it - here or on the laptop's Explore page.</div>`;
  let h = head + `<div class="card"><h2>Starred <small>${rows.length}</small></h2><div class="list">`;
  for (const [k, s] of rows) {
    const v = DB.byKey.get(k), w = STAR.pending.has(k) ? "waiting to sync" : "";
    if (!v) { h += `<div class="item"><div class="main"><div class="t muted">A starred car that is no longer in the data</div><div class="s">${esc([s.note, w].filter(Boolean).join(" · "))}</div></div>
      <button class="btn" data-unstar="${esc(k)}" aria-label="Unstar">★</button></div>`; continue; }
    h += carItem(vehicleAsItem(v), [s.note, w].filter(Boolean).join(" · "));
  }
  return h + `</div><div class="note">Star or unstar a car from its screen. Changes made here reach the laptop's watchlist on the next sync.</div></div>`;
});

ROUTES["/starred"].after = () => document.querySelectorAll("[data-unstar]").forEach(b => b.onclick = () => tapStar(b.dataset.unstar));

/* ------------------------------------------------------------ sync & pairing */
// how this phone reaches the laptop, from the address VPA was opened at
function connection() {
  const secure = location.protocol === "https:", ts = /\.ts\.net$/i.test(location.hostname);
  return secure && ts ? { cls: "good", text: "Tailscale · private HTTPS", host: location.hostname }
    : secure ? { cls: "good", text: "HTTPS", host: location.hostname }
    : { cls: "warn", text: "Home Wi-Fi · plain http", host: location.host };
}
route("/sync", { tab: null, back: true, titleText: "Sync & settings", needsData: false }, () => {
  const paired = !!LS.get("token"), S = DB ? DB.snap : null, cn = connection(), alt = LS.get("https");
  let h = "";
  const viaTs = /\.ts\.net$/i.test(location.hostname || "");
  if (SYNC.state === "unreachable" && paired) h += `<div class="card" style="border-color:var(--accent)">
    <h2>Can't reach the laptop</h2><p class="sub">${viaTs ? "Turn <b>Tailscale</b> on to sync, then come back here - it syncs by itself."
      : "Check the laptop is on and this phone is on the same Wi-Fi."} The laptop must be awake.
      ${S ? `Your copy is from <b>${dLong(S.built.slice(0, 10))}</b>.` : ""}</p>
    ${viaTs ? `<a class="btn primary block" href="${TS_LINK}">Open Tailscale</a>` : ""}
    <button class="btn block" id="syncGo2" style="margin-top:8px">Try again</button></div>`;
  if (cn.cls === "warn" && alt) h += `<div class="card" style="border-color:var(--accent)"><h2>Use the secure address</h2>
    <p class="sub">Your laptop has a private Tailscale address. Opened there, VPA works with no signal and syncs from anywhere the laptop is awake.</p>
    <a class="btn primary block" href="${esc(alt)}">Open ${esc(alt.replace(/^https:\/\//, ""))}</a>
    <div class="note">Then pair there and add it to your home screen. Tailscale must be on to sync.</div></div>`;
  if (!paired || SYNC.state === "removed") {
    h += `<div class="card"><h2>Pair this phone</h2>${SYNC.state === "removed" ? '<p class="warn">This phone was removed on the laptop. Pair it again to get updates - your copy stays until then.</p>' : ""}
      <p class="sub">On the laptop: VPA → <b>Phone</b> → <b>Pair a phone</b>. Type the six-digit code shown there.</p>
      <input type="text" id="code" class="code" inputmode="numeric" maxlength="7" autocomplete="one-time-code" placeholder="000 000">
      <button class="btn primary block" id="pairGo" style="margin-top:10px">Pair</button><div id="pairMsg" class="note"></div></div>`;
  }
  const st = { ok: ["good", "Up to date"], offline: ["", "No signal"], unreachable: ["", "Can't reach the laptop"], error: ["bad", "Sync failed"],
               downloading: ["", "Downloading"], idle: ["", "Waiting"], unpaired: ["warn", "Not paired"], removed: ["warn", "Removed on the laptop"] }[SYNC.state] || ["", SYNC.state];
  h += `<div class="card"><h2>This phone's copy</h2>${S ? `<div class="big">${n0(S.counts.vehicles)} cars</div>
    <div class="sub">${n0(S.counts.sightings)} sightings · ${n0(S.counts.selections)} makes, models &amp; trims</div>
    <table class="tbl" style="margin-top:10px"><tr><td>Data as of</td><td>${dLong(S.built.slice(0, 10))} ${S.built.slice(11, 16)}</td></tr>
    <tr><td>Latest capture</td><td>${dLong(S.counts.last_day)}</td></tr><tr><td>Last checked</td><td>${ago(SYNC.lastOk)}</td></tr>
    <tr><td>Status</td><td class="${st[0]}">${st[1]}</td></tr>
    ${LS.get("device") ? `<tr><td>Paired as</td><td>${esc(LS.get("device"))}</td></tr>` : ""}</table>`
    : `<p class="sub">No copy yet.</p>`}
    <div class="spread" style="margin-top:10px;font-size:13px"><span class="muted">Connection</span>
      <span style="text-align:right"><span class="${cn.cls}">${esc(cn.text)}</span><br><span class="faint" style="font-size:11.5px">${esc(cn.host)}</span></span></div>
    ${SYNC.error && SYNC.state !== "ok" ? `<div class="note bad">${esc(SYNC.error)}</div>` : ""}
    ${paired ? `<div class="row" style="margin-top:12px;gap:8px"><button class="btn primary" style="flex:1" id="syncGo" ${SYNC.busy ? "disabled" : ""}>${SYNC.busy ? "Syncing…" : "Sync now"}</button>
      ${viaTs ? `<a class="btn" href="${TS_LINK}">Tailscale</a>` : ""}</div>` : ""}
    <div class="note">Syncs by itself whenever VPA is opened or brought back to the front, and every few minutes while open${
      cn.text.startsWith("Tailscale") ? ", whenever Tailscale is on and the laptop is awake" : ""}. Only a newer copy is downloaded. With no signal, the copy on this phone is used.</div></div>`;
  const secure = window.isSecureContext && "serviceWorker" in navigator;
  const ready = secure && navigator.serviceWorker.controller;
  const sq = STARQ.load().filter(o => !o.res).length;
  if (sq) h += `<div class="card"><h2>Stars</h2><p class="sub">${sq} star change${sq > 1 ? "s" : ""} made on this phone ${
    STAR.off ? `can't be sent: <span class="warn">${esc(STAR.off)}</span>` : "waiting to reach the laptop - sent on the next sync."}</p></div>`;
  const th = window.vpaTheme ? vpaTheme() : "auto";
  h += `<div class="card"><h2>Appearance</h2><div class="seg">${[["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]].map(([k, l]) =>
    `<button data-theme-set="${k}" class="${th === k ? "on" : ""}" aria-pressed="${th === k}">${l}</button>`).join("")}</div>
    <div class="note">Auto follows the phone's own light or dark setting. Kept on this phone only.</div></div>`;
  h += `<div class="card"><h2>Offline &amp; home screen</h2>
    <p class="sub" id="offl">${ready ? '<span class="good">Ready to open with no signal.</span>' : secure ? "Getting ready for offline use…"
      : '<span class="warn">This address is plain http</span>, so the app itself can\'t open with no signal (browsers only allow that on HTTPS). Your data is still saved on the phone. Opening VPA through the laptop\'s HTTPS (Tailscale) address fixes this.'}</p>
    <p class="sub">To add VPA to your home screen in Firefox: tap <b>⋮</b> (menu) → <b>Add app to Home screen</b> (or <b>Install</b>).</p></div>`;
  if (paired) h += `<div class="card"><button class="btn danger block" id="unpair">Unpair this phone</button>
    <div class="note">Removes the key from this phone. Your copy of the data stays; pair again to get updates.</div></div>`;
  return h;
});
ROUTES["/sync"].after = () => {
  const ths = document.querySelectorAll("[data-theme-set]");
  ths.forEach(b => b.onclick = () => {
    if (window.vpaTheme) vpaTheme(b.dataset.themeSet);
    ths.forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", String(x === b)); });
  });
  if ($("syncGo2")) $("syncGo2").onclick = () => sync(true);
  if (window.isSecureContext && "serviceWorker" in navigator && !navigator.serviceWorker.controller)
    navigator.serviceWorker.getRegistration().then(r => {
      if (r && r.active && $("offl")) $("offl").innerHTML = '<span class="good">Ready to open with no signal.</span>'; });
  if ($("syncGo")) $("syncGo").onclick = () => sync(true);
  if ($("unpair")) $("unpair").onclick = () => {
    if (!confirm("Unpair this phone? Your copy of the data stays on it.")) return;
    LS.set("token", null); SYNC.state = "unpaired"; pill(); render(true);
  };
  if ($("pairGo")) $("pairGo").onclick = async () => {
    const code = $("code").value.replace(/\D/g, ""), m = $("pairMsg");
    if (code.length !== 6) { m.innerHTML = '<span class="bad">The code is six digits.</span>'; return; }
    m.textContent = "Pairing…";
    try {
      const r = await fetch("/api/pair", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }), signal: withTimeout(10000) }).then(x => x.json());
      if (!r.ok) { m.innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
      LS.set("token", r.token); LS.set("device", r.device.name); SYNC.state = "idle";
      toast("Paired as " + r.device.name); render(true); sync(true);
    } catch (e) { m.innerHTML = '<span class="bad">Can\'t reach the laptop. Is phone sync on, and is this phone on the same network?</span>'; }
  };
};

/* ------------------------------------------------------------ start */
(async function start() {
  try { const snap = await idbGet("snap"); if (snap && snap.format && snap.vehicles) useSnapshot(snap); }
  catch (e) { console.warn("no stored copy", e); }
  pill(); render(false);
  autoSync();
  if (window.isSecureContext && "serviceWorker" in navigator) {
    let reloading = false;
    const hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (hadController && !reloading) { reloading = true; location.reload(); }   // a new app version
    });
    navigator.serviceWorker.register("/sw.js").catch(e => console.warn("offline shell unavailable", e));
  }
})();
