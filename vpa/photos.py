"""Listing photos: the photo addresses already inside the pages you save.

A saved carsales search page states, for every listing, its ad id and the
addresses of its first few photos (the schema.org `image` list beside the
listing's `url`). A saved details page states the ad's main photo the same
way and preloads the start of its gallery. This module keeps those
ADDRESSES - nothing else - so the laptop and the phone can show a car's
photo.

    Nothing here reaches a website. Addresses come only from files you saved
    and imported; no page is fetched, no photo is downloaded. A photo appears
    because the browser showing a car loads that one image, exactly as it
    would on the advert - only the cars you look at, only while you look.

Kept OUT of the database, in data/photos.json beside it:

* the price data is never touched - photos can be added, rebuilt or deleted
  without a write to vpa.db;
* the file is keyed by the site's own ad id (`SSE-AD-20672993`), which
  survives everything the database does to its own keys (merges, repairs);
* losing it loses only pictures. `python -m vpa photos --backfill` rebuilds
  what the kept pages hold.

Only addresses on the site's own photo server (`<site>.pxcrush.net`) of one
exact shape are stored, so a page can never make the app show an image from
anywhere else. Stored form: `carsales/cars/private/7zwwbadhk89otj6l43lwr4y06.jpg`;
the app adds the size it wants (`photo_url`).
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import date
from html import unescape
from pathlib import Path

FORMAT = "vpa-photos/1"
MAX_PER_CAR = 12               # a details page can preload more; a tile has 5
FILE_NAME = "photos.json"

#: a photo address on the site's own image server, in either of the two forms
#: a page uses: https://carsales.pxcrush.net/carsales/cars/private/ID.jpg and
#: https://carsales.pxcrush.net/cars/private/ID.jpg (same photo)
_URL = re.compile(
    r"https?://([a-z]{3,20})\.pxcrush\.net/(?:([a-z]{3,20})/)?([a-z]{2,20})/"
    r"([a-z]{2,20})/([a-z0-9]{10,64})\.(jpg|jpeg|png|webp)\b", re.I)
#: the stored form, checked again on every read and by both front ends
PATH = re.compile(r"^[a-z]{3,20}/[a-z]{2,20}/[a-z]{2,20}/[a-z0-9]{10,64}\.(jpg|jpeg|png|webp)$")
_AD = re.compile(r"\b([A-Z]{2,5}-AD-\d+)\b", re.I)
_PRELOAD = re.compile(
    r"<link[^>]+rel=\"preload\"[^>]+as=\"image\"[^>]+href=\"([^\"]+)\"", re.I)


def clean(url) -> str | None:
    """A photo address -> its stored form, or None if it is not one."""
    m = _URL.search(unescape(str(url or "")))
    if not m:
        return None
    host, first, cat, kind, pid, ext = (g.lower() if g else g for g in m.groups())
    if first and first != host:          # .../otherhost/... is not this site's
        return None
    p = f"{host}/{cat}/{kind}/{pid}.{ext}"
    return p if PATH.match(p) else None


def photo_url(path: str, w: int = 480, h: int = 320) -> str | None:
    """The address to show a stored photo at w x h (the image server sizes
    it; the whole photo is kept, padded rather than cropped)."""
    if not path or not PATH.match(path):
        return None
    host = path.split("/", 1)[0]
    return (f"https://{host}.pxcrush.net/{path}"
            f"?pxc_method=fitfill&pxc_bgtype=self&pxc_size={int(w)},{int(h)}")


def ad_id(listing_id=None, url=None) -> str | None:
    """The site's ad id for a stored car, from its listing id or its url."""
    for v in (listing_id, url):
        if isinstance(v, str) and (m := _AD.search(v)):
            return m.group(1).upper()
    return None


# ------------------------------------------------------------ reading pages
def _images(node) -> list[str]:
    im = node.get("image") if isinstance(node, dict) else None
    out = []
    for x in (im if isinstance(im, list) else [im] if im else []):
        p = clean(x.get("url") or x.get("contentUrl") if isinstance(x, dict) else x)
        if p and p not in out:
            out.append(p)
    return out


