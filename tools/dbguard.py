"""A fingerprint of everything in the database - to prove work left it alone.

    python tools/dbguard.py              print the fingerprint of data/vpa.db
    python tools/dbguard.py --save       ... and remember it (data/dbguard.json)
    python tools/dbguard.py --check      compare with the remembered one;
                                         exit 1 if anything differs
    python tools/dbguard.py --db PATH    another database

The database is opened READ-ONLY (SQLite refuses writes on that
connection). The fingerprint covers every row of every table, in a fixed
order, so it changes if and only if the stored data does - unlike the
file's bytes, which SQLite may rewrite without changing any data. Run
--save before a change to the code and --check after: the code may change,
the data may not unless you meant it to.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "data" / "vpa.db"


def fingerprint(db: Path) -> dict:
    con = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        counts = {}
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            n = 0
            for r in con.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
                h.update(repr(r).encode())
                n += 1
            counts[t] = n
    finally:
        con.close()
    return {"hash": h.hexdigest()[:16], "counts": counts}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    db = Path(argv[argv.index("--db") + 1]) if "--db" in argv else DEFAULT
    stamp = db.resolve().parent / "dbguard.json"
    fp = fingerprint(db)
    print(f"{db}: {fp['hash']}  ({fp['counts'].get('vehicles', 0)} vehicles, "
          f"{fp['counts'].get('observations', 0)} observations)")
    if "--save" in argv:
        stamp.write_text(json.dumps(dict(fp, saved=datetime.now().isoformat(timespec="seconds"),
                                         db=str(db.resolve())), indent=1), encoding="utf-8")
        print(f"remembered in {stamp}")
    if "--check" in argv:
        try:
            old = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("nothing remembered yet - run with --save first")
            return 1
        if old["hash"] == fp["hash"]:
            print(f"UNCHANGED since {old['saved']}")
            return 0
        moved = {t: (old["counts"].get(t), n) for t, n in fp["counts"].items()
                 if old["counts"].get(t) != n}
        print(f"CHANGED since {old['saved']} (was {old['hash']}); row counts moved: {moved or 'none'}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
