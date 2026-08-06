"""Segment definitions: make + model + year band, with broader roll-ups.

A segment is where stats and forecasts are computed. Three levels:
    band        'Ford Falcon 2011-2013'   (make+model+year band, the default)
    model_line  'Ford Falcon'             (all years)
    body        'car:sedan' / 'bike:dirt' (whole body type)
Year bands are fixed-width windows (default 3 years) anchored so the same
year always lands in the same band regardless of the data present.
"""
from __future__ import annotations

import re

import pandas as pd

DEFAULT_BAND_WIDTH = 3
UNKNOWN_BAND = "unknown-year"


def band_label(year, width: int = DEFAULT_BAND_WIDTH) -> str:
    if year is None or pd.isna(year):
        return UNKNOWN_BAND
    y = int(year)
    start = y - ((y - 1900) % width)
    return f"{start}-{start + width - 1}"


def add_segment_cols(df: pd.DataFrame, band_width: int = DEFAULT_BAND_WIDTH) -> pd.DataFrame:
    df = df.copy()
    df["model_line"] = df["make"].astype(str) + " " + df["model"].astype(str)
    df["year_band"] = df["year"].map(lambda y: band_label(y, band_width))
    df["segment"] = df["model_line"] + " " + df["year_band"]
    df["body_segment"] = df["vehicle_type"].astype(str) + ":" + df["body_type"].fillna("unknown").astype(str)
    return df


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def segment_groups(df: pd.DataFrame, level: str = "band"):
    """Yield (segment_name, sub_df). level: band | model_line | body."""
    col = {"band": "segment", "model_line": "model_line", "body": "body_segment"}[level]
    for name, sub in df.groupby(col, observed=True):
        yield str(name), sub


def filter_segment(df: pd.DataFrame, query: str, band: str | None = None) -> pd.DataFrame:
    """Case-insensitive substring match on 'make model' (e.g. 'ford falcon',
    'yz250f'). Optional explicit band 'YYYY-YYYY' overrides banding."""
    hay = (df["make"].astype(str) + " " + df["model"].astype(str) + " "
           + df["variant"].fillna("").astype(str)).str.casefold()
    out = df[hay.str.contains(query.strip().casefold(), regex=False)]
    if band:
        m = re.match(r"^(\d{4})\s*-\s*(\d{4})$", band.strip())
        if not m:
            raise ValueError(f"Band must look like 2011-2014, got: {band!r}")
        lo, hi = int(m.group(1)), int(m.group(2))
        out = out[(out["year"] >= lo) & (out["year"] <= hi)]
        out = out.copy()
        out["segment"] = out["model_line"] + f" {lo}-{hi}"
    return out
