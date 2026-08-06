"""Adapter registry: one small module per input shape, all emitting raw field
dicts that schema.make_record() normalises. Add a new source by adding a
module with `parse(path_or_text, ...) -> list[dict]` and registering it here.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ..schema import Record, make_record, file_mtime_date
from . import tabular, htmlpage, pastetext


def detect_format(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {".csv": "csv", ".json": "json", ".html": "html", ".htm": "html",
            ".txt": "text", ".text": "text"}.get(suffix, "text")


def parse_file(path: str | Path, source: str | None = None,
               fmt: str | None = None, capture_date: str | None = None
               ) -> tuple[list[Record], str, int]:
    """Parse one file into normalised Records.
    Returns (records, adapter_name, raw_count) where raw_count is how many
    raw blocks the adapter found — raw_count - len(records) were dropped
    for having no usable make+model.

    capture_date precedence: explicit CLI value > per-row value in the data
    > file modification time (when you saved the page) — never silently today.
    """
    path = Path(path)
    fmt = fmt or detect_format(path)
    fallback = date.fromisoformat(capture_date) if capture_date else file_mtime_date(path)

    if fmt == "csv":
        raws, src = tabular.parse_csv(path), source or "other"
    elif fmt == "json":
        raws, src = tabular.parse_json(path), source or "other"
    elif fmt == "html":
        raws, detected = htmlpage.parse(path)
        src = source or detected
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        raws, src = pastetext.parse(text), source or "other"

    records = []
    for raw in raws:
        row_source = raw.pop("source", None) or src
        if capture_date:
            raw["capture_date"] = capture_date   # explicit flag wins over row value
        rec = make_record(raw, source=str(row_source).casefold(), capture_fallback=fallback)
        if rec:
            records.append(rec)
    return records, fmt, len(raws)


def load_file(path: str | Path, source: str | None = None,
              fmt: str | None = None, capture_date: str | None = None
              ) -> tuple[list[Record], str]:
    """parse_file without the raw count — the original public signature."""
    records, fmt, _ = parse_file(path, source, fmt, capture_date)
    return records, fmt
