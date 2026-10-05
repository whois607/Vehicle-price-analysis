"""Phone sync: the listener phones talk to.

A separate, deliberately tiny HTTP server - not the laptop app's server, which
can import, undo and shut down, and stays on 127.0.0.1. This one can do
exactly four things, and none of them reaches the database: it only reads
files that phone.py wrote under output/phone/, and it imports nothing that
opens vpa.db (tests/check_phone.py enforces that):

    GET  /ping            "a VPA laptop is here" - no data, no token needed
    POST /api/pair        a pairing code from the laptop screen -> a token
    GET  /api/meta        the current copy's version, counts and size
    GET  /api/snapshot    the copy itself (gzip); "304 not modified" if the
                          phone already holds this version
    GET  /, icons, manifest   the phone page shell (no data in it)

Every /api/ call but pairing needs a paired phone's token (Authorization:
Bearer ...). Requests from outside private address ranges are refused
outright, so even if this were ever exposed by mistake it would not answer
the internet. Listens on 127.0.0.1 by default - the HTTPS route
(`tailscale serve`) reaches it there - and on the home network only when
"home Wi-Fi" is switched on.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import phonecfg

MAX_BODY = 4096
FAIL_WINDOW = 600              # seconds
FAIL_LIMIT = 30                # bad tokens / pair attempts per window, all callers
_CGNAT = ipaddress.ip_network("100.64.0.0/10")    # Tailscale addresses
_FAILS: list[float] = []
_FAIL_LOCK = threading.Lock()


def allowed_ip(host: str) -> bool:
    """Loopback, private LAN ranges and Tailscale's 100.64/10 - nothing that
    could be a stranger on the internet."""
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or (ip.version == 4 and ip in _CGNAT)


def _fail() -> None:
    with _FAIL_LOCK:
        _FAILS.append(time.time())


def _locked_out() -> bool:
    with _FAIL_LOCK:
        cut = time.time() - FAIL_WINDOW
        while _FAILS and _FAILS[0] < cut:
            _FAILS.pop(0)
        return len(_FAILS) >= FAIL_LIMIT


def _device_name(ua: str) -> str:
    ua = ua or ""
    os_ = ("Android" if "Android" in ua else "iPhone" if "iPhone" in ua
           else "iPad" if "iPad" in ua else "Windows" if "Windows" in ua
           else "Mac" if "Mac OS" in ua else "Linux" if "Linux" in ua else "")
    br = ("Firefox" if "Firefox" in ua else "Edge" if "Edg/" in ua
          else "Chrome" if "Chrome" in ua else "Safari" if "Safari" in ua else "")
    return " on ".join(x for x in (br, os_) if x) or "Phone"


class PhoneHandler(BaseHTTPRequestHandler):
    server_version = "vpa-phone"
    sys_version = ""

    def log_message(self, fmt, *args):
        """One short line for a download or a refusal - never a token, and
        not every "no change" poll."""
        code = str(args[1]) if len(args) > 1 else ""
        path = self.path.split("?")[0]
        if code.startswith(("4", "5")) or (code == "200" and path == "/api/snapshot")                 or path == "/api/pair":
            print(f"{time.strftime('%H:%M:%S')} phone {self.client_address[0]} "
                  f"{self.command} {path} -> {code}")

    # ---- responses
    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json",
              headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _gate(self) -> bool:
        if not allowed_ip(self.client_address[0]):
            self._send(403, b'{"ok":false,"error":"not from this network"}')
            return False
        return True

    def _device(self):
        """The paired phone making this request, or None (and a 401/429 sent)."""
        if _locked_out():
            self._json({"ok": False, "error": "too many failed attempts - wait ten "
                        "minutes"}, 429)
            return None
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        dev = phonecfg.device_for(token) if token else None
        if dev is None:
            if token:
                _fail()
            self._json({"ok": False, "paired": False,
                        "error": "this phone is not paired (or was removed) - pair it "
                                 "again from VPA on the laptop"}, 401)
        return dev

    # ---- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        if path == "/ping":
            self._json({"app": phonecfg.APP_ID, "format": phonecfg.FORMAT})
        elif path in _STATIC:
            body, ctype, cache = _STATIC[path]()
            if body is None:
                self._send(404, b"not found", "text/plain")
            else:
                self._send(200, body, ctype, {"Cache-Control": cache})
        elif path == "/api/meta":
            dev = self._device()
            if dev is None:
                return
            m = phonecfg.read_meta()
            if m is None:
                self._json({"ok": False, "error": "the laptop has not built the phone "
                            "copy yet - give it a minute"}, 503)
                return
            self._json({"ok": True, "device": dev.get("name"),
                        **{k: m.get(k) for k in ("format", "version", "built", "bytes",
                                                 "sha256", "counts")}})
        elif path == "/api/snapshot":
            dev = self._device()
            if dev is None:
                return
            m = phonecfg.read_meta()
            f = phonecfg.snapshot_path(m) if m else None
            if m is None or not f.is_file():
                self._json({"ok": False, "error": "no phone copy yet"}, 503)
                return
            etag = f'"{m["version"]}"'
            if self.headers.get("If-None-Match", "") == etag:
                phonecfg.seen(dev["id"], m["version"])
                self._send(304, b"", "application/json", {"ETag": etag,
                                                          "Cache-Control": "no-cache"})
                return
            body = f.read_bytes()
            self._send(200, body, "application/json; charset=utf-8",
                       {"Content-Encoding": "gzip", "ETag": etag,
                        "Cache-Control": "no-cache", "X-VPA-Version": m["version"]})
            phonecfg.seen(dev["id"], m["version"])
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._gate():
            return
        if self.path != "/api/pair":
            self._send(404, b"not found", "text/plain")
            return
        if _locked_out():
            self._json({"ok": False, "error": "too many failed attempts - wait ten "
                        "minutes"}, 429)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._json({"ok": False, "error": "bad request"}, 413)
            return
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "bad request"}, 400)
            return
        name = d.get("name") or _device_name(self.headers.get("User-Agent", ""))
        try:
            r = phonecfg.redeem(d.get("code"), name)
        except TimeoutError as e:
            self._json({"ok": False, "error": str(e)}, 503)
            return
        if not r["ok"]:
            _fail()
        self._json(r, 200 if r["ok"] else 403)


# ------------------------------------------------------------------ static
def _asset(name, ctype, cache="no-cache"):
    def f():
        p = phonecfg.ROOT / "assets" / name
        return (p.read_bytes() if p.is_file() else None), ctype, cache
    return f


def _page():
    return _PHONE_PAGE.encode("utf-8"), "text/html; charset=utf-8", "no-cache"


def _manifest():
    m = {"name": "VPA", "short_name": "VPA", "start_url": "/", "scope": "/",
         "display": "standalone", "background_color": "#f4f6f9",
         "theme_color": "#2b5fc4", "description": "Vehicle price analysis - your data, offline",
         "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                    "purpose": "any maskable"},
                   {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                    "purpose": "any maskable"}]}
    return json.dumps(m).encode(), "application/manifest+json", "no-cache"


_STATIC = {"/": _page, "/index.html": _page, "/manifest.webmanifest": _manifest,
           "/icon-192.png": _asset("vpa-192.png", "image/png"),
           "/icon-512.png": _asset("vpa-512.png", "image/png"),
           "/icon.svg": _asset("vpa.svg", "image/svg+xml"),
           "/favicon.ico": _asset("vpa.ico", "image/x-icon")}


def start(port: int, lan: bool = False) -> ThreadingHTTPServer:
    """Listen (in a daemon thread) and return the server. 127.0.0.1 only,
    unless lan. Raises OSError if the port is taken."""
    srv = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), PhoneHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="vpa-phone-http", daemon=True).start()
    return srv


# A minimal page for now: pair, then fetch the copy and say what arrived. It
# proves the whole path on the real phone; the search screens replace the
# body of this page next.
_PHONE_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>VPA</title>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#2b5fc4">
<style>
 :root{--ink:#151a24;--dim:#667083;--line:#e3e7ee;--accent:#2b5fc4;--bg:#f4f6f9;
   --good:#1f7a4a;--bad:#b3261e;--card:#fff}
 @media (prefers-color-scheme:dark){:root{--ink:#e8ebf1;--dim:#9aa3b2;--line:#2b313c;
   --bg:#12151b;--card:#1a1e26;--accent:#6f9bf0;--good:#5cc28c;--bad:#f08a80}}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
   font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
 .wrap{max-width:520px;margin:0 auto;padding:20px 16px 40px}
 header{display:flex;align-items:center;gap:12px;margin-bottom:20px}
 header img{width:40px;height:40px;border-radius:10px}
 h1{font-size:20px;margin:0}h1 small{display:block;font-size:13px;color:var(--dim);font-weight:400}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
 label{font-size:13px;color:var(--dim);display:block;margin-bottom:6px}
 input{width:100%;font-size:28px;letter-spacing:.3em;text-align:center;padding:10px;
   border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
 button{width:100%;margin-top:12px;font-size:16px;padding:13px;border:0;border-radius:10px;
   background:var(--accent);color:#fff;font-weight:600}
 button.plain{background:transparent;color:var(--dim);border:1px solid var(--line)}
 .msg{margin-top:12px;font-size:14px;min-height:1.4em}.bad{color:var(--bad)}.good{color:var(--good)}
 .big{font-size:30px;font-weight:700}.dim{color:var(--dim);font-size:14px}
 dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:12px 0 0;font-size:14px}
 dt{color:var(--dim)}dd{margin:0;font-variant-numeric:tabular-nums}
</style></head><body><div class="wrap">
<header><img src="/icon-192.png" alt=""><h1>VPA<small>vehicle price analysis</small></h1></header>
<div id="pair" class="card" hidden>
 <b>Pair this phone</b>
 <p class="dim">On the laptop, open VPA &rarr; Phone &rarr; <b>Pair a phone</b>, then type the six-digit code shown there.</p>
 <label for="code">code</label><input id="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code">
 <button id="pairBtn">Pair</button><div id="pmsg" class="msg"></div>
</div>
<div id="sync" class="card" hidden>
 <div class="dim">Your data on this phone</div>
 <div class="big" id="cars">&ndash;</div>
 <dl><dt>As of</dt><dd id="asof">&ndash;</dd><dt>Sightings</dt><dd id="sights">&ndash;</dd>
 <dt>Forecasts</dt><dd id="fcs">&ndash;</dd><dt>Download</dt><dd id="size">&ndash;</dd>
 <dt>Paired as</dt><dd id="dev">&ndash;</dd></dl>
 <button id="syncBtn">Sync now</button><div id="smsg" class="msg"></div>
 <button class="plain" id="forget">Unpair this phone</button>
</div>
<p class="dim">Search screens come next; this page checks the sync works.</p>
</div>
<script>
const $=id=>document.getElementById(id);
const TK="vpa.token", VK="vpa.version", MK="vpa.meta";
const get=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
const put=(k,v)=>{try{v==null?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}};
function show(){const t=get(TK);$("pair").hidden=!!t;$("sync").hidden=!t;
  const m=JSON.parse(get(MK)||"null");if(m)paint(m);}
function paint(m){$("cars").textContent=m.counts.vehicles.toLocaleString()+" cars";
  $("asof").textContent=new Date(m.built).toLocaleString();
  $("sights").textContent=m.counts.sightings.toLocaleString();
  $("fcs").textContent=m.counts.forecasts.toLocaleString();
  $("size").textContent=Math.round(m.bytes/1024)+" KB";$("dev").textContent=m.device||"-";}
$("pairBtn").onclick=async()=>{
  $("pmsg").className="msg";$("pmsg").textContent="pairing...";
  try{const r=await fetch("/api/pair",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code:$("code").value})}).then(r=>r.json());
    if(!r.ok){$("pmsg").className="msg bad";$("pmsg").textContent=r.error;return;}
    put(TK,r.token);show();sync();
  }catch(e){$("pmsg").className="msg bad";$("pmsg").textContent="can't reach the laptop: "+e;}
};
async function sync(){
  const t=get(TK);if(!t)return;
  $("smsg").className="msg";$("smsg").textContent="checking...";
  const H={Authorization:"Bearer "+t};
  try{
    const m=await fetch("/api/meta",{headers:H});
    if(m.status===401){put(TK,null);put(VK,null);show();
      $("pmsg").className="msg bad";$("pmsg").textContent="this phone was unpaired on the laptop - pair it again";return;}
    const meta=await m.json();if(!meta.ok){$("smsg").className="msg bad";$("smsg").textContent=meta.error;return;}
    const have=get(VK);
    const r=await fetch("/api/snapshot",{headers:{...H,...(have?{"If-None-Match":'"'+have+'"'}:{})}});
    if(r.status===304){$("smsg").className="msg good";$("smsg").textContent="up to date";}
    else if(r.ok){const snap=await r.json();
      if(snap.version!==meta.version||snap.counts.vehicles!==meta.counts.vehicles)throw new Error("copy did not match its description");
      put(VK,snap.version);
      $("smsg").className="msg good";$("smsg").textContent="downloaded "+snap.vehicles.rows.length.toLocaleString()+" cars";}
    else throw new Error("HTTP "+r.status);
    put(MK,JSON.stringify(meta));paint(meta);
  }catch(e){$("smsg").className="msg bad";$("smsg").textContent="sync failed: "+e.message;}
}
$("syncBtn").onclick=sync;
$("forget").onclick=()=>{if(confirm("Unpair this phone? You can pair it again any time.")){put(TK,null);put(VK,null);put(MK,null);show();}};
show();if(get(TK))sync();
</script></body></html>
"""
