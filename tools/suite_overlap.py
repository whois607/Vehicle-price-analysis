"""Which test suites cover code no other suite does.

    python tools/suite_overlap.py <folder of tools/linecov.py outputs>

For each suite: lines of vpa/ it runs, and how many of those NO other suite
runs. A suite with few unique lines is a candidate for folding into another
- not proof it is redundant: two suites can run the same lines to check
different things. Read what it asserts before removing anything.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    folder = Path(sys.argv[1])
    cov = {p.stem: {(f, ln) for f, lines in json.loads(p.read_text(encoding="utf-8")).items()
                    for ln in lines}
           for p in sorted(folder.glob("*.json"))}
    count = Counter(x for s in cov.values() for x in s)
    union = set(count)
    print(f"{'suite':22} {'lines':>6} {'unique':>7}  unique files")
    for name, s in sorted(cov.items(), key=lambda kv: sum(count[x] == 1 for x in kv[1])):
        uniq = [x for x in s if count[x] == 1]
        files = Counter(f for f, _ in uniq).most_common(3)
        print(f"{name:22} {len(s):6} {len(uniq):7}  "
              + ", ".join(f"{Path(f).name}:{n}" for f, n in files))
    print(f"\nall suites together: {len(union)} lines of vpa/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
