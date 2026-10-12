"""The laptop's answers for check_phone_app, worked out in parallel.

Every selection view and car card is an independent call into the page code
(explore.detail_payload / car_payload). Each call used to re-read the whole
database; here each worker process reads the analysis frame ONCE and hands
every call a copy of it - the same memo phonebuild uses - so a call sees
exactly what it would have read. On the real data this turns ~20 minutes
into a couple.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor

_DB = None


def plain(o):
    return json.loads(json.dumps(o, default=lambda v: v.item() if hasattr(v, "item") else str(v)))


def _init(db: str) -> None:
    global _DB
    from vpa import compare, explore
    frame = explore._frame(db)
    explore._frame = lambda db_path: frame.copy()
    compare._frame = explore._frame
    _DB = db


def _detail(spec):
    from vpa import explore
    mk, mo, va, filters = spec
    return plain(explore.detail_payload(_DB, mk, mo, va, **(filters or {})))


def _card(spec):
    from vpa import explore
    mk, mo, va, opts = spec
    return plain(explore.car_payload(_DB, mk, mo, va, **opts))


def answers(db, details: list, cards: list, workers: int | None = None) -> tuple[list, list]:
    """(detail payloads, card payloads), in the order asked."""
    workers = workers or max(1, min(4, (os.cpu_count() or 2) - 2))
    n = len(details) + len(cards)
    if workers == 1 or n < 150:          # small jobs: process start-up costs more
        _init(str(db))
        return [_detail(s) for s in details], [_card(s) for s in cards]
    chunk = max(1, n // (workers * 8))
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(str(db),)) as ex:
        d = list(ex.map(_detail, details, chunksize=chunk))
        c = list(ex.map(_card, cards, chunksize=chunk))
    return d, c
