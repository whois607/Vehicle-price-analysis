"""Build a shareable zip of the code - and only the code.

    python tools/make_package.py

Writes output/vpa-<commit>.zip from the COMMITTED tree (`git archive HEAD`),
so nothing that is not committed can end up in it: no database, no saved
pages, no photos list, no private data/sites.json, no git history. Then it
checks the zip and refuses (deleting it) if:

* any file holds a word from your private site file (vpa/sites.py,
  `private_words`) - the shared code names no website;
* any file is data: a database, a saved page, or anything under data/
  except its placeholder.

Commit first: uncommitted changes are not in the zip, and it says so.
"""
from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_SUFFIXES = (".db", ".db-journal", ".db-wal", ".db-shm", ".htm", ".html", ".xlsx", ".lnk")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def problems(zf: zipfile.ZipFile, words: list[str]) -> list[str]:
    """Why this zip must not be shared - empty when it may."""
    out = []
    low_words = [w.casefold() for w in words]
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if (name.startswith("data/") and name != "data/.gitkeep") or \
                (name.lower().endswith(DATA_SUFFIXES) and not name.startswith("vpa/")):
            out.append(f"{name}: is data, not code")
            continue
        text = zf.read(name).decode("utf-8", errors="ignore").casefold()
        hits = sorted({w for w, lw in zip(words, low_words) if lw in text or lw in name.casefold()})
        if hits:
            out.append(f"{name}: names {', '.join(hits)}")
    return out


def main() -> int:
    from vpa import sites
    words = sites.private_words()
    if not words:
        print("No private site file (data/sites.json) - nothing to check names against.")
    sha = _git("rev-parse", "--short", "HEAD").decode().strip()
    dirty = _git("status", "--porcelain", "--untracked-files=no").decode().strip()
    blob = _git("archive", "--format=zip", "HEAD")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        bad = problems(zf, words)
        n = sum(1 for i in zf.infolist() if not i.is_dir())
    if bad:
        print("NOT built - the committed code is not safe to share:")
        for b in bad:
            print("  " + b)
        return 1
    out = ROOT / "output" / f"vpa-{sha}.zip"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(blob)
    print(f"Built {out} - {n} files from commit {sha}, {len(words)} private words checked.")
    if dirty:
        print("Note: you have uncommitted changes; they are NOT in the zip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