def extract(html: str) -> dict[str, list[str]]:
    """ad id -> photo addresses, from one saved page. Read-only.

    Search page: each schema.org listing names its own ad (in `url`) and
    its own photos (`image`), side by side in one object - nothing is
    matched up by position, so one car's photo cannot land on another.

    Details page: the ad is the page's single ad id; its photos are the
    schema.org `image` and the gallery images the page preloads (the ones
    shown first, at the top). Other photos on a details page are NOT read:
    that is where the site puts other people's cars ("similar listings"),
    which are never preloaded."""
    from .adapters import detailpage, htmlpage
    html = html or ""
    if "pxcrush.net" not in html:
        return {}
    found: dict[str, list[str]] = {}
    if detailpage.is_detail_page(html):
        d = detailpage.parse_html(html) or {}
        ad = ad_id(d.get("listing_id"), d.get("url"))
        if not ad:
            return {}
        pics: list[str] = []
        for m in htmlpage._LDJSON.finditer(html):
            try:
                data = json.loads(unescape(m.group(1)))
            except ValueError:
                continue
            for node in (data if isinstance(data, list) else [data]):
                pics += [p for p in _images(node) if p not in pics]
        main_kind = pics[0].split("/")[2] if pics else None
        for href in _PRELOAD.findall(html):
            p = clean(href)
            # same seller folder as the ad's own main photo, or nothing:
            # without a main photo there is nothing to check a preload against
            if p and main_kind and p.split("/")[2] == main_kind and p not in pics:
                pics.append(p)
        if pics:
            found[ad] = pics[:MAX_PER_CAR]
        return found
    for m in htmlpage._LDJSON.finditer(html):
        try:
            data = json.loads(unescape(m.group(1)))
        except ValueError:
            continue
        objs: list[dict] = []
        htmlpage._walk_ld(data, objs)
        for obj in objs:
            ad = ad_id(obj.get("sku"), obj.get("url")) or ad_id(obj.get("@id"))
            pics = _images(obj)
            if ad and pics:
                cur = found.setdefault(ad, [])
                cur += [p for p in pics if p not in cur]
    return {k: v[:MAX_PER_CAR] for k, v in found.items()}


# ------------------------------------------------------------------ storage
def store_path(db_path) -> Path:
    """data/photos.json for data/vpa.db: always beside the database it
    belongs to, so a test database never reads or writes the real one."""
    return Path(db_path).resolve().parent / FILE_NAME


@contextmanager
def _locked(path: Path, timeout: float = 10.0):
    lock = path.with_name(path.name + ".lock")
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("the photo list is busy")
            time.sleep(0.03)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def _read(path: Path, strict: bool = False) -> dict:
    """The stored ads. Missing or damaged reads as empty; `strict` raises
    instead when the file is there but could not be read (load() then
    keeps no cache, so a moment's lock never hides the photos)."""
    from .phonecfg import read_text_retry
    try:
        d = json.loads(read_text_retry(path))
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    except ValueError:
        return {}
    ads = d.get("ads") if isinstance(d, dict) else None
    out = {}
    for k, v in (ads or {}).items():
        if isinstance(v, dict) and isinstance(v.get("p"), list):
            p = [x for x in v["p"] if isinstance(x, str) and PATH.match(x)]
            if p:
                out[str(k).upper()] = {"p": p[:MAX_PER_CAR], "d": str(v.get("d") or "")}
    return out


_CACHE: dict[str, tuple] = {}


def load(db_path) -> dict[str, list[str]]:
    """ad id -> photo addresses. Cached until the file changes."""
    path = store_path(db_path)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        got = {k: v["p"] for k, v in _read(path, strict=True).items()}
    except OSError:
        return hit[1] if hit else {}
    _CACHE[str(path)] = (sig, got)
    return got


