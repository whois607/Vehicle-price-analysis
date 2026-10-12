"""Which lines of vpa/ a test suite runs - no third-party package needed.

    python tools/linecov.py tests.check_links out.json

Runs the suite's module as __main__ in this process with Python 3.12's
sys.monitoring recording every line executed in vpa/, and writes
{"file": [lines...]} to out.json. Work a suite hands to a subprocess (a
server, `python -m vpa ...`) is not seen. Used by tools/suite_overlap.py
to find suites that cover nothing the others do not.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = str(ROOT / "vpa")


def main() -> int:
    mod, out = sys.argv[1], Path(sys.argv[2])
    sys.path.insert(0, str(ROOT))
    seen: dict[str, set[int]] = {}
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "linecov")

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(PKG):
            seen.setdefault(f, set()).add(line)
        return mon.DISABLE            # each line once is enough

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)
    sys.argv = [mod]
    rc = 0
    try:
        runpy.run_module(mod, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)
        out.write_text(json.dumps({str(Path(f).relative_to(ROOT)): sorted(v) for f, v in seen.items()}),
                       encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
