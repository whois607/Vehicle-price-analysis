"""Watch folder - save pages into one folder and they import themselves.

    served on the Bulk page (Add data > Bulk upload > Watch a folder)

You browse and save pages yourself, exactly as before (Ctrl+S). This module
only notices each file as it lands in the folder, imports it through the
very same `bulk.run()` the Bulk page's Upload button uses, and then moves it
out into `imported/<date>/`. Moving it out is the point of the design: the
next page you save under the same file name lands in an empty folder, so
there is no "replace existing file?" prompt and no numbering files by hand.

Nothing here reaches a website. It reads files you saved, like every other
way into this app.

---------------------------------------------------------------- a session
You start one with a page count. For its duration:

* **One backup, at the start**, before anything is written. Every page's
  run records that backup's path (bulk.run's `backup=`), instead of each
  page taking its own copy of the whole database.
* **Every page is its own bulk run**, so it lands in Recent runs and can be
  undone alone; "Undo session" undoes the lot, newest first.
* **Files already in the folder when the session starts are left alone** -
  they are listed, never imported, because nothing says they belong to it.
* **A file is imported only once it has stopped changing** (same size and
  modified time on two looks, 0.8 s apart), so a page still being written
  by the browser is never read half-finished.
* **The same page saved twice is skipped** (same content hash in this
  session), and a file that is not a listings page (a bot check, an empty
  save) is refused with the reason and does not count towards the total.
* **It stops by itself** once the page count is reached.

Pace and time left come from the gaps between your saves (their modified
times), using the median gap so one long pause does not wreck the estimate.

------------------------------------------------------------- the pieces
`Session` is the watcher and knows nothing about carsales - the importer is
passed in as a function (`ingest=`), so the same watcher works for any file
the app can read. `start()/stop()/state()/undo_session()` are what the web
routes call; there is at most one session per running app.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path

from . import bulk
from .store import DEFAULT_DB, Store

#: files the importers can read; anything else in the folder is ignored
SUFFIXES = {".html", ".htm", ".csv", ".json", ".txt"}
#: a file must look the same on two scans this far apart to count as saved
SETTLE_S = 0.8
POLL_S = 0.4
MAX_PAGES = 500
ARCHIVE = "imported"


# ----------------------------------------------------------------- settings
def _settings_path(db_path) -> Path:
    return Path(db_path).resolve().parent / "inbox.json"


#: inside the project, beside data/ - and in .gitignore, because saved pages
#: are copies of someone else's site and must never be committed
INBOX_DIR = Path(__file__).resolve().parent.parent / "carsales inbox"


def default_folder() -> str:
    return str(INBOX_DIR)


def load_settings(db_path=DEFAULT_DB) -> dict:
    out = {"folder": default_folder(), "pages": 20, "label": ""}
    try:
        out.update(json.loads(_settings_path(db_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return out


def save_settings(db_path, **kw) -> None:
    s = load_settings(db_path)
    s.update({k: v for k, v in kw.items() if v is not None})
    try:
        _settings_path(db_path).write_text(json.dumps(s, indent=1), encoding="utf-8")
    except OSError:
        pass                                   # a preference, not data


# ------------------------------------------------------------------ helpers
def _candidates(folder: Path) -> list[Path]:
    out = []
    try:
        for p in folder.iterdir():
            n = p.name
            if (p.is_file() and p.suffix.lower() in SUFFIXES
                    and not n.startswith((".", "~"))
                    and not (folder / (n + ".part")).exists()):
                out.append(p)
    except OSError:
        pass
    return sorted(out, key=lambda p: p.stat().st_mtime if p.exists() else 0)


def _sig(p: Path):
    st = p.stat()
    return (st.st_size, st.st_mtime_ns)


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(2, 10000):
        alt = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not alt.exists():
            return alt
    raise OSError(f"no free name for {dest}")


def _check_folder(folder: Path, db_path) -> str | None:
    """Why this folder must not be watched, or None."""
    if not folder.is_absolute():
        return "give the folder's full path, e.g. C:\\Users\\you\\Downloads\\carsales inbox"
    db_dir = Path(db_path).resolve().parent
    here = Path(__file__).resolve().parent.parent
    f = folder.resolve()
    if f in (db_dir, here, here / "vpa", Path(f.anchor)) or f == Path.home():
        return ("pick a folder of its own for saved pages - not the app's "
                "folder, its data folder, your home folder or a drive root")
    if f.exists() and any(f.glob("*.db")):
        return "that folder holds a database file; use an empty folder for saved pages"
    return None


# ------------------------------------------------------------------ session
class Session:
    def __init__(self, db_path, folder: Path, target: int, label: str,
                 ingest=None):
        self.db_path = db_path
        self.folder = folder
        self.target = target
        self.label = label or "watch folder"
        self.ingest = ingest or self._bulk_ingest
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.started = time.time()
        self.status = "watching"
        self.stop_reason = ""
        self.backup = None
        self.events: list[dict] = []
        self.done = 0
        self.arrivals: list[float] = []
        self.hashes: set[str] = set()
        self.ignored: dict[str, tuple] = {}
        self._pending: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # the default importer: exactly what the Bulk page's Upload button does
    def _bulk_ingest(self, name, text, capture_date, page_no):
        return bulk.run([{"name": name, "text": text, "capture_date": capture_date}],
                        self.db_path, label=f"{self.label} - page {page_no}",
                        backup=self.backup)

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        for p in _candidates(self.folder):
            self.ignored[p.name] = _sig(p)
        with Store(self.db_path) as store:
            self.backup = str(store.backup())
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="vpa-inbox")
        self._thread.start()

    def stop(self, reason="stopped"):
        with self._lock:
            if self.status == "watching":
                self.status, self.stop_reason = "stopped", reason
        self._stop.set()

    def join(self, timeout=10):
        if self._thread:
            self._thread.join(timeout)

    # ---- the loop
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as exc:          # never die silently
                with self._lock:
                    self.status = "error"
                    self.stop_reason = f"the watcher stopped: {type(exc).__name__}: {exc}"
                return
            if self.done >= self.target:
                with self._lock:
                    self.status = "complete"
                    self.stop_reason = f"all {self.target} pages imported"
                return
            self._stop.wait(POLL_S)

    def _scan(self):
        now = time.time()
        for p in _candidates(self.folder):
            if self._stop.is_set() or self.done >= self.target:
                return
            try:
                sig = _sig(p)
            except OSError:
                continue
            if self.ignored.get(p.name) == sig:
                continue                      # there before the session began
            seen = self._pending.get(p.name)
            if not seen or seen[0] != sig:
                self._pending[p.name] = (sig, now)
                continue
            if now - seen[1] < SETTLE_S:
                continue
            self._pending.pop(p.name, None)
            self.ignored.pop(p.name, None)
            self._take(p, sig)

    def _take(self, p: Path, sig):
        try:
            data = p.read_bytes()
        except OSError:
            return                            # still locked - next scan
        saved_at = sig[1] / 1e9
        ev = {"seq": len(self.events) + 1, "name": p.name,
              "saved_at": datetime.fromtimestamp(saved_at).strftime("%H:%M:%S"),
              "kind": "", "error": "", "html": False, "run": None,
              "obs_new": 0, "records": 0, "archived": ""}
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.hashes:
            ev.update(kind="duplicate",
                      error="the same page again - identical to one already imported, skipped")
        else:
            text = data.decode("utf-8", errors="replace")
            day = datetime.fromtimestamp(saved_at).strftime("%Y-%m-%d")
            res = self.ingest(p.name, text, day, self.done + 1)
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "the import failed")
            f = (res.get("files") or [{}])[0]
            if res.get("wrote") and f.get("ok"):
                self.hashes.add(digest)
                self.done += 1
                self.arrivals.append(saved_at)
                ev.update(kind="imported", run=res.get("run"),
                          obs_new=f.get("obs_new", 0), records=f.get("records", 0),
                          page=self.done, result=res)
            else:
                ev.update(kind="refused", error=f.get("error") or res.get("reason", ""),
                          html=bool(f.get("html")), result=res)
        # OneDrive (the project folder lives in it) can hold a fresh file for
        # a moment while it syncs: retry the move briefly, and if it still
        # cannot go, leave it - the page is already in, and the content hash
        # stops it being imported again.
        for attempt in range(6):
            try:
                ev["archived"] = self._archive(p)
                break
            except OSError as exc:
                if attempt == 5:
                    ev["archive_error"] = (f"imported, but could not be moved out "
                                           f"of the folder ({exc.__class__.__name__}) "
                                           f"- delete it before saving the next page")
                    self.hashes.add(digest)
                    try:
                        self.ignored[p.name] = _sig(p)
                    except OSError:
                        pass
                else:
                    time.sleep(0.5)
        with self._lock:
            self.events.append(ev)

    def _archive(self, p: Path) -> str:
        """Move the file (and a 'Web Page, complete' _files folder) out of the
        inbox, so the next save under the same name finds the folder empty."""
        day = datetime.now().strftime("%Y-%m-%d")
        dest_dir = self.folder / ARCHIVE / day
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        dest = _unique(dest_dir / f"{stamp} {p.name}")
        shutil.move(str(p), str(dest))
        extra = self.folder / f"{p.stem}_files"
        if extra.is_dir():
            try:
                shutil.move(str(extra), str(_unique(dest_dir / f"{stamp} {extra.name}")))
            except OSError:
                pass
        return str(dest.relative_to(self.folder))

    # ---- what the page shows
    def pace(self):
        gaps = [b - a for a, b in zip(self.arrivals, self.arrivals[1:]) if b > a]
        return statistics.median(gaps) if gaps else None

    def state(self, since: int = 0) -> dict:
        with self._lock:
            events = list(self.events)
            status, reason = self.status, self.stop_reason
        pace = self.pace()
        left = max(0, self.target - self.done)
        eta = finish = None
        if pace and left and status == "watching":
            nxt = max(time.time(), self.arrivals[-1] + pace)
            end = nxt + (left - 1) * pace
            eta, finish = end - time.time(), datetime.fromtimestamp(end).strftime("%H:%M")
        return {
            "id": self.id, "folder": str(self.folder), "label": self.label,
            "target": self.target, "done": self.done, "status": status,
            "reason": reason, "backup": self.backup,
            "started": datetime.fromtimestamp(self.started).strftime("%H:%M:%S"),
            "elapsed_s": round(time.time() - self.started),
            "pace_s": round(pace, 1) if pace else None,
            "eta_s": round(eta) if eta is not None else None, "finish": finish,
            "refused": sum(1 for e in events if e["kind"] == "refused"),
            "duplicates": sum(1 for e in events if e["kind"] == "duplicate"),
            "ignored": sorted(self.ignored),
            "runs": [e["run"] for e in events if e.get("run")],
            "seq": len(events),
            "events": [{k: v for k, v in e.items() if k != "result" or e["seq"] > since}
                       for e in events if e["seq"] > since],
        }


# ------------------------------------------------ one session per running app
_CUR: dict = {"s": None}
_GLOCK = threading.Lock()


def start(db_path, folder: str, pages, label: str = "") -> dict:
    try:
        n = int(pages)
    except (TypeError, ValueError):
        return {"ok": False, "error": "how many pages? give a whole number"}
    if not 1 <= n <= MAX_PAGES:
        return {"ok": False, "error": f"pages must be between 1 and {MAX_PAGES}"}
    folder = (folder or "").strip().strip('"')
    if not folder:
        return {"ok": False, "error": "choose a folder to watch"}
    f = Path(os.path.expandvars(os.path.expanduser(folder)))
    why = _check_folder(f, db_path)
    if why:
        return {"ok": False, "error": why}
    with _GLOCK:
        cur = _CUR["s"]
        if cur and cur.status == "watching":
            return {"ok": False, "error": "a session is already watching - stop it first"}
        s = Session(db_path, f, n, label)
        try:
            s.start()
        except OSError as exc:
            return {"ok": False, "error": f"cannot use that folder: {exc}"}
        _CUR["s"] = s
    save_settings(db_path, folder=str(f), pages=n, label=label)
    return {"ok": True, "session": s.state()}


def stop() -> dict:
    s = _CUR["s"]
    if s:
        s.stop("stopped by you")
    return {"ok": True, "session": s.state() if s else None}


def state(db_path, since: int = 0) -> dict:
    s = _CUR["s"]
    return {"ok": True, "settings": load_settings(db_path),
            "session": s.state(since) if s else None}


def undo_session(db_path) -> dict:
    """Undo every run this session made, newest first - bulk.undo each."""
    s = _CUR["s"]
    if not s:
        return {"ok": False, "error": "no session to undo"}
    s.stop("stopped for undo")
    s.join()
    done, errors = [], []
    for run_id in reversed([e["run"] for e in s.events if e.get("run")]):
        r = bulk.undo(db_path, run_id=run_id)
        (done if r.get("ok") else errors).append(
            run_id if r.get("ok") else f"run #{run_id}: {r.get('error')}")
    return {"ok": not errors, "undone": done, "errors": errors,
            "error": "; ".join(errors)}
