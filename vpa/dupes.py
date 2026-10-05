"""The Duplicates page: one car stored as two identities, found and resolved.

WHAT A DUPLICATE IS HERE
A car's identity is its site ad id when the capture carried one, and a
fingerprint when it did not. Every capture before August 2026 (the v2
tracker, screenshots, pasted text) carried none, so a car captured then and
again later from a saved page is two identities that can never collide:
two price histories, and a car that seems to leave the market the day it
was re-captured. See merge.py for the full story and the rules.

THREE KINDS OF GROUP, and what this page does with each
  * PROVEN - passes every rule in merge.py. Offered for merging in one
    click. These are exactly what `python -m vpa merge-duplicates` merges.
  * FOR YOUR JUDGEMENT - fails ONLY for lack of proof: the shared odometer
    is a round figure (187,000 km), or neither identity has a site ad id so
    there is no site-issued key to keep. Nothing in the data contradicts
    them being one car. Shown side by side, with each one's sightings, how
    it was captured and a link to its advert; merged only when you say so,
    and "different cars" is remembered so the same group is not asked again.
  * NOT OFFERED - the data says they are different, or cannot be one car:
    two prices on one day, an odometer running backwards, a year or state
    conflict, no variant, or two different site ad ids (a relisted advert
    is a new listing, not a lost one). Counted, never merged here.

SAFETY - the same net as merge.py, plus undo
  * A backup before every merge and every undo.
  * Merges run through `merge._apply_merges`: one transaction, observation
    count and the full price multiset identical before and after, no
    orphan, no identity left holding two prices on one day - or rollback.
  * Every absorbed identity is logged in merge_log (its vehicle row, which
    observations moved, its star), so a merge made here is undone EXACTLY,
    under the same checks.
  * A merge request names groups by their keys. The groups are recomputed
    at merge time and a request for a group that is no longer offered in
    exactly that shape is refused - nothing merges on a stale screen.
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import links
from .merge import _apply_merges, _components, _failures, _fingerprint, _is_site_id, _label, _rows
from .store import DEFAULT_DB, Store

#: the only reasons a group may be offered for your judgement. Anything else
#: is the data contradicting "one car", and is not offered at all.
_LACKS_PROOF = {"rounded odometer", "no single site ad id"}


def _gid(keys) -> str:
    return ",".join(sorted(keys))


def _dismissed(store: Store) -> set[str]:
    return {r[0] for r in store.conn.execute("SELECT keys FROM not_duplicates")}


def dismissed_sets(db_path: str | Path = DEFAULT_DB) -> list[frozenset]:
    """Groups you said are different cars, for Explore's "duplicate?" tag."""
    with Store(db_path) as store:
        return [frozenset(k.split(",")) for k in _dismissed(store)]


def dup_sigs(df, dismissed: list[frozenset]) -> tuple[set, int]:
    """(price, odometer) readings held by more than one identity in `df`,
    leaving out groups you have said are different cars; and how many
    surplus identities that makes. Explore's tag and count, in one place."""
    keys = defaultdict(set)
    for p, o, k in zip(df["price_aud"], df["odometer_km"], df["key"]):
        if o == o and o is not None:          # NaN-safe: no odometer, no match
            keys[(p, o)].add(str(k))
    sigs, extra = set(), 0
    for sig, ks in keys.items():
        if len(ks) > 1 and not any(ks <= d for d in dismissed):
            sigs.add(sig)
            extra += len(ks) - 1
    return sigs, extra


def _survivor(comp: list[str], by_key: dict) -> str:
    """The identity that is kept. The one with a site ad id when there is
    one (the stable, site-issued key, and the one that carries the advert
    link); otherwise the one with the longest history, then the most
    recently seen, then the key - deterministic, never a coin toss."""
    site = [k for k in comp if _is_site_id(by_key[k][0]["listing_id"])]
    if len(site) == 1:
        return site[0]
    return sorted(comp, key=lambda k: (-len(by_key[k]),
                                       -_day_num(max(r["capture_date"] for r in by_key[k])),
                                       k))[0]


def _day_num(d: str) -> int:
    return int(str(d).replace("-", "")[:8] or 0)


