"""The Duplicates page (vpa/dupes.py): sorting, merging, undo, "different cars".

    python -m tests.check_dupes      (from vehicle-price-analysis/)

A throwaway database is seeded with one group of every kind:
  * PROVEN     - passes every rule in merge.py
  * FOR REVIEW - lacks proof only (a round odometer; no site ad id at all)
  * NOT OFFERED - contradicted (a year conflict; two different ad ids)
and the checks prove: each lands in the right list; a merge touches no price
datum and is logged; undo puts every table back EXACTLY (vehicle rows,
which observation points where, the star that moved with the car); a stale
request merges nothing; "different cars" hides a group without touching
data, and Explore's "duplicate?" tag obeys it; and the status poll that
lights the header button reports the count.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from vpa import dupes
from vpa.merge import _fingerprint
from vpa.schema import make_record
from vpa.store import Store

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "dupes_work"
DB = WORK / "dupes_test.db"
D1, D2, D3 = "2026-05-10", "2026-06-14", "2026-07-19"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def seed() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    raw = []

    def car(tag, lid, day, price, odo, **kw):
        raw.append({"make": "Ford", "model": "Falcon", "variant": tag,
                    "listing_id": lid, "capture_date": day, "price": price,
                    "odometer": odo, "year": kw.pop("year", 2013),
                    "state": kw.pop("state", "NSW"), **kw})

    car("Proven", "EXA-AD-1001", D2, 18000, 167_431)
    car("Proven", "EXA-AD-1001", D3, 17500, 167_431)
    car("Proven", None, D1, 18000, 167_431, state=None)
    car("Round", "EXA-AD-3003", D2, 15000, 150_000)
    car("Round", None, D1, 15000, 150_000)
    car("NoId", "IMG-bbbb2222", D1, 16000, 121_559)
    car("NoId", None, D2, 16000, 121_559)
    car("NoId", None, D3, 15500, 121_900)     # a third, unrelated NoId car
    car("YearClash", "EXA-AD-4004", D2, 21000, 143_216, year=2012)
    car("YearClash", None, D1, 21000, 143_216, year=2015)
    car("TwoIds", "EXA-AD-5005", D1, 19000, 132_887)
    car("TwoIds", "EXB-AD-5006", D2, 19000, 132_887)
    recs = [r for r in (make_record(d, source="examplecars") for d in raw) if r]
    with Store(DB) as store:
        store.ingest(recs, source_file="(dupes-test)", adapter="test")


def snapshot() -> dict:
    """Every table, in full, as plain data - what undo must restore."""
    c = sqlite3.connect(DB)
    out = {t: sorted(map(tuple, c.execute(f"SELECT * FROM {t}")))
           for t in ("vehicles", "observations", "starred")}
    c.close()
    return out


def key_of(variant: str, with_id: bool) -> str:
    with Store(DB) as store:
        rows = store.conn.execute(
            "SELECT key, listing_id FROM vehicles WHERE variant = ?", (variant,)).fetchall()
    for k, lid in rows:
        if bool(lid and lid.startswith(("EXA-AD-", "EXB-AD-", "IMG-"))) == with_id:
            return k
    raise KeyError(variant)


def labels(groups) -> set:
    return {g["label"].split()[-1] for g in groups}


def main() -> int:
    seed()
    print("\n== each group lands in the right list ==")
    p = dupes.payload(DB)
    check("the proven group is offered as proven", labels(p["proven"]) == {"Proven"},
          str(labels(p["proven"])))
    check("round-odometer and no-ad-id groups are offered for your judgement",
          labels(p["review"]) == {"Round", "NoId"}, str(labels(p["review"])))
    check("a year conflict and two different ad ids are NOT offered",
          sum(p["refused"].values()) == 2
          and "year conflicts" in p["refused"]
          and any("relisted" in k for k in p["refused"]), str(p["refused"]))
    rnd = next(g for g in p["review"] if g["label"].endswith("Round"))
    check("the one with the site ad id is the one kept",
          rnd["winner"] == key_of("Round", True), rnd["winner"])
    check("the shared reading is stated as the evidence",
          rnd["shared"] == [{"price": 15000.0, "odometer_km": 150000.0}], str(rnd["shared"]))
    noid = next(g for g in p["review"] if g["label"].endswith("NoId"))
    check("the unrelated third NoId car is not pulled into the group",
          len(noid["keys"]) == 2, f"{len(noid['keys'])} identities")
    check("the header count matches the lists",
          dupes.summary(DB) == {"proven": 1, "review": 2}, str(dupes.summary(DB)))

    print("\n== a merge touches no price datum, and undo restores everything exactly ==")
    absorbed = key_of("Round", False)
    with Store(DB) as store:
        store.conn.execute("INSERT INTO starred (key, starred_at, note) VALUES (?,?,?)",
                           (absorbed, "2026-07-01 10:00:00", "watching this one"))
        store.conn.commit()
        fp = _fingerprint(store)
    before = snapshot()
    r = dupes.merge(DB, [rnd["id"]])
    check("the merge succeeds and folds in one identity",
          r.get("ok") and r["absorbed"] == 1, str({k: r.get(k) for k in ("ok", "absorbed", "error")}))
    after = snapshot()
    with Store(DB) as store:
        check("no price, date, odometer or note changed", _fingerprint(store) == fp, "multiset identical")
        moved = store.conn.execute("SELECT COUNT(*) FROM observations WHERE key = ?",
                                   (rnd["winner"],)).fetchone()[0]
        log = store.conn.execute("SELECT COUNT(*), MAX(basis) FROM merge_log").fetchone()
    check("the absorbed car's sighting now belongs to the kept one", moved == 2, f"{moved} sightings")
    check("the star moved with the car",
          any(s[0] == rnd["winner"] for s in after["starred"])
          and not any(s[0] == absorbed for s in after["starred"]), str(after["starred"]))
    check("the merge is logged, and says it was your judgement",
          log[0] == 1 and log[1].startswith("you confirmed"), str(tuple(log)))
    check("the page shows it under merges made here",
          len(dupes.payload(DB)["history"]) == 1, "1 batch")
    u = dupes.undo(DB, r["batch"])
    check("undo succeeds", u.get("ok") and u["restored"] == 1, str(u.get("error") or u["restored"]))
    check("undo puts every table back EXACTLY as before the merge",
          snapshot() == before, "vehicles, observations and starred identical")
    check("undoing the same merge twice is refused",
          not dupes.undo(DB, r["batch"]).get("ok"), "already undone")

    print("\n== nothing merges on a stale screen ==")
    before = snapshot()
    r = dupes.merge(DB, [rnd["id"], "not-a-group"])
    check("a request naming a group no longer offered merges NOTHING",
          not r.get("ok") and snapshot() == before, r.get("error", "")[:60])
    r = dupes.merge(DB, ["x,y"])
    check("a made-up group is refused", not r.get("ok"), "refused")

    print("\n== 'different cars' hides a group, and changes no data ==")
    before = snapshot()
    dupes.dismiss(DB, noid["id"], on=True)
    p2 = dupes.payload(DB)
    check("the group leaves the review list and is listed as different cars",
          labels(p2["review"]) == {"Round"} and labels(p2["dismissed"]) == {"NoId"},
          f"review {labels(p2['review'])}, dismissed {labels(p2['dismissed'])}")
    check("no vehicle, sighting or star changed", snapshot() == before, "identical")
    check("the header count drops with it", dupes.summary(DB)["review"] == 1, str(dupes.summary(DB)))
    import pandas as pd
    with Store(DB) as store:
        df = store.frame()
    nd = df[df["variant"] == "NoId"]
    sig_before, _ = dupes.dup_sigs(nd, [])
    sig_after, extra = dupes.dup_sigs(nd, dupes.dismissed_sets(DB))
    check("Explore's 'duplicate?' tag obeys it",
          sig_before and not sig_after and extra == 0, f"{len(sig_before)} -> {len(sig_after)}")
    dupes.dismiss(DB, noid["id"], on=False)
    check("'ask again' brings it back", labels(dupes.payload(DB)["review"]) == {"Round", "NoId"}, "back")

    print("\n== merging the proven group through the page ==")
    pr = dupes.payload(DB)["proven"][0]
    r = dupes.merge(DB, [pr["id"]])
    check("proven merge succeeds", r.get("ok") and r["absorbed"] == 1, str(r.get("error") or r["absorbed"]))
    check("afterwards nothing proven remains", dupes.summary(DB)["proven"] == 0, str(dupes.summary(DB)))

    print("\n== the header button is fed by the status poll ==")
    from vpa.importui import status_payload
    s = status_payload(DB)
    check("status reports the duplicate count", s.get("dupes") == dupes.summary(DB), str(s.get("dupes")))

    fails = [n for ok, n in RESULTS if not ok]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
