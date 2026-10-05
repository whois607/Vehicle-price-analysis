"""Stars from phones (vpa/phoneinbox.py, starred.apply_phone_ops, the
listener's POST /api/stars, phone._apply_phone_stars).

    python -m tests.check_phone_stars      (from vehicle-price-analysis/)

The first thing a phone can change on the laptop, so this checks that it
can change exactly one thing - the starred list - and nothing else:

  * a request is checked field by field; anything odd is refused whole;
  * a resend of the same tap is recognised, never applied twice; applying
    the same request again changes nothing;
  * the newer action wins: an unstar tapped BEFORE the laptop starred the
    car again leaves the star; a star for a car merged away lands on the
    car it was merged into; a star for a car gone is reported, not invented;
  * every table but `starred` is byte-identical afterwards, a failing batch
    changes nothing at all, and the database is backed up before the first
    change a phone ever makes (and only then);
  * an unreadable inbox is never rewritten from nothing;
  * over the network: only a paired phone, only while "phones can star" is
    on, only small bodies - and each phone hears only its own outcomes.

Throwaway database, settings and inbox only. ASCII output; exit 0 only if
every check passes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from vpa import phone, phonecfg, phoneinbox, phonesrv, starred
from vpa.store import Store
from tests.check_phone import free_port, http, records

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "phone_stars_work"
DB = WORK / "stars_test.db"
RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def tables_but_starred(db: Path) -> str:
    """A hash of every row of every table except `starred`."""
    h = hashlib.sha256()
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name != 'starred' ORDER BY name"):
            h.update(t.encode())
            for row in con.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                h.update(repr(row).encode())
    finally:
        con.close()
    return h.hexdigest()[:16]


def stars(db: Path) -> dict:
    with Store(db) as st:
        return {r[0]: (r[1], r[2]) for r in st.conn.execute(
            "SELECT key, starred_at, note FROM starred")}


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    phonecfg.SETTINGS = WORK / "phone.json"
    phonecfg.OUT = WORK / "phone_out"
    with Store(DB) as st:
        st.ingest(records(5), source_file="(may)", adapter="test")
        keys = [r[0] for r in st.conn.execute("SELECT key FROM vehicles ORDER BY key")]
    A, B, C, D, E = keys[:5]
    now = datetime.now().replace(microsecond=0)
    real_inbox = phonecfg.ROOT / "data" / "phone_stars.json"
    real_before = real_inbox.read_bytes() if real_inbox.exists() else None

    print("\n== what a phone may send ==")
    good = {"id": "abc12345xyz", "k": A, "on": True, "at": ms(now)}
    bad = [dict(good, id="x"), dict(good, id="has space 123"), dict(good, k=""),
           dict(good, k="a'; DROP TABLE starred;--"), dict(good, on="true"), dict(good, on=1),
           dict(good, at="yesterday"), dict(good, at=True),
           dict(good, at=ms(now - timedelta(days=400))), "not an object", None]
    check("anything odd is refused: ids, keys, a 'true' that is not true, dates",
          phoneinbox.clean_op(good) is not None
          and all(phoneinbox.clean_op(b) is None for b in bad),
          f"{len(bad)} malformed requests refused, the good one kept")
    fut = phoneinbox.clean_op(dict(good, at=ms(now + timedelta(days=3)), extra="x", note="hi"))
    check("a phone clock in the future is clamped to now; only four fields kept",
          fut and set(fut) == {"id", "k", "on", "at"}
          and fut["at"] <= datetime.now().isoformat(sep=" ", timespec="seconds"),
          f"{fut}")

    print("\n== the inbox (the listener's side) ==")
    phonecfg.set_options(enabled=True)
    p = phonecfg.start_pairing()
    tok1 = phonecfg.redeem(p["code"], "Phone one")["token"]
    p = phonecfg.start_pairing()
    tok2 = phonecfg.redeem(p["code"], "Phone two")["token"]
    dev1, dev2 = phonecfg.device_for(tok1), phonecfg.device_for(tok2)
    t0 = now - timedelta(minutes=30)
    r1 = phoneinbox.receive(dev1, [{"id": "op1aaaaaaaa", "k": A, "on": True, "at": ms(t0)}], [])
    r1b = phoneinbox.receive(dev1, [{"id": "op1aaaaaaaa", "k": A, "on": True, "at": ms(t0)}], [])
    check("a resend of the same tap is received, but queued once",
          r1["accepted"] == ["op1aaaaaaaa"] == r1b["accepted"] and len(phoneinbox.pending()) == 1,
          f"queue {len(phoneinbox.pending())}")
    try:
        phoneinbox.receive(dev1, [dict(good, id=f"many{i:08d}") for i in range(26)], [])
        many = "accepted"
    except ValueError as e:
        many = str(e)
    try:
        phoneinbox.receive(dev1, [good, dict(good, id="bad")], [])
        mixed = "accepted"
    except ValueError as e:
        mixed = str(e)
    check("too many at once, or one bad among good ones: the whole request refused",
          "at most" in many and "not understood" in mixed and len(phoneinbox.pending()) == 1,
          f"{many}; {mixed}")

    print("\n== applying them (the starred table only) ==")
    with Store(DB) as st:
        # B: starred on the laptop with a note, an hour before the phone's unstar
        st.conn.execute("INSERT INTO starred VALUES (?,?,?)",
                        (B, (now - timedelta(hours=1)).isoformat(sep=" "), "check the rust"))
        # C: starred on the laptop AFTER the phone's (older) unstar
        st.conn.execute("INSERT INTO starred VALUES (?,?,NULL)", (C, now.isoformat(sep=" ")))
        # a star whose car has left the data
        st.conn.execute("INSERT INTO starred VALUES ('gone0000car', '2026-05-01 10:00:00', NULL)")
        # E was merged into D on the Duplicates page (D survives)
        st.conn.execute("INSERT INTO merge_log (batch, ts, winner, absorbed, vehicle, obs_ids) "
                        "VALUES ('b1', '2026-06-01', ?, 'mergedAwayKey', '{}', '[]')", (D,))
        st.conn.commit()
    others0 = tables_but_starred(DB)
    phoneinbox.receive(dev1, [
        {"id": "op2bbbbbbbb", "k": B, "on": False, "at": ms(now - timedelta(minutes=5))},
        {"id": "op3cccccccc", "k": C, "on": False, "at": ms(now - timedelta(minutes=10))},
        {"id": "op4dddddddd", "k": "noSuchCar1", "on": True, "at": ms(now)},
        {"id": "op5eeeeeeee", "k": "gone0000car", "on": False, "at": ms(now)},
        {"id": "op6ffffffff", "k": "mergedAwayKey", "on": True, "at": ms(now)}], [])
    phoneinbox.receive(dev2, [{"id": "op7gggggggg", "k": A, "on": True, "at": ms(now)}], [])
    backups = DB.parent / "backups"
    phone.HOST.db = str(DB)
    phone._apply_phone_stars()
    after = stars(DB)
    done = {o["id"]: o for o in json.loads(phoneinbox.path().read_text(encoding="utf-8"))["done"]}
    r = {i: done[i]["r"] for i in done}
    check("a star lands, with the time it was tapped",
          r["op1aaaaaaaa"] == "applied" and after.get(A, ("",))[0] == t0.isoformat(sep=" "),
          f"A {after.get(A)}")
    check("the same star from a second phone changes nothing",
          r["op7gggggggg"] == "already", r["op7gggggggg"])
    check("an unstar newer than the laptop's star removes it, and its note is kept in the log",
          r["op2bbbbbbbb"] == "applied" and B not in after
          and done["op2bbbbbbbb"]["note"] == "check the rust", done["op2bbbbbbbb"]["note"])
    check("an unstar OLDER than the laptop's star leaves it: the newer action wins",
          r["op3cccccccc"] == "kept" and C in after, done["op3cccccccc"]["why"])
    check("a star for a car not in the data is reported, not invented",
          r["op4dddddddd"] == "gone" and "noSuchCar1" not in after, r["op4dddddddd"])
    check("a starred car that left the data can still be unstarred from the phone",
          r["op5eeeeeeee"] == "applied" and "gone0000car" not in after, r["op5eeeeeeee"])
    check("a star for a car merged away lands on the car it was merged into",
          r["op6ffffffff"] == "applied" and D in after and "mergedAwayKey" not in after,
          f"-> {done['op6ffffffff']['key'] == D}")
    check("every table but `starred` is unchanged", tables_but_starred(DB) == others0, others0)
    bk = sorted(backups.glob("*.db")) if backups.exists() else []
    check("the database was backed up before the first change a phone made",
          len(bk) == 1 and json.loads(phoneinbox.path().read_text(encoding="utf-8"))["backup"] == str(bk[0]),
          bk[0].name if bk else "no backup")
    check("the queue is empty once applied", phoneinbox.pending() == [], "0 waiting")

    print("\n== again, and when things go wrong ==")
    before = stars(DB)
    res = starred.apply_phone_ops(DB, [{"id": "op1aaaaaaaa", "k": A, "on": True,
                                        "at": t0.isoformat(sep=" ")}])
    check("applying a request a second time (a crash before it was logged) changes nothing",
          res[0]["r"] == "already" and stars(DB) == before, res[0]["r"])
    try:
        starred.apply_phone_ops(DB, [{"id": "x1x1x1x1", "k": E, "on": True, "at": now.isoformat(sep=" ")},
                                     {"id": "x2x2x2x2", "k": A}])         # broken: no "on"
        broke = "applied"
    except KeyError:
        broke = "raised"
    check("a batch that fails part-way changes nothing at all",
          broke == "raised" and stars(DB) == before and E not in stars(DB), broke)
    phoneinbox.receive(dev1, [{"id": "op8hhhhhhhh", "k": E, "on": True, "at": ms(now)}], [])
    phone._apply_phone_stars()
    check("a later change makes no second backup",
          len(list(backups.glob("*.db"))) == 1 and E in stars(DB), f"{len(list(backups.glob('*.db')))} backup")
    raw = phoneinbox.path().read_bytes()
    phoneinbox.path().write_text("{not json", encoding="utf-8")
    try:
        phoneinbox.receive(dev1, [dict(good, id="zz99zz99zz")], [])
        dmg = "accepted"
    except OSError:
        dmg = "refused"
    check("a damaged inbox is never rewritten from nothing",
          dmg == "refused" and phoneinbox.path().read_text(encoding="utf-8") == "{not json"
          and phoneinbox.pending() == [], dmg)
    phoneinbox.path().write_bytes(raw)

    print("\n== over the network ==")
    port = free_port()
    srv = phonesrv.start(port, lan=False)
    try:
        def post(token, body):
            c, _, b = http(port, "/api/stars", token=token, method="POST", body=body)
            try:
                return c, json.loads(b)
            except ValueError:
                return c, {}
        c0, _ = post(None, {"ops": [good]})
        cbad, _ = post("not-a-token", {"ops": [good]})
        check("no token, or a wrong one: refused", c0 == 401 and cbad == 401, f"{c0}, {cbad}")
        c, d = post(tok1, {"ops": [{"id": "net1aaaaaa", "k": A, "on": False, "at": ms(datetime.now())}],
                           "ask": ["op1aaaaaaaa", "op7gggggggg"]})
        check("a paired phone's change is queued, and it hears only its OWN earlier outcomes",
              c == 200 and d["accepted"] == ["net1aaaaaa"] and d["results"]["op1aaaaaaaa"]["r"] == "applied"
              and "op7gggggggg" not in d["results"] and d["waiting"] == [],
              f"{c}, results for {sorted(d.get('results', {}))}")
        phonecfg.set_options(phone_stars=False)
        c, d = post(tok1, {"ops": [{"id": "net2bbbbbb", "k": A, "on": True, "at": ms(datetime.now())}]})
        phonecfg.set_options(phone_stars=True)
        c4, _ = post(tok1, {"ops": [{"id": "bad", "k": A, "on": True, "at": 1}]})
        c5, _, _ = http(port, "/api/stars", token=tok1, method="POST", body={"ops": [], "pad": "x" * 9000})
        check("switched off on the laptop: refused, and said why; bad or huge bodies refused",
              c == 403 and d.get("off") is True and c4 == 400 and c5 == 413
              and not any(o["id"] == "net2bbbbbb" for o in phoneinbox.pending()),
              f"off {c}, bad {c4}, huge {c5}")
        phone._apply_phone_stars()
        check("...and the queued unstar is applied by the supervisor", A not in stars(DB),
              "A unstarred")
    finally:
        srv.shutdown()
        srv.server_close()

    with Store(DB) as st:
        ok = st.conn.execute("PRAGMA integrity_check").fetchone()[0]
    check("the database is intact", ok == "ok", ok)
    real_after = real_inbox.read_bytes() if real_inbox.exists() else None
    check("the real data/phone_stars.json was never touched", real_after == real_before,
          "untouched" if real_after == real_before else "CHANGED")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
