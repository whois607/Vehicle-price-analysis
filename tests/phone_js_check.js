// Run by tests/check_phone_app.py:  node phone_js_check.js <stats.js> <snapshot.json> <expect.json>
// Recomputes every expectation with the phone's stats.js and prints one JSON
// line per category: how many compared, and the first few mismatches.
"use strict";
const fs = require("fs");
const [statsPath, snapPath, expPath] = process.argv.slice(2);
const VS = require(statsPath);
const snap = JSON.parse(fs.readFileSync(snapPath, "utf8"));
const E = JSON.parse(fs.readFileSync(expPath, "utf8"));
const DB = VS.index(snap);
const out = {};

// numbers within a hundredth of a dollar (float summation order differs
// between numpy and a plain loop; every figure on screen is rounded far
// coarser than this), everything else exactly
function diff(a, b, path) {
  if (typeof a === "number" && typeof b === "number")
    return Math.abs(a - b) <= 0.0101 + 1e-9 * Math.abs(b) ? null : `${path}: phone ${a} laptop ${b}`;
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object")
    return a === b ? null : `${path}: phone ${JSON.stringify(a)} laptop ${JSON.stringify(b)}`;
  if (Array.isArray(a) !== Array.isArray(b)) return `${path}: array vs object`;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return `${path}: phone ${a.length} items, laptop ${b.length}`;
    for (let i = 0; i < a.length; i++) { const d = diff(a[i], b[i], `${path}[${i}]`); if (d) return d; }
    return null;
  }
  for (const k of Object.keys(b)) {
    if (!(k in a)) return `${path}.${k}: missing on the phone`;
    const d = diff(a[k], b[k], `${path}.${k}`); if (d) return d;
  }
  return null;
}
function tally(cat, label, d) {
  const o = out[cat] || (out[cat] = { n: 0, bad: [] });
  o.n += 1;
  if (d && o.bad.length < 5) o.bad.push(`${label}: ${d}`);
  else if (d) o.bad.push(null);
}
const pick = (o, keys) => Object.fromEntries(keys.filter(k => k in o).map(k => [k, o[k]]));

