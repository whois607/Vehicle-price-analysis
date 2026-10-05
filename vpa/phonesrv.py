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
DISCARD_MAX = 64 * 1024     # a refused body read (and dropped) before answering
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
                        "https_url": phonecfg.load().get("https_url"),
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

    def _discard_body(self):
        """Read and drop the body of a request being refused. Answering
        before it is read leaves it unread when the connection closes, and
        Windows then RESETS the connection - the client sees an abort instead
        of the answer. Only up to DISCARD_MAX is read; past that the
        connection is simply closed."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return
        if 0 < n <= DISCARD_MAX:
            self.rfile.read(n)

    def do_POST(self):
        if not self._gate():
            return
        if self.path != "/api/pair":
            self._discard_body()
            self._send(404, b"not found", "text/plain")
            return
        if _locked_out():
            self._discard_body()
            self._json({"ok": False, "error": "too many failed attempts - wait ten "
                        "minutes"}, 429)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._discard_body()
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
APP = phonecfg.ROOT / "vpa" / "phone_app"
_SHELL = ("index.html", "app.js", "stats.js", "app.css", "sw.js")


def _build_id() -> str:
    """A hash of the app's own files: the offline worker carries it, so a
    changed app is a changed worker and phones pick it up on the next open."""
    import hashlib
    h = hashlib.sha1()
    for f in _SHELL:
        try:
            h.update((APP / f).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


def _app(name, ctype):
    def f():
        p = APP / name
        if not p.is_file():
            return None, ctype, "no-cache"
        body = p.read_bytes()
        if name == "sw.js":
            body = body.replace(b"__BUILD__", _build_id().encode())
        return body, ctype, "no-cache"
    return f


def _asset(name, ctype, cache="no-cache"):
    def f():
        p = phonecfg.ROOT / "assets" / name
        return (p.read_bytes() if p.is_file() else None), ctype, cache
    return f


def _manifest():
    # PNG icons, listed once per purpose: launchers that crop to their own
    # shape take "maskable", the rest take "any". An SVG alone shows as a
    # blank tile on an Android home screen.
    icons = [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": p}
             for p in ("any", "maskable") for n in (192, 512)]
    m = {"name": "VPA", "short_name": "VPA", "start_url": "/", "scope": "/", "id": "/",
         "display": "standalone", "orientation": "portrait", "background_color": "#2b5fc4",
         "theme_color": "#2b5fc4", "description": "Vehicle price analysis - your data, offline",
         "icons": icons}
    return json.dumps(m).encode(), "application/manifest+json", "no-cache"


_PAGE = _app("index.html", "text/html; charset=utf-8")
_STATIC = {"/": _PAGE, "/index.html": _PAGE,
           "/app.js": _app("app.js", "text/javascript; charset=utf-8"),
           "/stats.js": _app("stats.js", "text/javascript; charset=utf-8"),
           "/app.css": _app("app.css", "text/css; charset=utf-8"),
           "/sw.js": _app("sw.js", "text/javascript; charset=utf-8"),
           "/manifest.webmanifest": _manifest,
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