def _groups(store: Store) -> dict:
    """Every group, sorted into proven / review / not offered. Touches nothing."""
    rows = _rows(store)
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["key"]].append(r)
    comps, evidence = _components(rows)
    gone = _dismissed(store)
    proven, review, refused, dismissed = [], [], defaultdict(int), []
    for comp in comps:
        fails = _failures(comp, by_key, evidence)
        codes = {c for c, _ in fails}
        n_site = sum(_is_site_id(by_key[k][0]["listing_id"]) for k in comp)
        if codes and not codes <= _LACKS_PROOF:
            refused[sorted(codes - _LACKS_PROOF)[0]] += 1
            continue
        if "no single site ad id" in codes and n_site > 1:
            refused["two different ad ids (a relisted advert)"] += 1
            continue
        g = {"id": _gid(comp), "keys": sorted(comp),
             "winner": _survivor(comp, by_key), "label": _label(by_key[comp[0]]),
             "why": [r for _, r in fails],
             "why_codes": [c for c, _ in fails]}
        g["absorbed"] = [k for k in g["keys"] if k != g["winner"]]
        if g["id"] in gone:
            dismissed.append(g)
        elif fails:
            review.append(g)
        else:
            proven.append(g)
    return {"proven": proven, "review": review, "dismissed": dismissed,
            "refused": dict(refused), "by_key": by_key}


# --- the header count --------------------------------------------------------
_CACHE: dict = {}
_LOCK = threading.Lock()


def _version(store: Store) -> tuple:
    q = lambda sql: tuple(store.conn.execute(sql).fetchone())
    return (q("SELECT COUNT(*), MAX(id) FROM observations"),
            q("SELECT COUNT(*) FROM vehicles"),
            q("SELECT COUNT(*), MAX(id) FROM edits"),
            q("SELECT COUNT(*) FROM not_duplicates"),
            q("SELECT COUNT(*), MAX(id), COUNT(undone_at) FROM merge_log"))


def summary(db_path: str | Path = DEFAULT_DB) -> dict:
    """{proven, review}: what the header button shows. Cached until the data
    changes, because every open page polls it."""
    with Store(db_path) as store:
        ver = (str(db_path), _version(store))
        with _LOCK:
            if _CACHE.get("ver") == ver:
                return _CACHE["val"]
        g = _groups(store)
    val = {"proven": len(g["proven"]), "review": len(g["review"])}
    with _LOCK:
        _CACHE.update(ver=ver, val=val)
    return val


# --- the page's data ---------------------------------------------------------
def _car(store: Store, key: str, rows: list[dict]) -> dict:
    v = store.conn.execute("SELECT * FROM vehicles WHERE key = ?", (key,)).fetchone()
    seen = store.sightings(key)
    how = sorted({(s["imported_at"] or "", s["source_file"] or "", s["adapter"] or "")
                  for s in seen})
    return {
        "key": key, "listing_id": v["listing_id"], "source": v["source"],
        "url": links.ad_url(v["url"], v["source"]), "title": v["raw_title"],
        "year": v["year"], "state": v["state"], "seller_type": v["seller_type"],
        "body_type": v["body_type"], "odometer_km": rows[-1]["odometer_km"] if rows else None,
        "first_seen": v["first_seen"], "last_seen": v["last_seen"],
        "site_id": _is_site_id(v["listing_id"]),
        "sightings": [{"day": s["capture_date"], "price": s["price_aud"],
                       "odometer_km": s["odometer_km"], "note": s["price_note"]}
                      for s in seen],
        "captured": [{"at": a[:16], "file": f, "adapter": _HOW.get(ad, ad or "unknown")}
                     for a, f, ad in how],
    }


#: how each import path names itself in ingest_log, in words
_HOW = {"ui": "Import page", "bulk": "Bulk upload", "quick_add": "Quick add",
        "v2db": "v2 tracker", "manual": "typed in", "html": "saved page",
        "csv": "CSV file", "json": "JSON file", "text": "pasted text"}