for (const x of E.details) {
  const all = VS.rowsOf(DB, x.mk, x.mo, x.va);
  const f = VS.applyFilters(all, x.filters);
  const rows = f.rows, P = x.payload, lab = x.label + (x.filters ? " " + JSON.stringify(x.filters) : "");
  const level = x.va ? "trim" : x.mo ? "line" : "make";
  const h = VS.head(rows, x.label, level);
  tally("headline", lab, diff(h, P.head, "head"));
  tally("splits", lab, diff(VS.splits(rows), P.splits, "splits"));
  const bd = VS.byday(rows);
  tally("capture days", lab, diff(bd, P.byday.map(b => pick(b, ["d", "n", "vehicles", "median", "q25", "q75", "lo", "hi"])), "byday"));
  tally("supply", lab, diff(VS.supply(bd), P.supply, "supply"));
  tally("monthly", lab, diff(VS.bymonth(rows), P.bymonth, "bymonth"));
  const pool = VS.latestPerVehicle(VS.prep(rows)).map(r => r.p);
  tally("histogram", lab, diff(VS.hist(pool), P.hist, "hist"));
  tally("filters applied", lab, diff(f.applied, P.filters || [], "filters"));
  tally("filter options", lab, diff(VS.facetOptions(all), P.facets, "facets"));
  const sc = VS.scatter(rows, snap.consts.MAX_SCATTER);
  tally("scatter", lab, diff(sc.points, P.scatter, "scatter") || (sc.total === P.scatter_total ? null : "scatter_total"));
  const c = VS.cars(DB, rows);
  tally("car list", lab, diff(c.vehicles.slice(0, snap.consts.MAX_VEHICLES), P.vehicles, "vehicles"));
  tally("price movers", lab, diff(c.movers.slice(0, 40), P.movers, "movers"));
  if (!x.filters) {
    const e = DB.sel[x.label];
    const s = VS.standouts(DB, rows, e.d);
    const key = o => [o.z == null ? 1e9 : o.z, o.key];
    const ord = a => a.slice().sort((p, q) => { const A = key(p), B = key(q); return A[0] - B[0] || (A[1] < B[1] ? -1 : 1); });
    // the laptop sends its first MAX_STANDOUTS (by distance from the middle);
    // the phone keeps every car, so compare the same slice
    tally("standouts", lab, diff(ord(s.rows.slice(0, snap.consts.MAX_STANDOUTS || 800)), ord(P.standouts), "standouts"));
    tally("live cars", lab, diff([s.live.day, s.live.keys.size],
      [P.distribution.live_day, P.distribution.live_n], "live"));
    // and the shipped (laptop) numbers are what the phone shows unfiltered
    tally("shipped explore", lab, diff(e.d.head, P.head, "shipped.head"));
  }
}
const NOW = new Date(E.now);
for (const x of E.cards) {
  const got = VS.carCard(DB, x.mk, x.mo, x.va, x.opts, NOW);
  const exp = x.payload;
  if (got.history && exp.history) {
    got.history = pick(got.history, Object.keys(got.history).filter(k => k in exp.history));
    exp.history = pick(exp.history, Object.keys(got.history));
  }
  tally(x.opts.key ? "car card (stored car)" : "car card (typed car)", `${x.va} ${JSON.stringify(x.opts)}`,
        diff(got, exp, "card"));
}
for (const x of E.links) {
  const g = VS.findLink(DB, x.link), e = x.payload;
  const a = { ok: g.ok, in_data: g.in_data, key: g.key, basis: g.basis, error: g.error,
              listing_id: g.found ? g.found.listing_id : undefined };
  const b = { ok: e.ok, in_data: e.in_data, key: e.key, basis: e.basis,
              error: e.error ? e.error.replace("Pick the car's trim below instead, or use Type one I've seen.",
                                               "Find the car's trim instead, or use Price check.") : e.error,
              listing_id: e.found && !e.in_data ? e.found.listing_id : undefined };
  tally("advert links", x.link, diff(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)), "link"));
}
for (const x of E.parse) {
  const g = [VS.parsePrice(x.s), VS.parseOdometer(x.s), VS.normState(x.s), VS.normSeller(x.s), VS.normYear(x.s)];
  tally("typed values", JSON.stringify(x.s), diff(g, x.want, "parse"));
}
for (const x of E.rounding) tally("python rounding", String(x[0]), diff(VS.pyRound(x[0], x[1]), x[2], "round"));
for (const x of E.compare) {
  const cols = VS.compareCols(DB, x.labels).map(c => pick(c, ["label", "rank", "vs_cheapest", "median"]));
  tally("compare ranks", x.labels.join(" / "), diff(cols, x.want, "compare"));
}
// ---- every screen, through app.js itself, against a stand-in page: no
// screen may throw, or print NaN / undefined / [object Object]
(function screens() {
  const vm = require("vm"), path = require("path");
  const el = () => { const e = { hidden: false, className: "", textContent: "", innerHTML: "", style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} }, querySelector: () => el(), querySelectorAll: () => [],
    addEventListener() {}, focus() {}, scrollIntoView() {} }; return e; };
  const els = {};
  const ctx = { console, URLSearchParams, URL, Intl, Date, Math, JSON, Promise, Map, Set, Number, String,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    location: { hash: "#/", replace() {}, reload() {} }, history: { length: 1, back() {}, replaceState() {} },
    navigator: { onLine: false }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { getElementById: id => els[id] || (els[id] = el()), querySelectorAll: () => [], addEventListener() {},
                visibilityState: "visible" },
    window: { addEventListener() {}, scrollTo() {}, scrollY: 0, isSecureContext: false },
    indexedDB: undefined, fetch: () => Promise.reject(new Error("offline")) };
  ctx.self = ctx; ctx.window.VS = undefined;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(statsPath, "utf8"), ctx);
  vm.runInContext(fs.readFileSync(path.join(path.dirname(statsPath), "app.js"), "utf8"), ctx);
  ctx.__snap = snap;
  vm.runInContext("useSnapshot(__snap)", ctx);
  const R = vm.runInContext("ROUTES", ctx);
  const run = (what, p, q) => {
    let h;
    try { h = R[p].fn(q); } catch (e) { tally("screens render", what, "threw " + e.message); return; }
    const text = String(h).replace(/<[^>]+>/g, " ");
    const m = /.{0,30}(\bNaN\b|\bundefined\b|\[object Object\]).{0,20}/.exec(text);
    tally("screens render", what, m ? "shows: " + m[0] : null);
  };
  const labels = Object.keys(snap.sel);
  for (const l of labels) {
    run("selection " + l, "/sel", { l });
    run("filtered " + l, "/sel", { l, y0: "2010", y1: "2015", p1: "40000", s: "cheap" });
    run("keyword " + l, "/sel", { l, kw: "xr", s: "km", trims: "all" });
    run("listing filters " + l, "/sel", { l, cond: "notnew", gb: "manual", nosug: "1", st: "~", s: "dear" });
    run("listing filters 2 " + l, "/sel", { l, cond: "anynew", pt: "drive away", cyl: "6" });
  }
  for (const v of snap.vehicles.rows) run("car " + v[0], "/car", { k: v[0] });
  for (const l of labels.filter(l => snap.sel[l].lv === "trim"))
    run("price check " + l, "/check", { l, p: "$19,990", k: "120k", y: "2012", s: "NSW", w: "private" });
  for (const m of snap.picker.makes) run("make " + m.name, "/make", { m: m.name });
  for (let i = 0; i < labels.length; i++)
    run("compare", "/compare", { l: [labels[i], labels[(i * 7 + 3) % labels.length], labels[(i * 5 + 1) % labels.length]].join("|") });
  // a label saved before trims were regrouped opens the group it is now
  // (phonebuild._aliases -> app.js selLabel), on Find, Compare and Check
  for (const [old, now] of Object.entries(snap.alias || {})) {
    const show = p => { try { return String(R[p.r].fn(p.q)); } catch (e) { return "threw " + e.message; } };
    const sel = show({ r: "/sel", q: { l: old } }), cmp = show({ r: "/compare", q: { l: old } });
    tally("old labels", old, sel.includes("isn't in your data") || sel.startsWith("threw")
      ? `selection: ${sel.slice(0, 80)}` : !cmp.includes(now) ? "compare did not open " + now : null);
  }
  for (const [p, q] of [["/", {}], ["/", { lv: "trim" }], ["/find", {}], ["/find", { y0: "2010", p1: "30000" }],
                        ["/starred", {}], ["/sync", {}], ["/sel", { l: "No Such Car" }], ["/car", { k: "nope" }],
                        ["/check", {}], ["/check", { l: "Ford Falcon", p: "1" }], ["/compare", {}]])
    run("screen " + p + JSON.stringify(q), p, q);
})();
for (const [k, v] of Object.entries(out)) v.bad_total = v.bad.length, v.bad = v.bad.filter(Boolean);
console.log(JSON.stringify(out));
