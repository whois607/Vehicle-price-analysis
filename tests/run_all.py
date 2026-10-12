"""Run every verification suite.

    python -m tests.run_all              every suite, several at once (~1 min)
    python -m tests.run_all --quick      without the Excel suite (needs Excel, slow)
    python -m tests.run_all --real       ... then the phone app against a read-only
                                         copy of YOUR data with YOUR site file
                                         (~3 min), proving the database untouched
    python -m tests.run_all --serial     one at a time, output streamed (debugging)
    python -m tests.run_all --only check_trims,check_dupes

Each suite works in its own folder under tests/synthetic/, so they run side
by side; each one's full output is kept in tests/synthetic/logs/<suite>.log
and a failing suite's FAIL lines are printed. The Excel suite drives the one
real Excel, so it runs alone, after the rest. Exit 0 = everything passed.

The suites:
  selfcheck        the core pipeline on synthetic data with known answers
  check_titles     make / model / trim from listing titles
  check_sitepages  seller, state and body from saved pages; advert links
  check_importui   the import UI over live HTTP, pasted tiles, overview
  check_explore    Explore: selection levels, merged identities, freshness
  check_car        one car judged against its trim
  check_compare    Compare: catalogue, side-by-side stats, ranking
  check_verify     the Data check, against deliberately broken databases
  check_repair     identity migration
  check_merge      the duplicate rule, one condition broken at a time
  check_dupes      the Duplicates page: sorting, merge, exact undo
  check_edit       manual corrections: validation, transaction, undo
  check_starred    the watchlist
  check_turnover   what left view between captures, and when it refuses
  check_bulk       direct upload: backup before, undo after
  check_inbox      the watch folder: a real session, undo
  check_quickadd   Quick Add over live HTTP
  check_workbook   how viewer.xlsx is built: cache, safe save, stamp
  check_phone      phone sync: read-only copy, pairing, the listener
  check_phone_app  the phone app's numbers against the laptop's (needs node)
  check_phone_stars  stars from phones
  check_photos     listing photo addresses, never the database
  check_savewarn   saves that went wrong: cut-off pages, a page saved twice
  check_trims      trim names grouped on read, stored names untouched
  check_neutral    the shared files name no site; works with no site file
  check_viewer     the workbook's formulas in real Excel (COM)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SUITES = ["tests.selfcheck", "tests.check_titles", "tests.check_sitepages",
          "tests.check_importui", "tests.check_explore", "tests.check_car", "tests.check_compare",
          "tests.check_verify", "tests.check_repair", "tests.check_merge", "tests.check_dupes",
          "tests.check_edit", "tests.check_starred", "tests.check_turnover", "tests.check_bulk",
          "tests.check_inbox", "tests.check_quickadd", "tests.check_workbook", "tests.check_phone",
          "tests.check_phone_app", "tests.check_phone_stars", "tests.check_photos",
          "tests.check_savewarn", "tests.check_trims", "tests.check_neutral",
          "tests.check_viewer"]
#: drives the one real Excel - never beside anything else
ALONE = {"tests.check_viewer"}

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "tests" / "synthetic" / "logs"
TIMES = LOGS / "times.json"


def _run(suite: str, extra=(), env=None) -> tuple[str, int, float]:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{suite.split('.')[-1]}.log"
    t = time.time()
    with open(log, "w", encoding="utf-8") as f:
        rc = subprocess.run([sys.executable, "-X", "utf8", "-m", suite, *extra], cwd=ROOT,
                            stdout=f, stderr=subprocess.STDOUT, env=env).returncode
    return suite, rc, time.time() - t


def _report(suite: str, rc: int, secs: float) -> None:
    log = LOGS / f"{suite.split('.')[-1]}.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    tally = next((ln.strip("= ") for ln in reversed(text.splitlines()) if "checks passed" in ln), "")
    print(f"-- {suite}: {'OK' if rc == 0 else 'FAILED'} ({secs:.0f}s) {tally}", flush=True)
    if rc:
        bad = [ln for ln in text.splitlines() if "[FAIL]" in ln or "Error" in ln][:8]
        for ln in bad or text.splitlines()[-8:]:
            print("     " + ln[:200])
        print(f"     full output: {log}")


def _real(db: Path) -> bool:
    """The phone app against a read-only copy of the real data, with the
    private site file - and the real database's fingerprint unchanged."""
    sys.path.insert(0, str(ROOT / "tools"))
    import dbguard
    sites = ROOT / "data" / "sites.json"
    before = dbguard.fingerprint(db)["hash"]
    env = dict(os.environ)
    if sites.exists():
        env["VPA_SITES"] = str(sites)
    print(f"\n== real data: phone app against a read-only copy of {db} ==", flush=True)
    suite, rc, secs = _run("tests.check_phone_app", ["--db", str(db)], env)
    _report("tests.check_phone_app", rc, secs)
    after = dbguard.fingerprint(db)["hash"]
    print(f"-- the real database: {before} -> {after} "
          f"{'UNCHANGED' if after == before else 'CHANGED - investigate before anything else'}")
    return rc == 0 and after == before


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    suites = list(SUITES)
    if "--only" in argv:
        want = {s if s.startswith("tests.") else "tests." + s
                for s in argv[argv.index("--only") + 1].split(",")}
        suites = [s for s in suites if s in want]
    if "--quick" in argv:
        suites = [s for s in suites if s not in ALONE]
    try:
        times = json.loads(TIMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        times = {}
    t0 = time.time()
    results: dict[str, int] = {}
    side = [s for s in suites if s not in ALONE]
    if "--serial" in argv:
        for s in suites:
            print(f"\n{'=' * 70}\n== {s}\n{'=' * 70}", flush=True)
            t = time.time()
            results[s] = subprocess.run([sys.executable, "-X", "utf8", "-m", s], cwd=ROOT).returncode
            times[s] = time.time() - t
            print(f"-- {s}: {'OK' if results[s] == 0 else 'FAILED'} ({times[s]:.0f}s)")
    else:
        workers = max(2, min(6, (os.cpu_count() or 4) - 2))
        side.sort(key=lambda s: -times.get(s, 30))         # longest first packs best
        print(f"running {len(side)} suites, {workers} at a time"
              + (f", then {len(suites) - len(side)} alone" if len(suites) > len(side) else ""),
              flush=True)
        with ThreadPoolExecutor(workers) as ex:
            for fut in as_completed([ex.submit(_run, s) for s in side]):
                s, rc, secs = fut.result()
                results[s], times[s] = rc, secs
                _report(s, rc, secs)
        for s in suites:
            if s in ALONE:
                s, rc, secs = _run(s)
                results[s], times[s] = rc, secs
                _report(s, rc, secs)
    LOGS.mkdir(parents=True, exist_ok=True)
    TIMES.write_text(json.dumps(times, indent=1), encoding="utf-8")
    failed = [s for s in suites if results.get(s)]
    ok = not failed
    if "--real" in argv:
        nxt = argv[argv.index("--real") + 1] if len(argv) > argv.index("--real") + 1 else ""
        db = Path(nxt) if nxt and not nxt.startswith("--") else ROOT / "data" / "vpa.db"
        ok = _real(db) and ok
        if not ok and not failed:
            failed = ["real-data check"]
    print(f"\n{'=' * 70}")
    print(f"{'all ' + str(len(suites)) + ' suites passed' if not failed else 'FAILED: ' + ', '.join(failed)}"
          f"  ({time.time() - t0:.0f}s total)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