def payload(db_path: str | Path = DEFAULT_DB) -> dict:
    with Store(db_path) as store:
        g = _groups(store)
        by_key = g["by_key"]

        def full(grp):
            cars = [_car(store, k, sorted(by_key[k], key=lambda r: r["capture_date"]))
                    for k in grp["keys"]]
            # the readings they share - the evidence, stated
            seen = defaultdict(set)
            for k in grp["keys"]:
                for r in by_key[k]:
                    if r["price_aud"] is not None and r["odometer_km"] is not None:
                        seen[(r["price_aud"], r["odometer_km"])].add(k)
            shared = sorted([{"price": p, "odometer_km": o}
                             for (p, o), ks in seen.items() if len(ks) > 1],
                            key=lambda s: (s["price"], s["odometer_km"]))
            # What else they agree on, beyond the shared reading. Stated
            # per field as same / differs / not recorded on one, because
            # "no conflict" and "confirmed" are very different evidence.
            facts = []
            for f, name in (("year", "year"), ("state", "state"),
                            ("seller_type", "seller"), ("body_type", "body")):
                vals = [c[f] for c in cars]
                known = {str(v) for v in vals if v not in (None, "")}
                if len(known) > 1 and f == "seller_type" and any(
                        str(c["listing_id"] or "").startswith("BASE-")
                        and c["seller_type"] == "dealer" for c in cars):
                    # The May 2026 v2 captures (BASE- ids) say "dealer" for
                    # 84 of 90 cars - a default, not a reading. A mismatch
                    # against one of them is shown, but not held against it.
                    facts.append({"field": name, "is": "weak", "value": " / ".join(sorted(known))})
                elif len(known) > 1:
                    facts.append({"field": name, "is": "differs", "value": " / ".join(sorted(known))})
                elif known and len(known) == 1 and all(v not in (None, "") for v in vals):
                    facts.append({"field": name, "is": "same", "value": known.pop()})
                else:
                    facts.append({"field": name, "is": "unknown",
                                  "value": known.pop() if known else None})
            # seen on one day under both identities at the same price: the
            # same advert captured twice in one sweep by two methods
            days = defaultdict(set)
            for c in cars:
                for s in c["sightings"]:
                    days[(s["day"], s["price"])].add(c["key"])
            together = sorted({d for (d, _), ks in days.items() if len(ks) > 1})
            return {**{k: v for k, v in grp.items()}, "cars": cars, "shared": shared,
                    "facts": facts, "together": together,
                    "score": sum(f["is"] == "same" for f in facts)
                    - 2 * sum(f["is"] == "differs" for f in facts)
                    + (1 if together else 0)
                    + (1 if any(c["site_id"] for c in cars) else 0)}

        review = sorted((full(x) for x in g["review"]),
                        key=lambda x: (-x["score"], x["label"], x["id"]))
        out = {"ok": True,
               "proven": [full(x) for x in g["proven"]],
               "review": review,
               "dismissed": [full(x) for x in g["dismissed"]],
               "refused": g["refused"],
               "history": _history(store)}
    return out


def _history(store: Store) -> list[dict]:
    rows = store.conn.execute("""
        SELECT batch, MIN(ts) ts, COUNT(*) n, MAX(basis) basis,
               MAX(backup_path) backup, MAX(undone_at) undone_at
        FROM merge_log GROUP BY batch ORDER BY MIN(id) DESC LIMIT 30""").fetchall()
    return [dict(r) for r in rows]


