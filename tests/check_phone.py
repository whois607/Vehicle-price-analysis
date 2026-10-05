"""Phone sync (vpa/phone.py, phonecfg.py, phonesrv.py): the laptop side.

    python -m tests.check_phone      (from vehicle-price-analysis/)

Everything against a throwaway database, a throwaway settings file and a
throwaway output folder - never data/phone.json or output/phone/:

  * the phone copy is read through a connection SQLite will not let write,
    and building it leaves the database byte-for-byte unchanged;
  * it holds every sighting exactly, and the same forecasts the workbook has;
  * it is rebuilt only when the data (or the starred list) changed, keeps
    the newest two files, and its meta matches the file;
  * pairing: codes expire, five wrong guesses void one, tokens are stored
    only as hashes, removing a phone locks it out, and concurrent changes to
    the settings file are not lost;
  * the listener answers only paired phones, only from private addresses,
    hands out the copy (or "not modified"), locks out guessing, and listens
    on 127.0.0.1 unless home Wi-Fi is switched on;
  * the listener cannot reach the database: nothing it imports can open it;
  * the app starts phone sync only for the real database, never a scratch one.

ASCII output; exit 0 only if all checks pass.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from vpa import phone, phonecfg, phonesrv
from vpa.schema import make_record
from vpa.store import Store, connect_readonly, read_frame  # noqa: F401

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "synthetic" / "phone_work"
DB = WORK / "phone_test.db"
PKG = ROOT.parent / "vpa"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def records(month: int, n: int = 12, start: int = 0):
    """n Falcons a variant, captured in 2026-<month>; the same ad ids recur
    across months with a falling price, so there is a real history."""
    out = []
    for var, base in (("XR6", 16000), ("XR8", 30000)):
        for i in range(start, start + n):
            lid = f"SSE-AD-{9000 + i}{1 if var == 'XR6' else 2}"
            out.append(make_record({
                "listing_id": lid, "make": "Ford", "model": "Falcon", "variant": var,
                "year": 2012 + i % 4, "price": base - i * 400 - month * 120,
                "odometer": 90000 + i * 9000 + month * 1500, "state": "NSW",
                "seller_type": "private" if i % 2 else "dealer",
                "url": f"/cars/details/ford-falcon/{lid}/",
                "capture_date": f"2026-{month:02d}-10"}, source="carsales"))
    return out


def http(port, path, token=None, method="GET", body=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def vpa_imports(path: Path) -> set[str]:
    """Modules of the vpa package a file imports (relative or absolute)."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                out.add(node.module.split(".")[0])
            elif node.level:
                out.update(a.name for a in node.names)
            elif node.module and node.module.startswith("vpa"):
                out.add((node.module.split(".") + [""])[1] or node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name.split(".")[1] for a in node.names
                       if a.name.startswith("vpa."))
    return out


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    phonecfg.SETTINGS = WORK / "phone.json"
    phonecfg.OUT = WORK / "phone_out"
    with Store(DB) as st:
        for m in (5, 6, 7, 8):
            st.ingest(records(m), source_file=f"(month {m})", adapter="test")
        st.conn.execute("INSERT INTO starred (key, starred_at, note) VALUES "
                        "((SELECT key FROM vehicles LIMIT 1), '2026-08-11 09:00:00', 'look')")
        st.conn.commit()

    print("\n== reading without the power to write ==")
    with Store(DB) as st:
        full = st.frame()
    ro = read_frame(DB)
    check("the read-only frame is exactly Store.frame()", ro.equals(full),
          f"{len(ro)} rows, {len(ro.columns)} columns")
    conn = connect_readonly(DB)
    try:
        conn.execute("DELETE FROM observations")
        refused = False
    except sqlite3.OperationalError as e:
        refused = "readonly" in str(e).lower()
    finally:
        conn.close()
    check("SQLite itself refuses a write on that connection", refused,
          "DELETE FROM observations -> attempt to write a readonly database")
    ghost = WORK / "no such.db"
    try:
        connect_readonly(ghost)
        made = True
    except FileNotFoundError:
        made = ghost.exists()
    check("...and a missing database is an error, not a new empty file",
          not made, "nothing created")

    print("\n== building the phone copy ==")
    before = sha(DB)
    m1 = phone.build_snapshot(DB)
    snapf = phonecfg.snapshot_path(m1)
    check("the build leaves the database byte-for-byte unchanged",
          sha(DB) == before, f"sha256 {before}")
    with sqlite3.connect(DB) as c:
        nv = c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        no = c.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        obs = {(r[0], r[1], r[2], r[3]) for r in c.execute(
            "SELECT key, capture_date, price_aud, odometer_km FROM observations")}
    snap = json.loads(gzip.decompress(snapf.read_bytes()))
    check("it holds every car and sighting the database has",
          m1["built"] and snap["counts"]["vehicles"] == nv
          and snap["counts"]["sightings"] == no,
          f"{nv} cars, {no} sightings, {m1['bytes']} bytes gzip")
    keys = [v[0] for v in snap["vehicles"]["rows"]]
    got = {(keys[s[0]], s[1], s[2], s[3]) for s in snap["sightings"]["rows"]}
    want = {(k, d[:10], round(p) if p is not None else None,
             round(o) if o is not None else None) for k, d, p, o in obs}
    check("...each one exactly: same car, day, price and odometer", got == want,
          f"{len(got & want)} of {len(want)} identical")
    check("the meta's hash and size describe the file", m1["sha256"] ==
          hashlib.sha256(snapf.read_bytes()).hexdigest() and m1["bytes"] == snapf.stat().st_size,
          m1["sha256"][:16])
    # the laptop's own Explore and Compare, computed here independently
    from vpa import compare, explore
    sel = snap["sel"]
    agree = []
    for label in ("Ford", "Ford Falcon", "Ford Falcon XR6", "Ford Falcon XR8"):
        mk, mo, va = (label.split(" ") + [None, None])[:3]
        d = explore.detail_payload(DB, mk, mo, va)
        c = compare._one(explore._frame(DB), label, compare.selection_index(
            explore._frame(DB))[label], 3)
        e = sel.get(label, {})
        agree.append(e.get("d", {}).get("head") == json.loads(json.dumps(d["head"]))
                     and e["d"]["forecast"] == json.loads(json.dumps(d["forecast"]))
                     and e["d"]["bymonth"] == json.loads(json.dumps(d["bymonth"]))
                     and e["c"]["median"] == c["median"] and e["c"]["fc_next"] == c["fc_next"])
    check("every selection carries the laptop's own Explore and Compare results",
          all(agree) and len(sel) == len(compare.selection_index(explore._frame(DB))),
          f"{sum(agree)} of {len(agree)} spot-checked identical; {len(sel)} selections")
    xr6 = sel["Ford Falcon XR6"]
    check("...with a real forecast and mileage fit where the data allows",
          xr6["d"]["forecast"]["method"] != "insufficient" and xr6["d"]["mileage"]["usable"]
          and xr6["k"]["fit"][0] and len(xr6["k"]["cars"]) == 12,
          f"XR6: {xr6['d']['forecast']['method']}, {xr6['d']['mileage']['pct_per_10k_km']}% "
          f"per 10,000 km, a leave-one-out fit for each of {len(xr6['k']['cars'])} cars")

    t = snapf.stat().st_mtime_ns
    m2 = phone.build_snapshot(DB)
    check("nothing changed -> nothing written", m2["built"] is False
          and m2["version"] == m1["version"] and snapf.stat().st_mtime_ns == t
          and len(list(phonecfg.OUT.glob("snapshot-*"))) == 1, "same version, same file")
    with Store(DB) as st:
        st.conn.execute("UPDATE starred SET note='second look'")
        st.conn.commit()
    m3 = phone.build_snapshot(DB)
    check("a change to the starred list is a new version",
          m3["built"] and m3["version"] != m1["version"], m3["version"])
    with Store(DB) as st:
        st.ingest(records(9, n=3), source_file="(month 9)", adapter="test")
    m4 = phone.build_snapshot(DB)
    files = sorted(p.name for p in phonecfg.OUT.glob("snapshot-*"))
    check("an import is a new version; the newest two files are kept",
          m4["built"] and m4["counts"]["sightings"] == no + 6 and len(files) == 2
          and m4["file"] in files and m3["file"] in files,
          f"{m4['counts']['sightings']} sightings, files kept: {len(files)}")
    check("no temp files are left behind",
          not list(phonecfg.OUT.glob("*.tmp")), "none")

    print("\n== pairing and the settings file ==")
    check("phone sync starts off, and no code can be made while off",
          phonecfg.load()["enabled"] is False and not phonecfg.start_pairing()["ok"],
          phonecfg.start_pairing().get("error", ""))
    phonecfg.set_options(enabled=True)
    p = phonecfg.start_pairing()
    wrong = [phonecfg.redeem("000000" if p["code"] != "000000" else "111111", "x")
             for _ in range(4)]
    ok = phonecfg.redeem(p["code"], "Test phone")
    check("four wrong guesses, then the right code pairs the phone",
          all(not w["ok"] for w in wrong) and ok["ok"] and len(ok["token"]) > 30,
          f"device {ok.get('device')}")
    token = ok["token"]
    raw = phonecfg.SETTINGS.read_text(encoding="utf-8")
    check("the token is stored only as a hash, and the code is gone",
          token not in raw and phonecfg.load()["pair"] is None
          and hashlib.sha256(token.encode()).hexdigest() in raw, "sha256 only")
    p = phonecfg.start_pairing()
    for _ in range(5):
        phonecfg.redeem("999999" if p["code"] != "999999" else "888888", "x")
    late = phonecfg.redeem(p["code"], "x")
    check("five wrong guesses void the code, even for the right one later",
          not late["ok"] and "too many" in late["error"] or "no pairing" in late["error"],
          late["error"][:60])
    p = phonecfg.start_pairing()
    phonecfg.update(lambda s: s["pair"].__setitem__("expires", time.time() - 1))
    exp = phonecfg.redeem(p["code"], "x")
    check("an expired code is refused", not exp["ok"] and "expired" in exp["error"],
          exp["error"][:50])
    check("the token identifies its phone; with sync off it identifies nothing",
          (phonecfg.device_for(token) or {}).get("name") == "Test phone"
          and phonecfg.device_for("nope") is None
          and phonecfg.set_options(enabled=False) and phonecfg.device_for(token) is None,
          "on: Test phone, off: none")
    phonecfg.set_options(enabled=True)

    def adder(tag):
        for i in range(30):
            phonecfg.update(lambda s: s["devices"].append({"id": f"{tag}{i}", "name": "t"}))
    ths = [threading.Thread(target=adder, args=(t,)) for t in "ab"]
    [t.start() for t in ths]
    [t.join() for t in ths]
    n = len([d for d in phonecfg.load()["devices"] if d.get("name") == "t"])
    check("sixty changes from two writers at once: none lost", n == 60, f"{n} of 60 kept")
    phonecfg.update(lambda s: s.__setitem__("devices",
                                            [d for d in s["devices"] if d.get("name") != "t"]))

    print("\n== the listener ==")
    port = free_port()
    srv = phonesrv.start(port, lan=False)
    try:
        check("it listens on this laptop only unless home Wi-Fi is on",
              srv.server_address[0] == "127.0.0.1", f"bound to {srv.server_address}")
        c, _, b = http(port, "/ping")
        check("/ping says a VPA laptop is here, with no data",
              c == 200 and json.loads(b) == {"app": "vpa-phone", "format": phonecfg.FORMAT},
              b.decode())
        c1, _, _ = http(port, "/api/meta")
        c2, _, _ = http(port, "/api/snapshot", token="not-a-token")
        check("no token, or a wrong one: no data", c1 == 401 and c2 == 401, f"{c1}, {c2}")
        c, _, b = http(port, "/api/meta", token=token)
        meta = json.loads(b)
        check("a paired phone gets the copy's description",
              c == 200 and meta["version"] == m4["version"]
              and meta["counts"] == m4["counts"] and meta["device"] == "Test phone",
              f"version {meta.get('version')}")
        c, h, body = http(port, "/api/snapshot", token=token)
        snap = json.loads(gzip.decompress(body))
        check("...and the copy itself, gzip, the version it was told",
              c == 200 and h.get("Content-Encoding") == "gzip"
              and snap["version"] == meta["version"]
              and hashlib.sha256(body).hexdigest() == meta["sha256"],
              f"{len(body)} bytes")
        c, _, body = http(port, "/api/snapshot", token=token,
                          headers={"If-None-Match": f'"{meta["version"]}"'})
        check("a phone that already has this version gets 'not modified'",
              c == 304 and body == b"", "304, no body")
        dev = phonecfg.load()["devices"][0]
        check("the laptop sees when the phone last synced and which version",
              dev["last_seen"] and dev["last_version"] == meta["version"], dev["last_seen"])
        p = phonecfg.start_pairing()
        c, _, b = http(port, "/api/pair", method="POST", body={"code": p["code"]},
                       headers={"User-Agent": "Mozilla/5.0 (Android 14; Mobile; rv:131.0) "
                                              "Gecko/131.0 Firefox/131.0"})
        r2 = json.loads(b)
        check("pairing over the network names the phone from its browser",
              c == 200 and r2["ok"] and r2["device"]["name"] == "Firefox on Android",
              r2["device"]["name"] if r2.get("ok") else r2.get("error"))
        phonecfg.remove_device(r2["device"]["id"])
        c, _, _ = http(port, "/api/meta", token=r2["token"])
        check("a removed phone is locked out at once", c == 401, f"HTTP {c}")
        codes = {pth: http(port, pth)[0] for pth in ("/api/import", "/api/shutdown", "/../data/vpa.db",
                                                    "/output/phone/meta.json")}
        cpost = http(port, "/api/commit", method="POST", body={})[0]
        big = http(port, "/api/pair", method="POST", body={"code": "1" * 9000})[0]
        check("nothing else is there: no import, no shutdown, no files",
              set(codes.values()) == {404} and cpost == 404 and big == 413,
              f"{codes}, POST /api/commit {cpost}, huge body {big}")
        page = http(port, "/")[2].decode()
        app = http(port, "/app.js")[2].decode()
        sw = http(port, "/sw.js")[2].decode()
        man = json.loads(http(port, "/manifest.webmanifest")[2])
        check("the app, its offline worker and manifest are served (no data in them)",
              "/app.js" in page and "/api/pair" in app and man["display"] == "standalone"
              and "__BUILD__" not in sw and http(port, "/stats.js")[0] == 200
              and http(port, "/icon-512.png")[0] == 200,
              f"page {len(page)}, app {len(app)} chars, worker build {sw.split(chr(34))[1]}")
        pngs = {(i["sizes"], i["purpose"]) for i in man["icons"] if i["type"] == "image/png"}
        check("the home-screen icon is a real PNG, for launchers that crop and those that don't",
              pngs >= {("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"),
                       ("512x512", "maskable")}
              and 'rel="icon" href="/icon-192.png" type="image/png"' in page
              and 'rel="apple-touch-icon"' in page, f"{len(pngs)} PNG icon entries")
        phonecfg.update(lambda st: st.__setitem__("https_url", "https://lapop.example.ts.net"))
        c, _, b = http(port, "/api/meta", token=token)
        check("a phone is told the laptop's secure (Tailscale) address",
              c == 200 and json.loads(b).get("https_url") == "https://lapop.example.ts.net",
              json.loads(b).get("https_url"))
        ips = {"192.168.1.5": True, "10.0.0.2": True, "100.101.1.1": True,
               "127.0.0.1": True, "::1": True, "8.8.8.8": False,
               "::ffff:8.8.8.8": False, "1.1.1.1": False, "203.2.218.214": False, "garbage": False}
        bad = [ip for ip, want in ips.items() if phonesrv.allowed_ip(ip) != want]
        check("only private, Tailscale and loopback addresses are answered", not bad,
              "wrong for: " + ", ".join(bad) if bad else f"{len(ips)} addresses right")
        for _ in range(phonesrv.FAIL_LIMIT):
            http(port, "/api/meta", token="guess")
        c, _, _ = http(port, "/api/meta", token=token)
        check("thirty bad tokens lock everyone out for ten minutes", c == 429, f"HTTP {c}")
        phonesrv._FAILS.clear()
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n== Tailscale, as the Phone page reads it ==")
    from vpa import tailnet
    me = {"BackendState": "Running", "CertDomains": ["lapop.t.ts.net"],
          "Self": {"DNSName": "lapop.t.ts.net.", "TailscaleIPs": ["100.71.0.1"]},
          "Peer": {"x": {"HostName": "S25", "OS": "android", "Online": True}}}
    web = {"Web": {"lapop.t.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}}}
    cases = {
        "ready": (me, web),
        "funnel": (me, dict(web, AllowFunnel={"lapop.t.ts.net:443": True})),
        "not-served": (me, {}),
        "not-served ": (me, {"Web": {"lapop.t.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}}),
        "no-https": (dict(me, CertDomains=[]), web),
        "stopped": (dict(me, BackendState="Stopped"), web),
        "stopped ": (None, None),
    }
    got = {k: tailnet.read_status(st, sv, 8766)["state"] for k, (st, sv) in cases.items()}
    check("every Tailscale state is read right - Funnel on is flagged, never 'ready'",
          all(got[k] == k.strip() for k in got)
          and tailnet.read_status(*cases["ready"], 8766)["https_url"] == "https://lapop.t.ts.net",
          ", ".join(f"{k.strip()}={v}" for k, v in got.items()))

    print("\n== what the listener can reach ==")
    banned = {"store", "bulk", "inbox", "edit", "merge", "quickadd", "importui",
              "repair", "dupes", "starred", "phone", "viewer_xlsx", "explore"}
    seen, todo, reach = set(), ["phonesrv"], set()
    while todo:
        mod = todo.pop()
        if mod in seen or not (PKG / f"{mod}.py").exists():
            continue
        seen.add(mod)
        imps = vpa_imports(PKG / f"{mod}.py")
        reach |= imps
        todo.extend(imps)
    check("the listener imports nothing that can open the database",
          not (reach & banned) and reach <= {"phonecfg"},
          f"reaches only: {sorted(reach) or 'the standard library'}")

    print("\n== who listens: the app, or the background listener ==")
    port = free_port()
    phonecfg.update(lambda s: s.__setitem__("port", port))
    phone.TICK, phone.SETTLE = 0.3, 0.4
    stamp = WORK / "code_stamp"            # stands in for "VPA's code changed"
    stamp.write_text("1")
    wrapper = ("import sys; from pathlib import Path; from vpa import phonecfg, phone; "
               f"phonecfg.SETTINGS=Path(r'{phonecfg.SETTINGS}'); "
               f"phonecfg.OUT=Path(r'{phonecfg.OUT}'); phone.TICK=0.3; phone.SETTLE=0.4; "
               f"phone._code_stamp=lambda: Path(r'{stamp}').read_text(); "
               f"phone._spawn_background=lambda force=False: __import__('subprocess').Popen("
               f"[sys.executable, '-c', open(r'{WORK / 'wrapper.py'}').read()], cwd=r'{ROOT.parent}'); "
               f"sys.exit(phone.serve_forever(r'{DB}'))")
    (WORK / "wrapper.py").write_text(wrapper, encoding="utf-8")
    spawned = []

    def spawn():                          # the real one runs "-m vpa phone-serve",
        spawned.append(subprocess.Popen(  # which would read the REAL settings
            [sys.executable, "-c", wrapper], cwd=str(ROOT.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    phone._spawn_background = spawn

    def wait(fn, t=15.0):
        end = time.time() + t
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.1)
        return False

    def version():
        return (phonecfg.read_meta() or {}).get("version")
    try:
        phone.start(DB, role="app")
        check("with the app open, the app listens", wait(lambda: phone.HOST.status == "here")
              and phone._ping(port) is not None, f"status {phone.HOST.status}")
        v0 = version()
        with Store(DB) as st:
            st.ingest(records(10, n=2), source_file="(month 10)", adapter="test")
        check("an import is in the phone copy within seconds, untouched by hand",
              wait(lambda: version() != v0, 60), f"{v0} -> {version()}")
        phonecfg.set_options(background=True)
        check("'keep running when closed' hands over to a background listener",
              wait(lambda: spawned and phone.HOST.server is None
                   and phone.HOST.status == "elsewhere" and phone._ping(port) is not None),
              f"app status {phone.HOST.status}, background pid "
              f"{spawned[0].pid if spawned else None}")
        v1 = version()
        with Store(DB) as st:
            st.ingest(records(11, n=2), source_file="(month 11)", adapter="test")
        check("...which keeps the copy current on its own",
              wait(lambda: version() != v1, 60), f"{v1} -> {version()}")
        pid0 = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/ping").read())
        stamp.write_text("2")                       # "an update landed"
        check("after an update the background listener restarts itself on the new code",
              wait(lambda: spawned[0].poll() is not None and phone._ping(port) is not None, 30)
              and phone.HOST.server is None,
              f"old listener exit {spawned[0].poll()}, a new one answers {phone._ping(port) is not None}"
              f" ({pid0['app']})")
        phonecfg.set_options(background=False)
        check("switching it off ends the background listener and the app takes over",
              wait(lambda: phone._ping(port) is not None and phone.HOST.status == "here", 30),
              f"background exit code {spawned[0].poll()}, app status {phone.HOST.status}")
        phonecfg.set_options(enabled=False)
        check("turning phone sync off stops listening", wait(
            lambda: phone.HOST.server is None and phone._ping(port) is None
            and phone.HOST.status == "off"), f"status {phone.HOST.status}")
    finally:
        phone.stop()
        for p in spawned:
            if p.poll() is None:
                p.kill()
    check("the database is intact after all of it", sqlite3.connect(DB).execute(
        "PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_check ok")

    print("\n== the app and phone sync ==")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; from vpa import importui; "
         f"importui.serve(sys.argv[1], {port}, open_browser=False)", str(DB)],
        cwd=str(ROOT.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        st = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/phone", timeout=2) as r:
                    st = json.loads(r.read())
                break
            except Exception:
                time.sleep(0.25)
        check("an app serving a scratch database never runs phone sync",
              st is not None and st["role"] is None and st["here"] is False,
              f"role {st and st['role']}, listening {st and st['here']}")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/phone", timeout=5) as r:
            html = r.read().decode()
        check("the Phone page renders with the shared chrome",
              html.count(".card{") == 1 and html.count("--ui:") == 1
              and 'id="phoneBtn"' in html and "navbtn on" in html,
              f"{len(html)} chars")
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/shutdown", data=b"{}"), timeout=3)
        except Exception:
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(ok for ok, _ in RESULTS)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
