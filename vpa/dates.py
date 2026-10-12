"""Australian dates for people to read.

Everything stored, keyed, compared or exported keeps ISO dates
(2026-09-19). Text meant for a person is passed through `au()` on the way
out: a day becomes 19/09/2026 and a month Sep 2026. The pages and the phone
app do the same in the browser (vpaAu in ui.py and phone_app/app.js), with
the same rule.
"""
from __future__ import annotations

import re

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAY = re.compile(r"(^|[^\d/-])((?:19|20)\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\d-])")
_MONTH = re.compile(r"(^|[^\d/-])((?:19|20)\d\d)-(0[1-9]|1[0-2])(?![\d/-])")


def au(text) -> str:
    """'captured 2026-09-19' -> 'captured 19/09/2026'; '2026-07' -> 'Jul 2026'."""
    s = "" if text is None else str(text)
    s = _DAY.sub(lambda m: f"{m[1]}{m[4]}/{m[3]}/{m[2]}", s)
    return _MONTH.sub(lambda m: f"{m[1]}{_MON[int(m[3]) - 1]} {m[2]}", s)