# --- writes ------------------------------------------------------------------
def merge(db_path: str | Path = DEFAULT_DB, ids: list[str] | None = None) -> dict:
    """Merge the named groups. Each id must name a group offered RIGHT NOW,
    in exactly that shape, or nothing at all is merged."""
    ids = [str(i) for i in (ids or []) if str(i).strip()]
    if not ids:
        return {"ok": False, "error": "no groups were named"}
    with Store(db_path) as store:
        g = _groups(store)
        offered = {x["id"]: (x, "proven") for x in g["proven"]}
        offered.update({x["id"]: (x, "you confirmed") for x in g["review"]})
        stale = [i for i in ids if i not in offered]
        if stale:
            return {"ok": False, "error":
                    f"{len(stale)} of these groups are no longer offered as they "
                    f"were shown (the data changed, or they were already merged). "
                    f"Nothing was merged - reload the page and look again."}
        merges = [{"winner": offered[i][0]["winner"],
                   "absorbed": offered[i][0]["absorbed"],
                   "basis": offered[i][1] + (": " + "; ".join(offered[i][0]["why"])
                                             if offered[i][0]["why"] else "")}
                  for i in ids]
        backup = store.backup()
        batch = uuid.uuid4().hex[:12]
        done = _apply_merges(store, merges, log={
            "batch": batch, "backup": str(backup),
            "ts": datetime.now().isoformat(sep=" ", timespec="seconds")})
    return {"ok": True, "batch": batch, "groups": len(merges),
            "absorbed": sum(len(m["absorbed"]) for m in merges),
            "backup": str(backup), **done}


def dismiss(db_path: str | Path = DEFAULT_DB, gid: str = "", on: bool = True,
            note: str | None = None) -> dict:
    """Remember (or forget) that a group is DIFFERENT cars. Touches no data."""
    gid = _gid(str(gid).split(","))
    if not gid:
        return {"ok": False, "error": "no group named"}
    with Store(db_path) as store:
        if on:
            store.conn.execute(
                "INSERT OR REPLACE INTO not_duplicates (keys, ts, note) VALUES (?,?,?)",
                (gid, datetime.now().isoformat(sep=" ", timespec="seconds"), note))
        else:
            store.conn.execute("DELETE FROM not_duplicates WHERE keys = ?", (gid,))
        store.conn.commit()
    return {"ok": True}


def undo(db_path: str | Path = DEFAULT_DB, batch: str = "") -> dict:
    """Put back every identity one merge batch folded in, exactly as it was:
    its vehicle row, its observations, its star. Backed up first; one
    transaction; the price data must be identical afterwards or it rolls back."""
    from . import analyze
    with Store(db_path) as store:
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM merge_log WHERE batch = ? ORDER BY id", (batch,))]
        if not rows:
            return {"ok": False, "error": f"no merge {batch!r}"}
        if any(r["undone_at"] for r in rows):
            return {"ok": False, "error": "that merge has already been undone"}
        cols = [r[1] for r in store.conn.execute("PRAGMA table_info(vehicles)")]
        # every precondition checked BEFORE the backup and the transaction
        for r in rows:
            if store.conn.execute("SELECT 1 FROM vehicles WHERE key = ?",
                                  (r["absorbed"],)).fetchone():
                return {"ok": False, "error": f"identity {r['absorbed']} exists "
                        f"again, so this merge cannot be put back as it was"}
            if not store.conn.execute("SELECT 1 FROM vehicles WHERE key = ?",
                                      (r["winner"],)).fetchone():
                return {"ok": False, "error": f"the car it was merged into "
                        f"({r['winner']}) no longer exists"}
            ids = json.loads(r["obs_ids"])
            if ids:
                marks = ",".join("?" * len(ids))
                n = store.conn.execute(
                    f"SELECT COUNT(*) FROM observations WHERE id IN ({marks}) "
                    f"AND key = ?", (*ids, r["winner"])).fetchone()[0]
                if n != len(ids):
                    return {"ok": False, "error": f"{len(ids) - n} of the sightings "
                            f"this merge moved have since changed or gone, so it "
                            f"cannot be undone exactly"}

        backup = store.backup()
        before_obs = store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        before_veh = store.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        before_fp = _fingerprint(store)
        before_bad = analyze.collided_keys(store.frame())
        ts = datetime.now().isoformat(sep=" ", timespec="seconds")
        store.conn.isolation_level = None
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            window = {}
            for r in rows:
                window.setdefault(r["winner"], (r["winner_first"], r["winner_last"]))
                v = json.loads(r["vehicle"])
                use = [c for c in cols if c in v]
                store.conn.execute(
                    f"INSERT INTO vehicles ({','.join(use)}) VALUES "
                    f"({','.join('?' * len(use))})", [v[c] for c in use])
                ids = json.loads(r["obs_ids"])
                if ids:
                    marks = ",".join("?" * len(ids))
                    store.conn.execute(
                        f"UPDATE observations SET key = ? WHERE id IN ({marks})",
                        (r["absorbed"], *ids))
                if r["star"]:
                    s = json.loads(r["star"])
                    store.conn.execute(
                        "INSERT OR REPLACE INTO starred (key, starred_at, note) "
                        "VALUES (?,?,?)", (r["absorbed"], s.get("starred_at"), s.get("note")))
                    if r["star_new"]:
                        store.conn.execute("DELETE FROM starred WHERE key = ?",
                                           (r["winner"],))
            for w, (first, last) in window.items():
                store.conn.execute(
                    "UPDATE vehicles SET first_seen = ?, last_seen = ? WHERE key = ?",
                    (first, last, w))
            store.conn.execute(
                "UPDATE merge_log SET undone_at = ? WHERE batch = ?", (ts, batch))

            after_obs = store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            after_veh = store.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
            if after_obs != before_obs:
                raise RuntimeError(f"observation count changed: {before_obs} -> {after_obs}")
            if _fingerprint(store) != before_fp:
                raise RuntimeError("the price data itself changed")
            if after_veh != before_veh + len(rows):
                raise RuntimeError(f"vehicle count wrong: {before_veh} -> {after_veh}")
            orphans = store.conn.execute(
                "SELECT COUNT(*) FROM observations o LEFT JOIN vehicles v "
                "ON v.key = o.key WHERE v.key IS NULL").fetchone()[0]
            if orphans:
                raise RuntimeError(f"{orphans} orphaned observation(s)")
            if analyze.collided_keys(store.frame()) - before_bad:
                raise RuntimeError("an identity would hold two prices on one day")
            store.conn.execute("COMMIT")
        except Exception:
            store.conn.execute("ROLLBACK")
            raise
        finally:
            store.conn.isolation_level = ""
    return {"ok": True, "restored": len(rows), "backup": str(backup),
            "vehicles": after_veh, "observations": after_obs}