def stat(db_path):
    """(mtime, size) of the photo file, or None - for change detection."""
    try:
        st = store_path(db_path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def remember(db_path, found: dict[str, list[str]], day=None) -> int:
    """Merge one page's photos in. Returns how many ads changed.

    The newer page's photos go first; ones only an older page showed are
    kept after them (a details page shows more than a search tile). A page
    older than what is stored adds its extra photos at the end and never
    reorders. Written atomically, under a lock, with the previous version
    kept as photos.json.bak. Never raises into an import: the caller wraps
    it, and a failure here costs only pictures."""
    found = {str(k).upper(): [p for p in v if isinstance(p, str) and PATH.match(p)]
             for k, v in (found or {}).items() if _AD.fullmatch(str(k))}
    found = {k: v for k, v in found.items() if v}
    if not found:
        return 0
    day = str(day or date.today().isoformat())[:10]
    path = store_path(db_path)
    from .phonecfg import _replace
    with _locked(path):
        # strict: a file that is there but unreadable aborts this save rather
        # than being rewritten from nothing - that would lose every photo
        ads = _read(path, strict=True)
        changed = 0
        for ad, pics in found.items():
            cur = ads.get(ad)
            if cur is None:
                merged, d = pics, day
            elif day >= cur["d"]:
                merged, d = pics + [p for p in cur["p"] if p not in pics], day
            else:
                merged, d = cur["p"] + [p for p in pics if p not in cur["p"]], cur["d"]
            merged = merged[:MAX_PER_CAR]
            if cur is None or merged != cur["p"] or d != cur["d"]:
                ads[ad] = {"p": merged, "d": d}
                changed += 1
        if changed:
            body = json.dumps({"format": FORMAT, "ads": dict(sorted(ads.items()))},
                              separators=(",", ":"))
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(body, encoding="utf-8")
            if path.exists():
                bak = path.with_name(path.name + ".bak")
                try:
                    bak.write_bytes(path.read_bytes())
                except OSError:
                    pass
            _replace(tmp, path)
    return changed


def remember_html(db_path, html: str, day=None) -> int:
    """extract + remember, for the import paths. Never raises."""
    try:
        return remember(db_path, extract(html), day)
    except Exception:
        return 0


# ------------------------------------------------------------ looking up
def for_car(db_path, listing_id=None, url=None) -> list[str]:
    ad = ad_id(listing_id, url)
    return list(load(db_path).get(ad, [])) if ad else []


def first_for(db_path, listing_id=None, url=None) -> str | None:
    p = for_car(db_path, listing_id, url)
    return p[0] if p else None


# ------------------------------------------------------------ backfill
def kept_pages(db_path) -> list[Path]:
    """Every saved page the app still has: data/raw and the watch folder's
    imported/ archive (beside the database's folder)."""
    data = Path(db_path).resolve().parent
    out: list[Path] = []
    for folder in (data / "raw", data.parent / "carsales inbox" / "imported"):
        if folder.is_dir():
            out += sorted(p for p in folder.rglob("*")
                          if p.suffix.lower() in (".htm", ".html") and p.is_file())
    return out


def backfill(db_path, pages=None) -> dict:
    """Read every kept page and remember its photos. Reads pages, writes
    only photos.json; the database is opened read-only to count coverage."""
    from .schema import file_mtime_date
    pages = kept_pages(db_path) if pages is None else [Path(p) for p in pages]
    changed = ads = 0
    for p in pages:
        try:
            html = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = extract(html)
        ads += len(found)
        if found:
            changed += remember(db_path, found, file_mtime_date(p).isoformat())
    return {"pages": len(pages), "ads_in_pages": ads, "changed": changed,
            **coverage(db_path)}


def coverage(db_path) -> dict:
    """How many stored cars have a photo. Read-only."""
    from .store import connect_readonly
    have = load(db_path)
    try:
        conn = connect_readonly(db_path)
    except Exception:
        return {"cars": 0, "with_photo": 0, "stored_ads": len(have)}
    try:
        rows = conn.execute("SELECT listing_id, url FROM vehicles").fetchall()
    finally:
        conn.close()
    n = sum(1 for lid, url in rows if (a := ad_id(lid, url)) and a in have)
    return {"cars": len(rows), "with_photo": n, "stored_ads": len(have)}
