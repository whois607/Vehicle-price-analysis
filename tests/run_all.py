"""Run every verification suite in sequence.

    python -m tests.run_all        (from vehicle-price-analysis/)

selfcheck (core pipeline), check_titles (make/model/variant splitting),
check_carsales (seller/state/body from saved carsales pages, both formats),
check_importui (import UI + FB paste + overview),
check_explore (Explore page, selection levels, merged identities, freshness),
check_car (one car judged against its trim: pool basis, gates, fit isolation),
check_compare (Compare page: catalogue, side-by-side stats, ranking),
check_verify (the Data check, run against deliberately broken databases),
check_repair (identity migration), check_edit (manual corrections:
validation, transaction, audit trail, undo), check_starred (the
watchlist), check_turnover (what left view between captures, and the four
ways that measurement lies), check_bulk (direct upload: backup before, undo
after), check_perf (each speed-up against the code it replaced), check_quickadd, check_viewer
(Excel workbook via real Excel COM - needs Excel installed and viewer test
files closed). Exit 0 = everything.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

SUITES = ["tests.selfcheck", "tests.check_titles", "tests.check_carsales", "tests.check_importui",
          "tests.check_explore", "tests.check_car", "tests.check_compare",
          "tests.check_verify", "tests.check_repair", "tests.check_merge", "tests.check_edit", "tests.check_starred",
          "tests.check_turnover", "tests.check_bulk", "tests.check_perf",
          "tests.check_quickadd", "tests.check_viewer"]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    failed = []
    t0 = time.time()
    for suite in SUITES:
        print(f"\n{'=' * 70}\n== {suite}\n{'=' * 70}")
        t = time.time()
        proc = subprocess.run([sys.executable, "-m", suite], cwd=root)
        print(f"-- {suite}: {'OK' if proc.returncode == 0 else 'FAILED'} "
              f"({time.time() - t:.0f}s)")
        if proc.returncode != 0:
            failed.append(suite)
    print(f"\n{'=' * 70}")
    if failed:
        print(f"FAILED suites: {', '.join(failed)}  ({time.time() - t0:.0f}s total)")
        return 1
    print(f"all {len(SUITES)} suites passed  ({time.time() - t0:.0f}s total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
