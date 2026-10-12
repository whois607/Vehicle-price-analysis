/* VPA phone - the arithmetic.

   Pure functions, no DOM. The heavy statistics (forecasts, mileage fits,
   bell curves, turnover) arrive precomputed by the laptop's own Python in the
   phone copy; what is here is the counting the phone does over the sightings
   it carries - car lists, ranks, comparables, filtered stats - each a port of
   the named Python function, rule for rule. tests/check_phone_app.py runs
   this file under node against that Python on the same data, so the two
   cannot drift apart unnoticed.
*/
(function (root) {
"use strict";

/* ------------------------------------------------------------ numbers */
// Python's round(): the decimal the float actually is, with an exact half
// going to the even neighbour (12.5 -> 12, 0.125 -> 0.12).
function pyRound(x, nd) {
  if (x == null || !isFinite(x)) return null;
  nd = nd || 0;
  const ax = Math.abs(x), sign = x < 0 ? -1 : 1;
  // a true tie only when the float's EXACT decimal digits are ...5000...:
  // 0.125 is one, 0.05 (really 0.05000000000000000277) is not
  const digits = ax.toFixed(Math.min(100, nd + 60)).split(".")[1] || "";
  if (/^50*$/.test(digits.slice(nd))) {
    const m = Math.pow(10, nd), fl = Math.floor(ax * m);
    return sign * (fl % 2 === 0 ? fl : fl + 1) / m;
  }
  const r = Number(ax.toFixed(nd));
  return r === 0 ? 0 : sign * r;
}
// explore._num: round to 2 places, NaN/None -> null
const num = v => (v == null || Number.isNaN(v)) ? null : pyRound(Number(v), 2);
const asc = (a, b) => a - b;
// numpy's linear quantile, including its lerp for t >= 0.5
function quantile(vals, q) {
  const a = vals.filter(v => v != null && !Number.isNaN(v)).slice().sort(asc);
  const n = a.length;
  if (!n) return null;
  const pos = q * (n - 1), lo = Math.floor(pos), hi = Math.min(lo + 1, n - 1), t = pos - lo;
  const d = a[hi] - a[lo];
  return t >= 0.5 ? a[hi] - d * (1 - t) : a[lo] + d * t;
}
function median(vals) {
  const a = vals.filter(v => v != null && !Number.isNaN(v)).slice().sort(asc);
  const n = a.length;
  if (!n) return null;
  return n % 2 ? a[(n - 1) / 2] : (a[n / 2 - 1] + a[n / 2]) / 2;
}
function mean(vals) {
  const a = vals.filter(v => v != null && !Number.isNaN(v));
  if (!a.length) return null;
  let s = 0;
  for (const v of a) s += v;
  return s / a.length;
}
const minOf = a => { const b = a.filter(v => v != null); return b.length ? Math.min(...b) : null; };
const maxOf = a => { const b = a.filter(v => v != null); return b.length ? Math.max(...b) : null; };
const uniq = a => new Set(a).size;
// Python str(): "1,234" style with commas, no decimals (f"{x:,.0f}")
const fmt0 = x => {
  const r = pyRound(x, 0), s = String(Math.abs(r)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (r < 0 ? "-" : "") + s;
};
const dayNum = d => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10)) / 86400000;

/* ------------------------------------------------------------ the copy */
// Index a phone copy: cars as objects, sightings joined to their car, in
// the analysis frame's own order (capture day, then key).
function index(snap) {
  const vc = snap.vehicles.cols, V = snap.vehicles.rows.map((r, i) => {
    const o = { i };
    vc.forEach((c, j) => { o[c] = r[j]; });
    return o;
  });
  const byKey = new Map(V.map(v => [v.k, v]));
  const S = snap.sightings.rows.map(r => ({ v: V[r[0]], d: r[1], p: r[2], km: r[3] }));
  const bySel = new Map();               // "make|model|variant" -> rows, frame order
  const add = (k, r) => { let a = bySel.get(k); if (!a) bySel.set(k, a = []); a.push(r); };
  for (const r of S) {
    const v = r.v;
    add(v.make + "||", r);
    add(v.make + "|" + v.model + "|", r);
    if (v.variant != null && String(v.variant).trim()) add(v.make + "|" + v.model + "|" + v.variant, r);
  }
  const byCar = new Map();
  for (const r of S) { let a = byCar.get(r.v.k); if (!a) byCar.set(r.v.k, a = []); a.push(r); }
  const stars = new Map((snap.starred.rows || []).map(r => [r[0], { at: r[1], note: r[2] }]));
  const dismissed = (snap.dismissed || []).map(ks => new Set(ks));
  return { snap, V, byKey, S, bySel, byCar, stars, dismissed,
           consts: snap.consts || {}, sel: snap.sel || {} };
}
const rowsOf = (DB, mk, mo, va) =>
  DB.bySel.get((mk || "") + "|" + (mo || "") + "|" + (va || "")) || [];
const labelOf = (mk, mo, va) => [mk, mo, va].filter(Boolean).join(" ");

/* ------------------------------------------------------------ analyze.py */
const stableBy = (arr, f) => arr.map((x, i) => [x, i])
  .sort((a, b) => { const fa = f(a[0]), fb = f(b[0]); return fa < fb ? -1 : fa > fb ? 1 : a[1] - b[1]; })
  .map(x => x[0]);
// analyze.prep: priced rows, one per car per month (its last in that month)
function prep(rows) {
  const s = stableBy(rows.filter(r => r.p != null), r => r.d);
  const last = new Map();
  s.forEach((r, i) => last.set(r.v.k + "|" + r.d.slice(0, 7), i));
  return s.filter((r, i) => last.get(r.v.k + "|" + r.d.slice(0, 7)) === i);
}
// analyze.latest_per_vehicle: one row per car, its latest
function latestPerVehicle(prepped) {
  const s = stableBy(prepped, r => r.d), last = new Map();
  s.forEach((r, i) => last.set(r.v.k, i));
  return s.filter((r, i) => last.get(r.v.k) === i);
}
function liveKeys(rows) {
  const pr = rows.filter(r => r.p != null);
  if (!pr.length) return { keys: new Set(), day: null };
  const day = pr.reduce((m, r) => r.d > m ? r.d : m, pr[0].d);
  return { keys: new Set(pr.filter(r => r.d === day).map(r => r.v.k)), day };
}
function collidedKeys(rows) {
  const seen = new Map(), out = new Set();
  for (const r of rows) {
    if (r.p == null) continue;
    const k = r.v.k + "|" + r.d;
    let s = seen.get(k); if (!s) seen.set(k, s = new Set());
    s.add(r.p);
    if (s.size > 1) out.add(r.v.k);
  }
  return out;
}
const zOf = (price, curve) => (!curve || !curve.usable || price == null || price <= 0)
  ? null : pyRound((Math.log(price) - curve.mu) / curve.sigma, 2);
// analyze.price_curve's drawing grid, from the shipped mu/sigma/lo/hi
function curveGrid(c, grid) {
  grid = grid || 72;
  if (!c || !c.usable) return null;
  const x0 = Math.max(Math.min(c.lo, Math.exp(c.mu - 3 * c.sigma)), 1);
  const x1 = Math.max(c.hi, Math.exp(c.mu + 3 * c.sigma));
  const xs = [], ys = [];
  for (let i = 0; i < grid; i++) {
    const x = x0 + (x1 - x0) * i / (grid - 1);
    xs.push(x);
    ys.push(1 / (x * c.sigma * Math.sqrt(2 * Math.PI))
            * Math.exp(-((Math.log(x) - c.mu) ** 2) / (2 * c.sigma ** 2)));
  }
  return { x: xs, y: ys };
}
// dupes.dup_sigs: (price, odometer) held by more than one car, minus groups
// you said are different cars
function dupSigs(rows, dismissed) {
  const by = new Map();
  for (const r of rows) {
    if (r.km == null) continue;
    const s = r.p + "|" + r.km;
    let ks = by.get(s); if (!ks) by.set(s, ks = new Set());
    ks.add(r.v.k);
  }
  const sigs = new Set(); let extra = 0;
  for (const [s, ks] of by) {
    if (ks.size > 1 && !dismissed.some(d => [...ks].every(k => d.has(k)))) {
      sigs.add(s); extra += ks.size - 1;
    }
  }
  return { sigs, extra };
}

/* ------------------------------------------------------------ explore.py */
function parseNum(v) {
  if (v == null || v === "") return null;
  const f = Number(String(v).replace(/,/g, "").replace(/\$/g, "").trim());
  return Number.isFinite(f) && String(v).trim() !== "" ? f : null;
}
// explore._apply_filters
function applyFilters(rows, f) {
  f = f || {};
  const applied = [];
  let out = rows;
  const y0 = parseNum(f.year_min), y1 = parseNum(f.year_max);
  if (y0 != null || y1 != null) {
    out = out.filter(r => r.v.year == null ||
      ((y0 == null || r.v.year >= y0) && (y1 == null || r.v.year <= y1)));
    applied.push({ kind: "year", text: (y0 && y1) ? `${Math.trunc(y0)}-${Math.trunc(y1)}`
      : `${y0 ? Math.trunc(y0) : ""}-${y1 ? Math.trunc(y1) : ""}`.replace(/^-+|-+$/g, "") });
  }
  const p0 = parseNum(f.price_min), p1 = parseNum(f.price_max);
  if (p0 != null || p1 != null) {
    out = out.filter(r => r.p == null || ((p0 == null || r.p >= p0) && (p1 == null || r.p <= p1)));
    const lo = p0 != null ? "$" + fmt0(p0) : "", hi = p1 != null ? "$" + fmt0(p1) : "";
    applied.push({ kind: "price", text: lo && hi ? `${lo} to ${hi}` : (lo ? `from ${lo}` : `up to ${hi}`) });
  }
  const kw = String(f.keyword || "").trim();
  if (kw) {
    const k = kw.toLowerCase();
    out = out.filter(r => ["variant", "title", "model"].some(c =>
      r.v[c] != null && String(r.v[c]).toLowerCase().includes(k)));
    applied.push({ kind: "keyword", text: `"${kw}"` });
  }
  return { rows: out, applied };
}
// detail_payload's headline
function head(rows, label, level) {
  const pr = rows.filter(r => r.p != null), P = pr.map(r => r.p);
  const days = rows.map(r => r.d);
  return {
    label, level, observations: rows.length, vehicles: uniq(rows.map(r => r.v.k)),
    days: uniq(days), first_day: days.reduce((a, b) => b < a ? b : a),
    last_day: days.reduce((a, b) => b > a ? b : a),
    median: num(median(P)), mean: num(mean(P)), q25: num(quantile(P, .25)),
    q75: num(quantile(P, .75)), lo: num(minOf(P)), hi: num(maxOf(P)),
    median_odo: num(median(pr.map(r => r.km))),
    pct_no_odo: pyRound(rows.filter(r => r.km == null).length / rows.length * 100, 0),
    full: rows.filter(r => r.v.src !== "quick_add").length,
    brief: rows.filter(r => r.v.src === "quick_add").length,
    collisions: collidedKeys(rows).size,
  };
}
function splits(rows) {
  const pr = rows.filter(r => r.p != null);
  const one = (get, name) => {
    const g = new Map();
    for (const r of pr) {
      const v = get(r); const k = v == null ? "(unknown)" : name(v);
      let a = g.get(k); if (!a) g.set(k, a = []); a.push(r);
    }
    return [...g.keys()].sort().map(k => {
      const a = g.get(k), P = a.map(r => r.p);
      return { name: k, n: a.length, vehicles: uniq(a.map(r => r.v.k)), median: num(median(P)),
               lo: num(minOf(P)), hi: num(maxOf(P)), odo: num(median(a.map(r => r.km))) };
    });
    };
  const byN = a => stableBy(a, r => -r.n);
  return { seller: byN(one(r => r.v.seller, String)), state: byN(one(r => r.v.state, String)),
           source: byN(one(r => r.v.src, String)),
           year: one(r => r.v.year, v => String(Math.trunc(v))) };
}
function byday(rows) {
  const g = new Map();
  for (const r of rows) { if (r.p == null) continue; let a = g.get(r.d); if (!a) g.set(r.d, a = []); a.push(r); }
  return [...g.keys()].sort().map(d => {
    const a = g.get(d), P = a.map(r => r.p);
    return { d, n: a.length, vehicles: uniq(a.map(r => r.v.k)), median: num(median(P)),
             q25: num(quantile(P, .25)), q75: num(quantile(P, .75)),
             lo: num(minOf(P)), hi: num(maxOf(P)) };
  });
}
function supply(bd) {
  const c = bd.map(b => b.vehicles);
  return { captures: bd.length, now: c.length ? c[c.length - 1] : null,
           avg: c.length ? pyRound(c.reduce((a, b) => a + b, 0) / c.length, 1) : null,
           most: c.length ? Math.max(...c) : null, fewest: c.length ? Math.min(...c) : null,
           change: c.length > 1 ? c[c.length - 1] - c[c.length - 2] : null };
}
// analyze.bucket_stats over prep(): the monthly series
function bymonth(rows) {
  const p = prep(rows), g = new Map();
  for (const r of p) { const m = r.d.slice(0, 7); let a = g.get(m); if (!a) g.set(m, a = []); a.push(r.p); }
  return [...g.keys()].sort().map(d => {
    const P = g.get(d);
    return { d, n: P.length, median: num(pyRound(median(P), 0)),
             q25: num(pyRound(quantile(P, .25), 0)), q75: num(pyRound(quantile(P, .75), 0)) };
  });
}
// numpy.histogram over one price per car, as detail_payload bins it
function hist(prices) {
  const a = prices.filter(v => v != null);
  const n = a.length;
  if (n < 4) return null;
  const lo = Math.min(...a), hi = Math.max(...a);
  if (!(hi > lo)) return null;
  const bins = Math.trunc(Math.min(24, Math.max(6, pyRound(Math.sqrt(n), 0))));
  const step = (hi - lo) / bins, edges = [];
  for (let i = 0; i <= bins; i++) edges.push(i === bins ? hi : i * step + lo);
  const counts = new Array(bins).fill(0), norm = bins / (hi - lo);
  for (const x of a) {
    let i = Math.trunc((x - lo) * norm);
    if (i === bins) i -= 1;
    if (x < edges[i]) i -= 1;
    if (i !== bins - 1 && x >= edges[i + 1]) i += 1;
    counts[i] += 1;
  }
  return { bins: counts.map((c, i) => ({ lo: num(edges[i]), hi: num(edges[i + 1]), n: c })),
           median: num(median(a)), q25: num(quantile(a, .25)), q75: num(quantile(a, .75)), n };
}
function scatter(rows, max) {
  const w = rows.filter(r => r.p != null && r.km != null && r.km > 0);
  return { total: w.length, points: w.slice(Math.max(0, w.length - (max || 1200))).map(r => ({
    odo: num(r.km), price: num(r.p), year: r.v.year, variant: r.v.variant, key: r.v.k, d: r.d })) };
}
// detail_payload's vehicle table and price movers
// a car's first stored photo address (vpa/photos.py), as the laptop's lists send it
const firstPhoto = v => (v && v.ph && v.ph.length ? v.ph[0] : null);
function cars(DB, rows) {
  const collided = collidedKeys(rows), groups = new Map();
  for (const r of stableBy(rows, r => r.d)) {
    let a = groups.get(r.v.k); if (!a) groups.set(r.v.k, a = []); a.push(r);
  }
  const vehicles = [], movers = [];
  for (const key of [...groups.keys()].sort()) {
    const rs = groups.get(key), prices = rs.filter(r => r.p != null).map(r => r.p);
    const last = rs[rs.length - 1], v = last.v;
    const fp = prices.length ? num(prices[0]) : null, lp = prices.length ? num(prices[prices.length - 1]) : null;
    const clash = collided.has(key);
    const moved = fp != null && lp != null && uniq(prices) > 1 && !clash;
    const o = { key, year: v.year, make: v.make, model: v.model, variant: v.variant,
      price: lp, first_price: fp, odometer_km: num(last.km), state: v.state,
      seller_type: v.seller, source: v.src, url: v.url, title: v.title, listing_id: v.id,
      detail: v.src === "quick_add" ? "brief" : "full", sightings: rs.length,
      first_day: rs[0].d, last_day: last.d, moved, clash, starred: DB.stars.has(key),
      change: moved && fp ? pyRound((lp - fp) / fp * 100, 1) : null, ph: firstPhoto(v) };
    vehicles.push(o);
    if (moved) movers.push({ key, year: o.year, variant: o.variant, first_price: fp, price: lp,
      change: o.change, first_day: o.first_day, last_day: o.last_day, url: o.url, title: o.title,
      steps: rs.filter(r => r.p != null).map(r => ({ d: r.d, v: num(r.p) })) });
  }
  const vs = vehicles.map((x, i) => [x, i]).sort((A, B) => {
    const a = A[0], b = B[0];
    if (a.last_day !== b.last_day) return a.last_day < b.last_day ? 1 : -1;
    const pa = a.price || 0, pb = b.price || 0;
    return pa !== pb ? pb - pa : A[1] - B[1];
  }).map(x => x[0]);
  return { vehicles: vs, movers: stableBy(movers, m => -Math.abs(m.change || 0)) };
}
// detail_payload's standouts: every car's distance from the middle
function standouts(DB, rows, d) {
  const pool = latestPerVehicle(prep(rows));
  const live = liveKeys(rows), out = [];
  if (!d || !d.curve || !d.curve.usable) return { rows: out, live };
  const m = d.mileage || {}, adjOk = m.usable && d.adj_curve && d.adj_curve.usable && m.slope_per_km != null;
  const { sigs } = dupSigs(pool, DB.dismissed);
  for (const r of pool) {
    const adj = adjOk ? r.p * Math.exp(m.slope_per_km * (r.km == null ? 0 : (m.median_odo - r.km))) : null;
    out.push({ key: r.v.k, z: zOf(r.p, d.curve), z_adj: adjOk ? zOf(adj, d.adj_curve) : null,
      price: num(r.p), odometer_km: num(r.km), year: r.v.year, variant: r.v.variant,
      state: r.v.state, seller_type: r.v.seller, day: r.d, live: live.keys.has(r.v.k),
      url: r.v.url, title: r.v.title, dup: r.km != null && sigs.has(r.p + "|" + r.km) });
  }
  return { rows: stableBy(out, s => s.z == null ? Infinity : s.z), live };
}

/* ------------------------------------------- explore.car_payload, ported */
function rankIn(peers, value, C) {
  const n = peers.length;
  if (value == null || n < C.CAR_MIN_PEERS)
    return { n, usable: false, reason: `only ${n} other car(s) of this trim have a price - ` +
             `${C.CAR_MIN_PEERS} are needed before a ranking means anything` };
  const cheaper = peers.filter(p => p < value).length;
  const o = { n, usable: true, rank: cheaper + 1, dearer_than: cheaper, as_count: n < C.CAR_PERCENTILE_MIN };
  if (n >= C.CAR_PERCENTILE_MIN) o.percentile = pyRound(cheaper / n * 100, 1);
  return o;
}
const poolStats = P => ({ median: num(median(P)), q25: num(quantile(P, .25)),
  q75: num(quantile(P, .75)), lo: num(minOf(P)), hi: num(maxOf(P)) });
function splitMedian(peers, get, value, minN) {
  if (value == null) return { value: null, n: 0, median: null, reason: "not recorded for this car" };
  const m = peers.filter(r => get(r) === value), n = m.length;
  if (n < minN) return { value: String(value), n, median: null,
    reason: `only ${n} other ${value} listing(s) - too few to compare` };
  return { value: String(value), n, median: num(median(m.map(r => r.p))), reason: "" };
}

// schema.py's normalisers, for a car typed in by hand
const PRICE_NONE = /negotiable|poa|p\.o\.a|price on application|contact|call|offers|swap|tba|free/i;
function parsePrice(value) {
  if (value == null) return [null, null];
  if (typeof value === "number") return value >= 100 ? [value, null] : [null, "implausible_low"];
  const s = String(value).trim();
  if (!s) return [null, null];
  let note = null;
  const low = s.toLowerCase();
  if (low.includes("drive away") || low.includes("driveaway")) note = "drive_away";
  else if (low.includes("excl") && low.includes("gov")) note = "excl_gov_charges";
  const m = /(\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?)[ ]*([kK])?/.exec(s.replace(/\$/g, " "));
  if (!m) {
    if (PRICE_NONE.test(low)) return [null, low.includes("negotia") ? "negotiable"
      : (low.includes("po") || low.includes("application")) ? "poa" : "no_price"];
    return [null, "unparseable"];
  }
  let n = parseFloat(m[1].replace(/[, ]/g, ""));
  if (m[2]) n *= 1000;
  if (low.includes("negotia") || (low.match(/\b\w+\b/g) || []).includes("ono"))
    note = note ? note + ",negotiable" : "negotiable";
  if (n < 100) return [null, "implausible_low"];
  if (n > 2000000) return [null, "implausible_high"];
  return [n, note];
}
function parseOdometer(value) {
  if (value == null) return null;
  if (typeof value === "number") return value >= 0 && value < 2000000 ? value : null;
  const s = String(value).trim();
  if (!s || ["n/a", "na", "-", "unknown"].includes(s.toLowerCase())) return null;
  const m = /(\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?)[ ]*([kK]\b)?/.exec(s);
  if (!m) return null;
  let v = parseFloat(m[1].replace(/[, ]/g, ""));
  if (m[2]) v *= 1000;
  return v >= 0 && v < 2000000 ? v : null;
}
const STATES = { nsw: "NSW", "new south wales": "NSW", vic: "VIC", victoria: "VIC",
  qld: "QLD", queensland: "QLD", wa: "WA", "western australia": "WA", sa: "SA",
  "south australia": "SA", tas: "TAS", tasmania: "TAS", act: "ACT",
  "australian capital territory": "ACT", nt: "NT", "northern territory": "NT" };
function normState(s) {
  if (!s) return null;
  const k = String(s).trim().toLowerCase();
  if (STATES[k]) return STATES[k];
  for (const t of k.split(/[,\s]+/).reverse()) if (STATES[t]) return STATES[t];
  return null;
}
function normSeller(s) {
  if (!s) return null;
  const l = String(s).toLowerCase();
  if (l.includes("dealer") || l.includes("demo")) return "dealer";
  if (l.includes("private")) return "private";
  return null;
}
function normYear(v) {
  if (v == null) return null;
  const f = Number(String(v).trim());
  if (!Number.isFinite(f) || String(v).trim() === "") return null;
  const y = Math.trunc(f);
  return y >= 1900 && y <= 2035 ? y : null;
}

// One car against the others of its trim. `fits` is the laptop's mileage
// fit for this trim: k.fit for a typed car, k.cars[key] (the fit with the
// car itself left out) for a stored one.
// explore._estimate: the asking price a car with these details would be
// expected to list at - the middle half and the 10th-90th - on the most
// specific basis the data supports (mileage curve, nearest in km, the trim)
// explore._classic_reason, word for word
const classicReason = y => `a classic (${y}): its asking price depends on condition, originality and history ` +
  "far more than on kilometres, so no expected price is given - the spread of comparable cars is shown instead";
function estimate(peers, fitRows, fitT, subject, C, what) {
  what = what || "this trim";
  if (subject.classic) return { usable: false, basis: null, reason: classicReason(subject.year), why_not_curve: null };
  const odo = subject.odometer_km, qs = [0.10, 0.25, 0.75, 0.90];
  const out = { usable: false, basis: null, reason: "", why_not_curve: null };
  const [fUsable, slope, medOdo, , , fReason] = fitT;
  let got = null;
  if (odo != null && fUsable) {
    const used = fitRows.filter(r => r.p != null && r.km != null), U = used.map(r => r.km);
    const loQ = quantile(U, C.CAR_FIT_RANGE[0]), hiQ = quantile(U, C.CAR_FIT_RANGE[1]);
    if (loQ <= odo && odo <= hiQ) {
      const base = median(used.map(r => r.p)), mid = base * Math.exp(slope * (odo - medOdo));
      const res = used.map(r => Math.log(r.p) - Math.log(base) - slope * (r.km - medOdo));
      got = ["mileage", mid, qs.map(q => mid * Math.exp(quantile(res, q))), used.length];
    } else out.why_not_curve = `${fmt0(odo)} km is outside the ${fmt0(loQ)}-${fmt0(hiQ)} km the mileage curve covers`;
  } else if (odo != null) out.why_not_curve = `no mileage curve for ${what}: ${fReason || "refused"}`;
  if (!got && odo) {
    const P = peers.filter(r => r.km != null && r.p != null && r.km > 0)
      .map((r, i) => [Math.abs(Math.log(r.km / odo)), r.p, i])
      .sort((a, b) => a[0] !== b[0] ? a[0] - b[0] : a[1] !== b[1] ? a[1] - b[1] : a[2] - b[2])
      .slice(0, C.EST_NEAREST || 12).map(x => x[1]);
    if (P.length >= C.CAR_MIN_PEERS) got = ["nearest", median(P), qs.map(q => quantile(P, q)), P.length];
  }
  if (!got && !odo) {
    const P = peers.filter(r => r.p != null).map(r => r.p);
    if (P.length >= C.CAR_MIN_PEERS) got = ["trim", median(P), qs.map(q => quantile(P, q)), P.length];
  }
  if (!got) {
    const n = peers.filter(r => r.p != null).length;
    out.reason = `only ${n} priced car(s) of ${what} - ${C.CAR_MIN_PEERS} are needed before a range means anything`;
    return out;
  }
  const [basis, mid, [wlo, lo, hi, whi], n] = got, r100 = v => num(pyRound(v / 100, 0) * 100);
  return Object.assign(out, { usable: true, basis, n, mid: r100(mid), lo: r100(lo), hi: r100(hi),
    wide_lo: r100(wlo), wide_hi: r100(whi), at_odo: num(odo) });
}
// explore._fallback_note, word for word
function fallbackNote(level, va, trimN, mk, mo, yrs, n, stored, gen, tb) {
  const first = va ? `Only ${trimN}${stored ? " other" : ""} priced ${va}`
    : stored ? "No trim is recorded for this car" : "No trim was picked";
  if (level === "trim_years")
    return `${first} - compared with all ${n} ${mk} ${mo} ${tb} cars from ${yrs[0]} to ${yrs[1]}, ` +
      "of any generation, instead. Treat it as a rough guide.";
  if (level === "generation")
    return `${first} - compared with all ${n} ${mk} ${mo} ${gen} cars, every trim of that generation, ` +
      "instead. That pools different trims, so treat it as a rough guide.";
  return `${first} - compared with all ${n} ${mk} ${mo} cars from ${yrs[0]} to ${yrs[1]} ` +
    "instead. That pools different trims, so treat it as a rough guide.";
}
function carCard(DB, mk, mo, va, opts, now) {
  const C = DB.consts;
  let label = labelOf(mk, mo, va);
  const base = { ok: true, label, gates: { min_peers: C.CAR_MIN_PEERS, percentile_min: C.CAR_PERCENTILE_MIN } };
  if (!(mk && mo)) return Object.assign(base, { usable: false, level: "too broad",
    reason: "pick a trim first - at make level the peer set would pool cars that are not comparable" });
  const key = opts.key || null;
  // the pool: the trim, or - too few priced, or no trim - the model within
  // CAR_NEAR_YEARS of the car's year (explore.car_payload)
  const kv = key ? DB.byKey.get(key) : null;
  const subjYear = key ? (kv && kv.year != null ? kv.year : null) : normYear(opts.year);
  let sub = va ? rowsOf(DB, mk, mo, va) : [];
  if (va && !sub.length) return Object.assign(base, { usable: false, reason: "no listings match that selection" });
  if (key && va && !sub.some(r => r.v.k === key)) return { ok: false, error: "that car is not in this selection" };
  const peerN = s => { const p = latestPerVehicle(prep(s)); return key ? p.filter(r => r.v.k !== key).length : p.length; };
  // the ladder (explore.car_payload): trim, the same trim of any generation
  // within the years, every trim of the generation, the model within the years
  let level = "trim", fallback = null, what = "this trim";
  const trimN = va ? peerN(sub) : 0;
  if (trimN < C.CAR_MIN_PEERS) {
    const line = rowsOf(DB, mk, mo, null);
    const ref = key ? (DB.byCar.get(key) || []) : sub;
    const firstOf = c => { for (const r of ref) { const x = r.v[c]; if (typeof x === "string" && x) return x; } return null; };
    const gen = firstOf("gen"), tb = firstOf("tb"), steps = [];
    let lo = null, hi = null;
    const inYears = r => r.v.year != null && r.v.year >= lo && r.v.year <= hi;
    if (subjYear != null) {
      lo = subjYear - C.CAR_NEAR_YEARS; hi = subjYear + C.CAR_NEAR_YEARS;
      if (va && gen && tb) steps.push(["trim_years", line.filter(r => inYears(r) && r.v.tb === tb), `${mk} ${mo} ${tb} ${lo}-${hi}`, [lo, hi]]);
    }
    if (gen) steps.push(["generation", line.filter(r => r.v.gen === gen), `${mk} ${mo} ${gen}`, null]);
    if (subjYear != null) steps.push(["years", line.filter(inYears), `${mk} ${mo} ${lo}-${hi}`, [lo, hi]]);
    for (const [lv, rows, lab, yrs] of steps) {
      const n = rows.length ? peerN(rows) : 0;
      if (n >= C.CAR_MIN_PEERS) {
        level = lv; sub = rows; what = "these cars"; label = lab; base.label = label;
        fallback = { trim: va || null, trim_n: trimN, level: lv, years: yrs, generation: gen,
                     trim_base: tb, n, note: fallbackNote(lv, va, trimN, mk, mo, yrs, n, !!key, gen, tb) };
        break;
      }
    }
  }
  if (level === "trim" && !va) return Object.assign(base, { usable: false, level: "too broad",
    reason: subjYear == null
      ? `pick a trim first - or give the year, and it is judged against ${mk} ${mo} cars within ${C.CAR_NEAR_YEARS} years of it`
      : `pick a trim first - fewer than ${C.CAR_MIN_PEERS} priced ${mk} ${mo} cars are within ${C.CAR_NEAR_YEARS} years of ${subjYear}` });
  const prepped = prep(sub), pool = latestPerVehicle(prepped);
  if (!pool.length) return Object.assign(base, { usable: false, reason: "no priced listings in this trim" });
  // pooled trims: no mileage curve (its slope would measure the trim mix)
  const POOLED = [false, null, null, 0, null, "several trims or generations are pooled, so a mileage curve would measure the mix, not wear"];
  const entry = level === "trim" ? (DB.sel[label] || {}) : {};
  const K = level === "trim" ? (entry.k || { fit: [false, null, null, 0, null, "no fit"], cars: {}, caveats: [] })
    : { fit: POOLED, cars: {}, caveats: [] };
  let subject, peers, fitRows, fitT;
  if (key) {
    const own = pool.find(r => r.v.k === key);
    if (!own) return { ok: false, error: "that car is not in this selection" };
    const v = own.v;
    subject = { key, stored: true, price: num(own.p), odometer_km: num(own.km), year: v.year,
      state: v.state, seller_type: v.seller, variant: v.variant, gearbox: v.gear == null ? null : v.gear,
      title: v.title, url: v.url, source: v.src, day: own.d };
    subject.photos = (v.ph || []).slice();
    peers = pool.filter(r => r.v.k !== key);
    fitRows = prepped.filter(r => r.v.k !== key);
    fitT = level === "trim" ? (K.cars[key] || [false, null, null, 0, null, "no fit"]) : POOLED;
  } else {
    const [p, note] = parsePrice(opts.price);
    subject = { key: null, stored: false, price: num(p), price_note: note,
      odometer_km: num(parseOdometer(opts.odometer)), year: normYear(opts.year),
      state: normState(opts.state), seller_type: normSeller(opts.seller),
      variant: null, gearbox: null, title: null, url: null, source: null, day: null };
    // no price: "what should it be asking?" - answered by `estimate`
    peers = pool; fitRows = prepped; fitT = K.fit;
  }
  subject.label = label;
  // a classic: no mileage curve and no expected price (explore.CAR_CLASSIC_AGE)
  subject.classic = !!(subject.year && (now || new Date()).getFullYear() - subject.year >= C.CAR_CLASSIC_AGE);
  if (subject.classic) fitT = [false, null, null, 0, null, "a classic: kilometres don't set its price, so no mileage curve is used"];
  const peerP = peers.map(r => r.p);
  const raw = rankIn(peerP, subject.price, C);
  const unavailable = [], notes = [];
  let odoPct = null;
  const peerOdo = peers.map(r => r.km).filter(v => v != null);
  if (subject.odometer_km != null && peerOdo.length >= C.CAR_MIN_PEERS)
    odoPct = pyRound(peerOdo.filter(v => v < subject.odometer_km).length / peerOdo.length * 100, 1);
  else if (subject.odometer_km == null)
    unavailable.push("no odometer on this car, so it cannot be placed on kilometres or mileage-adjusted");

  const [fUsable, slope, medOdo, fN, fPct, fReason] = fitT;
  const fit = { usable: !!fUsable, reason: fReason || "", n: fN || 0, pct_per_10k_km: num(fPct),
                median_odo: num(medOdo) };
  let adjusted = null, expected = null;
  if (fUsable && subject.odometer_km != null && subject.price != null) {
    const used = fitRows.filter(r => r.p != null && r.km != null);
    const U = used.map(r => r.km);
    const loQ = quantile(U, C.CAR_FIT_RANGE[0]), hiQ = quantile(U, C.CAR_FIT_RANGE[1]);
    fit.covers = [num(loQ), num(hiQ)];
    const km = subject.odometer_km;
    if (!(loQ <= km && km <= hiQ)) {
      fit.outside = true;
      unavailable.push(`at ${fmt0(km)} km this car sits outside the ${fmt0(loQ)}-${fmt0(hiQ)} km ` +
        "the fit actually covers, so no mileage-adjusted figure is shown");
    } else {
      const adjOf = (p, k) => p * Math.exp(slope * (k == null ? 0 : (medOdo - k)));
      const subjAdj = adjOf(subject.price, km), peerAdj = peers.map(r => adjOf(r.p, r.km));
      const ps = poolStats(peerAdj);
      adjusted = { price: num(subjAdj), rank: rankIn(peerAdj, subjAdj, C),
        pool_median: ps.median, pool_q25: ps.q25, pool_q75: ps.q75, pool_lo: ps.lo, pool_hi: ps.hi,
        at_odo: num(medOdo) };
      const basePrice = median(used.map(r => r.p));
      const expP = basePrice * Math.exp(slope * (km - medOdo));
      const resid = Math.log(subject.price) - Math.log(expP);
      const above = used.filter(r => (Math.log(r.p) - Math.log(basePrice) - slope * (r.km - medOdo)) > resid).length;
      expected = { price: num(pyRound(expP / 100, 0) * 100),
        gap: num(pyRound((subject.price - expP) / 100, 0) * 100),
        gap_pct: pyRound((subject.price / expP - 1) * 100, 1), n: used.length, above, of: used.length };
    }
  } else if (!fUsable) {
    unavailable.push(`no mileage adjustment: ${fReason || "refused"}`);
  }

  // the nearest real ads, on |ln(km / subject km)|, then year, then none
  let cand, basis;
  if (subject.odometer_km) {
    cand = peers.filter(r => r.km != null && r.km > 0)
      .map(r => [r, Math.abs(Math.log(r.km / subject.odometer_km))]);
    basis = "odometer";
  } else if (subject.year) {
    cand = peers.filter(r => r.v.year != null).map(r => [r, Math.abs(r.v.year - subject.year)]);
    basis = "year";
  } else { cand = peers.map(r => [r, 0]); basis = "none"; }
  cand = cand.map((x, i) => [x[0], x[1], i]).sort((a, b) =>
    a[1] !== b[1] ? a[1] - b[1] : a[0].p !== b[0].p ? a[0].p - b[0].p : a[2] - b[2])
    .slice(0, C.CAR_COMPARABLES);
  const dup = dupSigs(peers, DB.dismissed);
  const comparables = cand.map(([r]) => ({ key: r.v.k, year: r.v.year, price: num(r.p),
    odometer_km: num(r.km), state: r.v.state, seller_type: r.v.seller, day: r.d, url: r.v.url,
    title: r.v.title, dup: r.km != null && dup.sigs.has(r.p + "|" + r.km), ph: firstPhoto(r.v) }));
  const band = { basis, n: comparables.length, dup_in_pool: dup.extra,
    median: comparables.length ? num(median(comparables.map(c => c.price))) : null };
  if (band.median && subject.price) band.vs = pyRound(subject.price / band.median * 100 - 100, 1);

  const sp = {
    seller: splitMedian(peers, r => r.v.seller, subject.seller_type, C.CAR_SPLIT_MIN_N),
    state: splitMedian(peers, r => r.v.state, subject.state, C.CAR_SPLIT_MIN_N),
    year: splitMedian(peers, r => r.v.year, subject.year, C.CAR_YEAR_MIN_N),
    gearbox: splitMedian(peers, r => r.v.gear, subject.gearbox, C.CAR_SPLIT_MIN_N),
  };
  let linePool = latestPerVehicle(prep(rowsOf(DB, mk, mo, null)));
  if (key) linePool = linePool.filter(r => r.v.k !== key);
  const lineP = linePool.map(r => r.p), lineRank = rankIn(lineP, subject.price, C);
  const lineCheck = { label: `${mk} ${mo}`, rank: lineRank, median: num(median(lineP)) };
  if (raw.percentile != null && lineRank.percentile != null) {
    lineCheck.gap = pyRound(lineRank.percentile - raw.percentile, 1);
    lineCheck.notable = Math.abs(lineCheck.gap) >= C.CAR_LINE_DISAGREE;
  }

  let history = null;
  if (key) {
    const own = stableBy(sub.filter(r => r.v.k === key), r => r.d);
    const prices = own.filter(r => r.p != null).map(r => r.p);
    const clash = collidedKeys(sub).has(key);
    const fd = own[0].d, ld = own[own.length - 1].d;
    history = { sightings: own.length, days: uniq(own.map(r => r.d)), first_day: fd, last_day: ld,
      at_least_days: dayNum(ld) - dayNum(fd), first_price: prices.length ? num(prices[0]) : null,
      price: prices.length ? num(prices[prices.length - 1]) : null, clash,
      steps: own.filter(r => r.p != null).map(r => ({ d: r.d, v: num(r.p) })),
      sights: own.map(r => ({ d: r.d, price: num(r.p), odometer_km: num(r.km) })) };
    history.moved = !clash && uniq(prices) > 1 && !!prices[0];
    history.change = history.moved ? pyRound((prices[prices.length - 1] - prices[0]) / prices[0] * 100, 1) : null;
    // the trim on each capture day, this car excluded (explore._timeline)
    const one = new Map();
    for (const r of stableBy(sub.filter(r => r.p != null), r => r.d)) one.set(r.d + "|" + r.v.k, r);
    const byD = new Map();
    for (const r of one.values()) { let a = byD.get(r.d); if (!a) byD.set(r.d, a = []); a.push(r); }
    const mine = {};
    for (const r of sub) if (r.v.k === key && r.p != null) mine[r.d] = r.p;
    history.byday = [...byD.keys()].sort().map(d => {
      const P = byD.get(d).filter(r => r.v.k !== key).map(r => r.p);
      const row = { d, n: P.length, mine: mine[d] != null ? mine[d] : null };
      if (P.length) Object.assign(row, { median: num(median(P)), q25: num(quantile(P, .25)),
        q75: num(quantile(P, .75)), lo: num(minOf(P)), hi: num(maxOf(P)) });
      if (row.mine != null && P.length >= C.CAR_MIN_PEERS) {
        row.rank = P.filter(p => p < row.mine).length + 1; row.of = P.length + 1;
      }
      return row;
    });
  }
  const days = pool.map(r => r.d), last = days.reduce((a, b) => b > a ? b : a);
  const nowD = now || new Date();
  const daysSince = Math.floor((nowD.getTime() - new Date(last + "T00:00:00").getTime()) / 86400000);
  return Object.assign(base, { usable: raw.usable, level, fallback, subject,
    pool: Object.assign({ n: peerP.length, first: days.reduce((a, b) => b < a ? b : a), last,
      days_since: daysSince, stale: daysSince > C.CAR_STALE_DAYS }, poolStats(peerP)),
    raw, adjusted, expected, estimate: key ? null : estimate(peers, fitRows, fitT, subject, C, what), fit, odo_percentile: odoPct, comparables, band, splits: sp,
    line_check: lineCheck, history, caveats: K.caveats || [], unavailable, notes,
    basis: "one price per car - each car's latest sighting - so a car seen on seven capture days counts once",
    footer: "These are ASKING prices, not sale prices, and this compares numbers only. " +
            "Condition, service history, rego and colour are not in the data." });
}

/* ------------------------------------------ explore.link_payload, ported */
const SITE_ID = /\b([A-Z]{2,5}-AD-\d+)\b/i;
function normPath(u) {
  u = String(u || "").trim();
  if (!u.includes("://") && !u.startsWith("/")) u = "https://" + u;
  let path;
  try { path = new URL(u).pathname; } catch (e) {
    path = u.replace(/^[a-z]+:\/\/[^/]*/i, "").split(/[?#]/)[0];
  }
  return path.replace(/\/+$/, "").toLowerCase();
}
function findLink(DB, link) {
  link = String(link || "").trim();
  if (!link) return { ok: false, error: "paste an advert link first" };
  if (link.length > 2000 || /\s/.test(link)) return { ok: false, error: "that does not look like a single link" };
  const low = link.toLowerCase(), rules = Object.assign({ unlinked: [], words: [], paths: [] }, DB.snap.links || {});
  if (rules.unlinked.some(w => low.includes(w))) return { ok: false, error:
    "That site's listings were captured without their links, so its links cannot be " +
    "matched. Find the car's trim instead, or use Price check." };
  const ids = new Map();
  for (const r of DB.S) if (r.v.id != null && !ids.has(String(r.v.id).toUpperCase())) ids.set(String(r.v.id).toUpperCase(), r.v.k);
  const want = normPath(link), segs = link.split(/[/?#&=]/).filter(Boolean);
  let key = null, basis = null;
  for (const s of segs.slice().reverse()) {
    const hit = ids.get(s.toUpperCase());
    if (hit != null) { key = hit; basis = "the ad id in the link"; break; }
  }
  if (key == null && want) {
    for (const r of DB.S) {
      const u = r.v.u0 || r.v.url;
      if (u && normPath(u) === want) { key = r.v.k; basis = "the advert's address"; break; }
    }
  }
  const m = SITE_ID.exec(link);
  if (key == null) {
    if (!m && !rules.paths.some(p => want.startsWith(p)) && !rules.words.some(w => low.includes(w)))
      return { ok: false, error: "no ad id in that link - paste the address of the advert itself " +
                                 "(it contains the ad's id)" };
    return { ok: true, in_data: false, key: null, found: { listing_id: m ? m[1].toUpperCase() : null } };
  }
  return { ok: true, in_data: true, key, basis, vehicle: DB.byKey.get(key) };
}

/* ------------------------------------------------------- compare.py */
function compareCols(DB, labels) {
  const cols = [];
  for (const l of labels) {
    const e = DB.sel[l];
    if (!e || cols.some(c => c.label === l)) continue;
    cols.push(Object.assign({}, e.c));
  }
  const meds = cols.filter(c => !c.empty && c.median).map(c => c.median);
  const cheapest = meds.length ? Math.min(...meds) : null;
  for (const c of cols) {
    if (c.empty || !c.median) { c.rank = null; c.vs_cheapest = null; continue; }
    c.rank = 1 + meds.filter(m => m < c.median).length;
    c.vs_cheapest = cheapest ? pyRound((c.median / cheapest - 1) * 100, 1) : null;
  }
  return cols;
}

const VS = { pyRound, num, quantile, median, mean, fmt0, index, rowsOf, labelOf, prep,
  latestPerVehicle, liveKeys, collidedKeys, zOf, curveGrid, dupSigs, applyFilters, head,
  splits, byday, supply, bymonth, hist, scatter, cars, standouts, rankIn, parsePrice,
  parseOdometer, normState, normSeller, normYear, carCard, normPath, findLink, compareCols };
if (typeof module !== "undefined" && module.exports) module.exports = VS;
else root.VS = VS;
})(typeof self !== "undefined" ? self : this);