# --- the page ----------------------------------------------------------------
_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Duplicates &middot; VPA</title>
<style>
__CHROME_CSS__
 .grp{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-top:12px;
   transition:border-color .15s,box-shadow .15s}
 .grp:hover{border-color:var(--field);box-shadow:var(--shadow)}
 .grp.hl{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
 .ghead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 .ghead .nm{font-weight:600;font-size:13px}
 .ghead .rd{color:var(--ink-2);font-size:12.5px}
 .ghead .acts{margin-left:auto;display:flex;gap:6px}
 .facts{margin:6px 0 2px;display:flex;flex-wrap:wrap;gap:4px}
 .facts .tag{margin-left:0}
 .tag.unk{background:var(--card);border:1px solid var(--line);color:var(--dim)}
 .grp table{margin-top:6px;table-layout:fixed}
 .grp th{font-size:10px;color:var(--faint);padding:5px 10px}
 .grp td{vertical-align:top}
 .grp .c-r{width:64px}.grp .c-a{width:140px}.grp .c-y{width:48px}.grp .c-s{width:52px}
 .grp .c-l{width:70px}.grp .c-c{width:200px}
 .cap{font-size:11.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .sights{font-size:12px;line-height:1.55;white-space:nowrap}
 /* split screen: drop "seller" and "captured by" rather than crush the
    sightings column the decision actually rests on */
 @media (max-width:980px){
   .grp th:nth-child(5),.grp td:nth-child(5),
   .grp th:nth-child(7),.grp td:nth-child(7){display:none}
   .grp .c-a{width:118px}}
 @media (max-width:600px){.ghead .acts{margin-left:0;width:100%}}
 .why{font-size:11.5px;color:var(--dim);margin-top:4px}
 #tickbar{display:flex;align-items:center;gap:10px}
 #tickbar .flat{font-size:12px}
 .hist td{white-space:nowrap}
</style></head><body><div class="wrap">
__NAV__
<div class="pagehead"><div>
 <h1 class="pagetitle">Duplicates</h1>
 <div class="sub">The same car stored twice. Nothing merges unless you say so, and every merge can be undone.</div>
</div>__ACTIONS__</div>

<div class="card">
 <div class="ptitle">summary <span id="refusedtxt"></span></div>
 <div class="tiles" id="tiles"></div>
 <div class="hint note">A <b>duplicate</b> is two identities in one trim holding the same
  asking price at the same odometer. Every capture before August 2026 (the v2
  tracker, screenshots, pasted text) carried no ad id, so a car captured then and
  again later from a saved page was stored twice and could never be matched up.
  <b>Proven</b> groups pass every rule (an exact odometer reading, one ad id, no
  conflicting year or state, no two prices on one day, no odometer running
  backwards). <b>For your judgement</b> groups break none of those rules &mdash;
  they only lack proof, usually because the odometer is a round figure.
  <b>Not offered</b> groups are ones the data says are not one car.</div>
 <div id="msg"></div>
</div>

<div class="card" id="provenCard" style="display:none">
 <div class="cardhead"><div class="ptitle">proven &mdash; safe to merge <span id="provenspan" class="flat"></span></div>
  <button class="primary mini" id="mergeProven">Merge all proven</button></div>
 <div id="proven"></div>
</div>

<div class="card" id="reviewCard">
 <div class="cardhead"><div class="ptitle">for your judgement <span id="reviewspan" class="flat"></span></div>
  <div id="tickbar"><span class="flat" id="tickn"></span>
   <button class="mini" id="tickAll">Tick all shown</button>
   <button class="primary mini" id="mergeTicked" disabled>Merge ticked</button></div></div>
 <div class="hint note" style="margin-top:0">Strongest first: more recorded details that
  agree, a real ad id on one side, and both seen in the same sweep rank higher.
  Open the advert (<b>carsales</b> or <b>archive</b>) of the one with an ad id to check.</div>
 <div id="review"></div>
</div>

<div class="card" id="histCard" style="display:none">
 <div class="ptitle">merges made here</div>
 <table class="hist"><thead><tr><th>when</th><th>what</th><th class="num">identities folded in</th><th>backup</th><th></th></tr></thead>
  <tbody id="hist"></tbody></table>
</div>

<div class="card" id="dismCard" style="display:none">
 <div class="ptitle">you said these are different cars <span id="dismspan" class="flat"></span></div>
 <div id="dism"></div>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money=v=>v==null?"—":"$"+Math.round(v).toLocaleString();
const km=v=>v==null?"—":Math.round(v).toLocaleString()+" km";
let D=null;const TICK=new Set();
const want=new URLSearchParams(location.search).get("key");

function say(html,kind){$("msg").innerHTML=html?`<div class="${kind==="bad"?"warnbox":"okbox"}" style="margin-top:10px">${html}</div>`:"";}

function carRows(g){
  return g.cars.map(c=>{
    const keep=c.key===g.winner;
    return `<tr><td>${keep?'<span class="tag good" title="this identity is kept; the others fold into it">keep</span>'
                        :'<span class="tag">fold in</span>'}</td>
      <td>${vpaAdLinks(c.url,false,c)}<div class="cap" title="${esc(c.listing_id||"")}">${esc(c.listing_id||"no id")}</div></td>
      <td>${esc(c.year||"")}</td><td>${esc(c.state||"")}</td><td>${esc(c.seller_type||"")}</td>
      <td class="sights">${c.sightings.map(s=>`${s.day} &nbsp;<b>${money(s.price)}</b> &middot; ${km(s.odometer_km)}`).join("<br>")}</td>
      <td>${c.captured.map(x=>`<div class="cap" title="${esc(x.file)}">${esc(x.adapter)} &middot; ${esc(x.at)}</div>`).join("")}</td></tr>`;
  }).join("");
}
const WHY_SHORT={"rounded odometer":"round odometer","no single site ad id":"no site ad id"};
function facts(g){
  const f=(g.facts||[]).map(x=>x.is==="same"?`<span class="tag good">same ${x.field}: ${esc(x.value)}</span>`
    :x.is==="differs"?`<span class="tag warn">${x.field} differs: ${esc(x.value)}</span>`
    :x.is==="weak"?`<span class="tag unk" title="${esc(x.value)} - the May 2026 v2 captures record almost every car as a dealer sale (84 of 90), so this mismatch is weak evidence">${x.field} differs (weak)</span>`
    :`<span class="tag unk" title="not recorded on at least one of them">${x.field} ${x.value?esc(x.value)+" on one only":"not recorded"}</span>`);
  (g.why_codes||[]).forEach((c,i)=>f.push(`<span class="tag unk" title="not proven: ${esc((g.why||[])[i]||c)}">`
    +`not proven: ${esc(WHY_SHORT[c]||c)}</span>`));
  if(g.together&&g.together.length)f.push(`<span class="tag good" title="both identities were captured on this day at the same price - one advert captured twice">seen together ${esc(g.together[0])}</span>`);
  return `<div class="facts">${f.join("")}</div>`;
}
function group(g,kind){
  const shared=g.shared.map(s=>`${money(s.price)} at ${km(s.odometer_km)}`).join("; ");
  const acts=kind==="review"
    ?`<button class="mini" data-merge="${esc(g.id)}">Same car &mdash; merge</button>
      <button class="mini" data-diff="${esc(g.id)}">Different cars</button>`
    :kind==="dism"?`<button class="mini" data-undiff="${esc(g.id)}">Ask again</button>`:"";
  return `<div class="grp" data-keys="${esc(g.keys.join(" "))}">
    <div class="ghead">${kind==="review"?`<input type="checkbox" data-tick="${esc(g.id)}"${TICK.has(g.id)?" checked":""} title="tick to merge with the others ticked">`:""}
     <span class="nm">${esc(g.label)}</span><span class="rd">both at ${shared}</span>
     <span class="acts">${acts}</span></div>
    ${facts(g)}
    <table><thead><tr><th class="c-r"></th><th class="c-a">advert</th><th class="c-y">year</th>
     <th class="c-s">state</th><th class="c-l">seller</th><th>sightings</th><th class="c-c">captured by</th></tr></thead>
     <tbody>${carRows(g)}</tbody></table>
    ${g.why&&g.why.length&&!g.why_codes?`<div class="why">Not proven: ${g.why.map(esc).join("; ")}.</div>`:""}
  </div>`;
}
function render(){
  const d=D;
  const nRef=Object.values(d.refused||{}).reduce((a,b)=>a+b,0);
  $("tiles").innerHTML=[[d.proven.length,"proven"],[d.review.length,"for your judgement"],
    [nRef,"not offered"],[d.dismissed.length,"different cars"]]
    .map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
  $("refusedtxt").innerHTML=nRef?`&mdash; ${nRef} not offered: `
    +Object.entries(d.refused).map(([k,n])=>`${n} &times; ${esc(k)}`).join(", "):"";
  $("provenCard").style.display=d.proven.length?"":"none";
  $("provenspan").textContent=`— ${d.proven.length} group${d.proven.length===1?"":"s"}`;
  $("proven").innerHTML=d.proven.map(g=>group(g,"proven")).join("");
  $("reviewspan").textContent=`— ${d.review.length} group${d.review.length===1?"":"s"}, strongest first`;
  $("review").innerHTML=d.review.map(g=>group(g,"review")).join("")
    ||'<div class="empty">Nothing needs your judgement.</div>';
  $("dismCard").style.display=d.dismissed.length?"":"none";
  $("dismspan").textContent=`— ${d.dismissed.length}`;
  $("dism").innerHTML=d.dismissed.map(g=>group(g,"dism")).join("");
  $("histCard").style.display=d.history.length?"":"none";
  $("hist").innerHTML=d.history.map(h=>`<tr><td>${esc(h.ts)}</td>
    <td>${esc((h.basis||"").split(":")[0])}</td><td class="num">${h.n}</td>
    <td><div class="cap" title="${esc(h.backup||"")}" style="max-width:260px">${esc((h.backup||"").split(/[\\/]/).pop())}</div></td>
    <td>${h.undone_at?`<span class="flat">undone ${esc(h.undone_at)}</span>`
      :`<button class="mini" data-undo="${esc(h.batch)}">Undo</button>`}</td></tr>`).join("");
  wire();tickCount();
}
function tickCount(){
  const live=new Set((D.review||[]).map(g=>g.id));
  [...TICK].forEach(t=>{if(!live.has(t))TICK.delete(t);});
  $("tickn").textContent=TICK.size?`${TICK.size} ticked`:"";
  $("mergeTicked").disabled=!TICK.size;
  $("mergeTicked").textContent=TICK.size?`Merge ${TICK.size} ticked`:"Merge ticked";
}
async function post(url,body){
  vpaBusy(true);
  try{const r=await fetch(url,{method:"POST",body:JSON.stringify(body)});return await r.json();}
  catch(e){return {ok:false,error:String(e)};}
  finally{vpaBusy(false);}
}
async function doMerge(ids){
  const r=await post("/api/dupes/merge",{ids});
  if(!r.ok){say(esc(r.error),"bad");return;}
  ids.forEach(i=>TICK.delete(i));
  await load();vpaStatus();
  say(`Merged ${r.groups} group(s), folding in ${r.absorbed} identit${r.absorbed===1?"y":"ies"}.
    No price was added, changed or removed. Backup: <code>${esc(r.backup)}</code>. It can be undone under <b>merges made here</b>.`);
}
function wire(){
  document.querySelectorAll("[data-merge]").forEach(b=>b.onclick=()=>doMerge([b.dataset.merge]));
  document.querySelectorAll("[data-diff]").forEach(b=>b.onclick=async()=>{
    const r=await post("/api/dupes/dismiss",{id:b.dataset.diff,on:true});
    if(!r.ok){say(esc(r.error),"bad");return;}
    await load();vpaStatus();
    say("Noted as different cars &mdash; not offered again unless another identity joins them. Nothing in your data was changed.");});
  document.querySelectorAll("[data-undiff]").forEach(b=>b.onclick=async()=>{
    await post("/api/dupes/dismiss",{id:b.dataset.undiff,on:false});await load();vpaStatus();});
  document.querySelectorAll("[data-undo]").forEach(b=>b.onclick=async()=>{
    if(!confirm("Undo this merge? Every identity it folded in is put back exactly as it was. A backup is taken first."))return;
    const r=await post("/api/dupes/undo",{batch:b.dataset.undo});
    if(!r.ok){say(esc(r.error),"bad");return;}
    await load();vpaStatus();
    say(`Undone: ${r.restored} identit${r.restored===1?"y":"ies"} put back as they were. Backup: <code>${esc(r.backup)}</code>.`);});
  document.querySelectorAll("[data-tick]").forEach(c=>c.onchange=()=>{
    c.checked?TICK.add(c.dataset.tick):TICK.delete(c.dataset.tick);tickCount();});
}
$("mergeProven").onclick=()=>{
  if(!confirm(`Merge all ${D.proven.length} proven group(s)? A backup is taken first and it can be undone.`))return;
  doMerge(D.proven.map(g=>g.id));};
$("mergeTicked").onclick=()=>{
  if(!confirm(`Merge the ${TICK.size} ticked group(s)? You are confirming each one is a single car. A backup is taken first and it can be undone.`))return;
  doMerge([...TICK]);};
$("tickAll").onclick=()=>{D.review.forEach(g=>TICK.add(g.id));render();};

async function load(){
  vpaBusy(true);
  try{D=await fetch("/api/dupes").then(r=>r.json());}
  catch(e){D=null;say("Could not load: "+esc(e),"bad");return;}
  finally{vpaBusy(false);}
  if(!D.ok){say(esc(D.error||"failed"),"bad");return;}
  render();
}
load().then(()=>{
  if(!want||!D)return;
  const el=[...document.querySelectorAll(".grp")].find(g=>g.dataset.keys.split(" ").includes(want));
  if(el){el.classList.add("hl");vpaReveal(el);}
  else say("That car is not in any group offered here &mdash; it may already be merged or marked as different cars, or the data says its twin is a different car.");
});
__CHROME_JS__
</script></div></body></html>
"""

from .ui import chrome as _chrome   # noqa: E402

PAGE = _chrome(_PAGE, "")
