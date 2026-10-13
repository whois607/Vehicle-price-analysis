"""The app on a throwaway copy of your data - for trying a change by hand.

    python tools/scratch_app.py            copy, then serve on http://127.0.0.1:8873
    python tools/scratch_app.py --port 8874

Copies data/vpa.db (through a read-only connection) and the files beside it
(photos, details, searches) into
tests/synthetic/scratch_app/ and runs the app on the copy, so a merge,
an edit or an import tried there never reaches your real database. The
app does not run phone sync for a scratch database. Stop it with Ctrl+C
or the app's own Shut down button; the copy is replaced on the next run.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "tests" / "synthetic" / "scratch_app" / "data"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    port = argv[argv.index("--port") + 1] if "--port" in argv else "8873"
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    src = sqlite3.connect(f"file:{(ROOT / 'data' / 'vpa.db').as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(WORK / "vpa.db")
    src.backup(dst)
    dst.close()
    src.close()
    for name in ("photos.json", "details.json", "sweeps.json"):
        if (ROOT / "data" / name).exists():
            shutil.copy(ROOT / "data" / name, WORK / name)
    print(f"serving a copy of your data at http://127.0.0.1:{port} - changes there are thrown away")
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "vpa", "--db", str(WORK / "vpa.db"),
                           "import-ui", "--port", port, "--no-browser"], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
