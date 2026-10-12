"""Does the working tree answer exactly what the committed code answers,
on YOUR data? The check to run before committing a refactor.

    python tools/equiv.py                 against HEAD (the last commit)
    python tools/equiv.py --ref abc123    against another commit
    python tools/equiv.py --all           every selection and many more cars (slower)

Exports the reference commit (`git archive`) into tests/synthetic/equiv/,
takes two read-only copies of data/vpa.db, and runs tools/_equiv_dump.py
under each code tree with your private site file: every kept page's records,
keys and photos; every car's advert link and site-id status; the Duplicates
page; the analysis frame; Explore for every make and model line and the
largest trims; car cards; the Compare catalogue. Then it says, section by
section, SAME or DIFFERENT with the first differences.

A difference is not automatically wrong - a deliberate change shows up here
too - but every one should be one you meant. The real database is only ever
read; its fingerprint is checked before and after.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "tests" / "synthetic" / "equiv"
sys.path.insert(0, str(ROOT / "tools"))


def _copy(src: Path, dst: Path) -> None:
    s = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


def _first_diffs(a, b, path="", out=None, limit=6):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append(f"{path}: {str(a)[:90]!r} -> {str(b)[:90]!r}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: {'only in new' if k not in a else 'missing in new'}")
            elif a[k] != b[k]:
                _first_diffs(a[k], b[k], f"{path}.{k}", out, limit)
            if len(out) >= limit:
                break
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} items -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                _first_diffs(x, y, f"{path}[{i}]", out, limit)
            if len(out) >= limit:
                break
    elif a != b:
        out.append(f"{path}: {str(a)[:90]!r} -> {str(b)[:90]!r}")
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ref = argv[argv.index("--ref") + 1] if "--ref" in argv else "HEAD"
    db = ROOT / "data" / "vpa.db"
    import dbguard
    before = dbguard.fingerprint(db)["hash"]
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "ref").mkdir(parents=True)
    blob = subprocess.run(["git", "archive", ref], cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(WORK / "ref", filter="data")
    for name in ("old.db", "new.db"):
        _copy(db, WORK / name)
    env = dict(os.environ)
    if (ROOT / "data" / "sites.json").exists():
        env["VPA_SITES"] = str(ROOT / "data" / "sites.json")
    dump = ROOT / "tools" / "_equiv_dump.py"
    extra = ["--all"] if "--all" in argv else []
    t = time.time()
    print(f"comparing {ref} with the working tree on a copy of {db} ...", flush=True)
    procs = [subprocess.Popen([sys.executable, "-X", "utf8", str(dump), str(tree), str(WORK / f"{n}.db"),
                               str(WORK / f"{n}.json"), *extra], cwd=str(tree), env=env,
                              stdout=subprocess.DEVNULL, stderr=open(WORK / f"{n}.log", "w"))
             for n, tree in (("old", WORK / "ref"), ("new", ROOT))]
    rcs = [p.wait() for p in procs]
    if any(rcs) or not all((WORK / f"{n}.json").exists() for n in ("old", "new")):
        print("a dump failed - see", WORK / "old.log", "and", WORK / "new.log")
        return 2
    a = json.loads((WORK / "old.json").read_text(encoding="utf-8"))
    b = json.loads((WORK / "new.json").read_text(encoding="utf-8"))
    same = True
    for name in sorted(set(a) | set(b)):
        x, y = a.get(name), b.get(name)
        n = len(x) if isinstance(x, (list, dict)) else ""
        if x == y:
            print(f"  SAME       {name} ({n})")
            continue
        same = False
        print(f"  DIFFERENT  {name}")
        for d in _first_diffs(x, y):
            print(f"               {d}")
    after = dbguard.fingerprint(db)["hash"]
    print(f"\n{'everything the same' if same else 'differences above - each should be one you meant'}"
          f"  ({time.time() - t:.0f}s)")
    print(f"the real database: {before} -> {after} {'UNCHANGED' if after == before else 'CHANGED'}")
    return 0 if same and after == before else 1


if __name__ == "__main__":
    sys.exit(main())
