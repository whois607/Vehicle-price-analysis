"""viewer.xlsx - refreshed sheets, your styling, and a sheet of your own.

    python -m vpa viewer                  (or the app's "Excel viewer" button)
    python -m vpa viewer --reset-styles   (also restore the default look)

THREE KINDS OF SHEET
--------------------
BACKEND - names start with an underscore. Pure data, rewritten from scratch
every refresh, so never edit them:
    _Data       every observation, one row each
    _Listings   full imported rows only, with a filter-following stats strip
    _Forecasts  the forecast ladder's output (values; predictions cannot be
                spreadsheet formulas)
    _Picks      dropdown sources and index lists
    _Chart      the scatter feed (its #N/A cells are deliberate chart gaps)

VIEWS - Picker, Trend, Supply, Comparison, RichStats, Forecasts, Models.
Rewritten every refresh and sized to the data, BUT styling you applied to
cells that already existed is kept: fonts, fills, number formats, column
widths, row heights. So restyle them freely; just don't expect moved blocks
or hand-added columns to stay put, because the content is regenerated.

MYVIEW - created once and then never written to again. This is the sheet to
build your own layout on: reference the named ranges below (or point cells
at the views) and nothing will ever overwrite it. Any other sheet you add
yourself is equally untouched.

WHY THIS STAYS CORRECT AS DATA GROWS
------------------------------------
Views read WORKBOOK NAMED RANGES (vpa_price, vpa_line, vpa_selection, ...)
which the refresh re-points at the new data extent, so formulas you write
on MyView keep working. Row blocks (months, capture days, variants) are
sized to the actual data each refresh - fixed oversized blocks made every
chart trail off through dozens of empty categories.

THE PICKER
----------
One place chooses the vehicle for every view: the Picker sheet's brand ->
model -> series/trim dropdowns, whose resolved text is the named range
vpa_selection. Selections match by EXACT equality against three disjoint
columns (make / model_line / sel_key), so "Ford" pools the brand, "Ford
Falcon" pools every trim, and a trim matches only itself - a trim that is a
word-prefix of a sibling never swallows it.

Functions newer than Excel 2007 need the _xlfn. prefix or Excel shows #NAME?.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, Reference, ScatterChart, Series
from openpyxl.chart.marker import Marker
from openpyxl.drawing.line import LineProperties
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.formula import ArrayFormula

from . import analyze, forecast, links
from .segments import add_segment_cols
from .store import Store, DEFAULT_DB

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "output" / "viewer.xlsx"

# Bumped whenever views must be rebuilt to keep working - e.g. when a
# backend sheet is renamed. A workbook whose stamp differs has views that
# reference sheets/names this version no longer creates: leaving them alone
# would strand their formulas (Excel reads the missing sheet as a broken
# external-workbook link and shows #VALUE! everywhere), so they are rebuilt.
# Bumped to 4 on 03/08/2026: the Comparison sheet grew from 14 rows to a full
# stat panel plus a month block and chart, so an existing workbook's old
# Comparison layout must be rebuilt rather than partially overwritten.
# Bumped to 5 on 03/08/2026: vpa_sel_list gained make-level rows, so the Models
# sheet and the Comparison dropdown are longer than the previous layout sized
# them for.
LAYOUT_VERSION = "5"

BACKEND = ["_Data", "_Listings", "_Forecasts", "_Picks", "_Chart"]
VIEWS = ["Picker", "Trend", "Supply", "Comparison", "RichStats", "Forecasts",
         "Models"]
# never written after creation - your sandbox for a layout of your own
SANDBOX = "MyView"
SHEET_ORDER = VIEWS + [SANDBOX] + BACKEND
LEGACY = ["Movement", "Stock", "Data", "PickLists", "ChartData", "Listings",
          "README"]

ARIAL = "Arial"
F_BASE = Font(name=ARIAL, size=10)
F_BOLD = Font(name=ARIAL, size=10, bold=True)
F_TITLE = Font(name=ARIAL, size=13, bold=True)
F_NOTE = Font(name=ARIAL, size=9, italic=True, color="666666")
F_WARN = Font(name=ARIAL, size=10, bold=True, color="A03030")
F_LINK = Font(name=ARIAL, size=10, color="008000")
F_PASTED = Font(name=ARIAL, size=10, color="0000FF")
F_INPUT = Font(name=ARIAL, size=12, bold=True, color="0000FF")
F_LABEL = Font(name=ARIAL, size=9, color="556070")
FILL_INPUT = PatternFill("solid", start_color="FFF2B8")
FILL_HDR = PatternFill("solid", start_color="EEF0F4")
FILL_BACKEND = PatternFill("solid", start_color="FDE9E9")
_side = Side(style="thin", color="B0A040")
BOX = Border(left=_side, right=_side, top=_side, bottom=_side)

FMT_MONEY = "$#,##0"
FMT_KM = "#,##0"
FMT_PCT = "0.0%"
FMT_DATE = "DD/MM/YYYY"

BLUE, RED, GREEN, SAND = "3060A0", "C0504D", "7AA874", "B0A470"

SEL = "vpa_selection"

DATA_COLS = ["model_line", "detail_level", "capture_date", "month", "make",
             "model", "variant", "year", "price_aud", "odometer_km", "state",
             "seller_type", "source", "year_band", "ln_price", "listing_id",
             "url", "day", "sel_key"]
# column letter in _Data -> named range exposed to the views
DATA_NAMES = {"A": "vpa_line", "B": "vpa_detail", "C": "vpa_date",
              "D": "vpa_month", "E": "vpa_make", "G": "vpa_variant",
              "H": "vpa_year", "I": "vpa_price", "J": "vpa_odo",
              "K": "vpa_state", "L": "vpa_seller", "M": "vpa_source",
              "O": "vpa_lnprice", "R": "vpa_day", "S": "vpa_key"}


# ------------------------------------------------------------------ helpers
# When a sheet already existed, values/formulas are rewritten but styling on
# cells that were already there is left alone - so restyling a managed view
# survives a refresh. Cells beyond the old extent get our default styling.
_CTX = {"preserve": False, "rows": 0, "cols": 0}


def _keep_style(row, col) -> bool:
    return _CTX["preserve"] and row <= _CTX["rows"] and col <= _CTX["cols"]


def _put(ws, row, col, value, font=F_BASE, fmt=None, fill=None, wrap=False):
    c = ws.cell(row=row, column=col)
    c.value = value
    if _keep_style(row, col):
        return c
    c.font = font
    # Always state the format, never inherit it. sheet() blanks values but
    # not number formats, so a cell whose MEANING changed between layouts
    # would otherwise keep the old one - which is how "distinct capture
    # days" once rendered as "$9".
    c.number_format = fmt or "General"
    if fill:
        c.fill = fill
    if wrap:
        c.alignment = Alignment(wrap_text=True, vertical="top")
    return c


def _arr(ws, coord, formula, font=F_BASE, fmt=None):
    ws[coord] = ArrayFormula(coord, formula)
    c = ws[coord]
    if _keep_style(c.row, c.column):
        return
    c.font = font
    c.number_format = fmt or "General"


def _widths(ws, mapping):
    if _CTX["preserve"]:
        return                       # your column widths, not ours
    for col, w in mapping.items():
        ws.column_dimensions[col].width = w


def _input_cell(ws, row, col, value):
    c = _put(ws, row, col, value, font=F_INPUT, fill=FILL_INPUT)
    c.border = BOX
    c.alignment = Alignment(horizontal="center", vertical="center")
    return c


def _show_axes(chart, x_title=None, y_title=None, y_money=False):
    """openpyxl leaves axes hidden in Excel unless both of these are set."""
    for axis in (chart.x_axis, chart.y_axis):
        axis.delete = False
        axis.tickLblPos = "nextTo"
    if x_title:
        chart.x_axis.title = x_title
    if y_title:
        chart.y_axis.title = y_title
    if y_money:
        chart.y_axis.numFmt = FMT_MONEY
        chart.y_axis.sourceLinked = False


def _style_line(chart, color=RED, y_title=None, x_title=None, y_money=True):
    chart.legend = None
    _show_axes(chart, x_title, y_title, y_money)
    s = chart.series[0]
    s.graphicalProperties.line = LineProperties(solidFill=color, w=24000)
    s.marker = Marker(symbol="circle", size=6)
    s.marker.graphicalProperties.solidFill = color
    s.smooth = False
    chart.y_axis.majorGridlines = None
    chart.height, chart.width = 7.2, 14.5


def _style_bar(chart, color=GREEN, y_title=None, x_title=None, y_money=False):
    chart.legend = None
    _show_axes(chart, x_title, y_title, y_money)
    chart.series[0].graphicalProperties.solidFill = color
    chart.gapWidth = 50
    chart.height, chart.width = 7.2, 14.5


class Match:
    """Selection matching over three DISJOINT identity columns, by exact
    equality. At most one can equal a given selection, so OR-summing the
    three can never double count."""

    COLS = ("vpa_make", "vpa_line", "vpa_key")

    def __init__(self, sel: str = SEL):
        self.sel = sel

    def count(self, extra: str = "") -> str:
        return ("(" + "+".join(f"COUNTIFS({c},{self.sel}{extra})"
                               for c in self.COLS) + ")")

    def cond(self) -> str:
        return "(" + "+".join(f"({c}={self.sel})" for c in self.COLS) + ")"


def _name(wb, name: str, ref: str):
    if name in wb.defined_names:
        del wb.defined_names[name]
    wb.defined_names[name] = DefinedName(name, attr_text=ref)


# ---------------------------------------------------------------- generate
def generate(db_path=DEFAULT_DB, out_path: Path = DEFAULT_OUT,
             reset_styles: bool = False) -> Path:
    with Store(db_path) as store:
        df = add_segment_cols(store.frame())
    if df.empty:
        raise RuntimeError("Dataset is empty - ingest something first.")
    df = df.sort_values(["model_line", "capture_date"]).reset_index(drop=True)
    df["day"] = df["capture_date"].dt.strftime("%Y-%m-%d")
    df["sel_key"] = df.apply(
        lambda r: "-" if not isinstance(r["variant"], str) or not r["variant"].strip()
        else f"{r['model_line']} {r['variant']}", axis=1)

    by_line = df.groupby("model_line")["price_aud"].count().sort_values(ascending=False)
    top = df[df["model_line"] == by_line.index[0]].iloc[0]
    second = (df[df["model_line"] == by_line.index[1]].iloc[0]
              if len(by_line) > 1 else top)
    makes = sorted(df["make"].unique())
    mm = df[["make", "model"]].drop_duplicates().sort_values(["make", "model"])
    mmv = (df.dropna(subset=["variant"])[["make", "model", "variant"]]
           .drop_duplicates().sort_values(["make", "model", "variant"]))
    vpairs = (df[df["sel_key"] != "-"][["model_line", "variant"]]
              .drop_duplicates().sort_values(["model_line", "variant"]))
    # Every selection Match can resolve, at all THREE of its levels. Makes
    # used to be missing here, so "Ford" worked in the app but the workbook's
    # dropdown rejected it - the same label has to mean the same thing in both.
    selections = (list(makes) + list(by_line.index)
                  + sorted({k for k in df["sel_key"] if k != "-"}))
    full_df = df[df["detail_level"] == "full"].copy()
    # blocks are sized to the DATA, so charts plot real points only - fixed
    # oversized slots made every chart trail off through empty categories
    months = [str(p) for p in pd.period_range(df["capture_date"].min(),
                                              df["capture_date"].max(), freq="M")]
    days = sorted(df["day"].unique())

    out_path = Path(out_path)
    wb = load_workbook(out_path) if out_path.exists() else Workbook()
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1:
        wb.remove(wb["Sheet"])

    stamp = None
    if "vpa_layout" in wb.defined_names:
        stamp = wb.defined_names["vpa_layout"].attr_text.strip('"')
    if out_path.exists() and stamp != LAYOUT_VERSION:
        reset_styles = True          # older layout: its styling won't fit
        print("Workbook was built by an older layout - rebuilding the sheets "
              "to match the current one.")
    # stale links to sheets an older layout owned; Excel shows these as
    # broken external workbooks
    wb._external_links = []

    for legacy in LEGACY:                    # sheets from earlier designs
        if legacy in wb.sheetnames:
            del wb[legacy]

    def sheet(name: str):
        """Rewrite a managed sheet's contents. If it already existed, styling
        on the cells it already had is preserved (unless reset_styles)."""
        exists = name in wb.sheetnames
        if exists:
            ws = wb[name]
            _CTX.update(preserve=not reset_styles,
                        rows=ws.max_row, cols=ws.max_column)
            for rng in list(ws.merged_cells.ranges):
                ws.unmerge_cells(str(rng))
            for row in ws.iter_rows():
                for c in row:
                    c.value = None
            ws._charts = []
            ws.data_validations.dataValidation = []
        else:
            _CTX.update(preserve=False, rows=0, cols=0)
            ws = wb.create_sheet(name)
        return ws

    # --- backend -----------------------------------------------------------
    _sheet_data(sheet("_Data"), df)
    fc_rows = _sheet_forecasts_data(sheet("_Forecasts"), df)
    _sheet_picks(sheet("_Picks"), makes, mm, mmv, vpairs, selections, days)
    _sheet_listings(sheet("_Listings"), full_df)
    last = len(df) + 2                 # backend rows: banner=1, header=2, data 3+
    _sheet_chart(sheet("_Chart"), last)

    # --- names the views read (re-pointed every refresh) --------------------
    for col, nm in DATA_NAMES.items():
        _name(wb, nm, f"'_Data'!${col}$3:${col}${last}")
    _name(wb, "vpa_fc_sel", f"'_Forecasts'!$A$3:$A${fc_rows + 2}")
    _name(wb, "vpa_fc_all", f"'_Forecasts'!$A$3:$S${fc_rows + 2}")
    _name(wb, "vpa_brands", f"'_Picks'!$A$3:$A${len(makes) + 2}")
    _name(wb, "vpa_mm_make", f"'_Picks'!$C$3:$C${len(mm) + 2}")
    _name(wb, "vpa_mm_model", f"'_Picks'!$D$3:$D${len(mm) + 2}")
    _name(wb, "vpa_var_key", f"'_Picks'!$F$3:$F${len(mmv) + 2}")
    _name(wb, "vpa_var_name", f"'_Picks'!$G$3:$G${len(mmv) + 2}")
    _name(wb, "vpa_sel_list", f"'_Picks'!$I$3:$I${len(selections) + 2}")
    _name(wb, "vpa_day_list", f"'_Picks'!$O$3:$O${len(days) + 2}")
    _name(wb, "vpa_vp_line", f"'_Picks'!$L$3:$L${len(vpairs) + 2}")
    _name(wb, "vpa_vp_var", f"'_Picks'!$M$3:$M${len(vpairs) + 2}")
    _name(wb, "vpa_selection", "Picker!$B$6")
    _name(wb, "vpa_layout", f'"{LAYOUT_VERSION}"')

    # --- views: refreshed, sized to the data, your styling kept -------------
    _view_picker(sheet("Picker"), top)
    _view_trend(sheet("Trend"), months, days)
    _view_supply(sheet("Supply"), days, len(vpairs))
    _view_comparison(sheet("Comparison"), top, second, months)
    _view_richstats(sheet("RichStats"), last, len(vpairs))
    _view_forecasts(sheet("Forecasts"), len(months))
    _view_models(sheet("Models"), len(selections))
    # the one sheet nothing ever writes to after it is created
    if SANDBOX not in wb.sheetnames:
        _CTX.update(preserve=False, rows=0, cols=0)
        _sheet_sandbox(wb.create_sheet(SANDBOX))

    order = [n for n in SHEET_ORDER if n in wb.sheetnames]
    order += [n for n in wb.sheetnames if n not in order]   # user's own sheets
    wb._sheets = [wb[n] for n in order]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


# ----------------------------------------------------------------- backend
def _backend_banner(ws, text):
    _put(ws, 1, 1, text, font=Font(name=ARIAL, size=10, bold=True, color="A03030"),
         fill=FILL_BACKEND)


def _sheet_data(ws, df: pd.DataFrame):
    _backend_banner(ws, "BACKEND - rewritten on every refresh; edit the view "
                        "sheets instead")
    for j, h in enumerate(DATA_COLS, start=1):
        _put(ws, 2, j, h, font=F_BOLD, fill=FILL_HDR)
    for i, row in enumerate(df.itertuples(index=False), start=3):
        d = row._asdict()
        _put(ws, i, 1, d["model_line"])
        _put(ws, i, 2, d["detail_level"])
        _put(ws, i, 3, d["capture_date"].date(), fmt=FMT_DATE)
        _put(ws, i, 4, f'=TEXT(C{i},"YYYY-MM")')
        _put(ws, i, 5, d["make"])
        _put(ws, i, 6, d["model"])
        _put(ws, i, 7, d["variant"])
        _put(ws, i, 8, int(d["year"]) if pd.notna(d["year"]) else None)
        _put(ws, i, 9, d["price_aud"] if pd.notna(d["price_aud"]) else None,
             fmt=FMT_MONEY)
        _put(ws, i, 10, d["odometer_km"] if pd.notna(d["odometer_km"]) else None,
             fmt=FMT_KM)
        _put(ws, i, 11, d["state"])
        _put(ws, i, 12, d["seller_type"])
        _put(ws, i, 13, d["source"])
        _put(ws, i, 14, d["year_band"])
        _put(ws, i, 15, f'=IF(N(I{i})>0,LN(I{i}),"")')
        _put(ws, i, 16, d["listing_id"])
        _put(ws, i, 17, links.ad_url(d["url"], d["source"]))
        _put(ws, i, 18, d["day"])
        _put(ws, i, 19, d["sel_key"])
    ws.auto_filter.ref = f"A2:S{len(df) + 2}"
    ws.freeze_panes = "A3"
    _widths(ws, {"A": 20, "B": 9, "C": 11, "D": 9, "E": 12, "F": 12, "G": 18,
                 "H": 6, "I": 10, "J": 11, "K": 6, "L": 9, "M": 10, "N": 12,
                 "O": 9, "P": 24, "Q": 30, "R": 11, "S": 30})


def _sheet_forecasts_data(ws, df: pd.DataFrame) -> int:
    """The ladder's output as values - predictions can't be formulas."""
    _backend_banner(ws, "BACKEND - rewritten on every refresh. Forecast values "
                        "from vpa forecast.py; the Forecasts view reads these.")
    _put(ws, 1, 6, f"generated {datetime.now():%d/%m/%Y %H:%M}", font=F_NOTE)
    hdr = ["selection", "method", "n", "buckets", "median now", "direction",
           "m+1", "forecast", "low", "high", "m+2", "forecast", "low", "high",
           "m+3", "forecast", "low", "high", "verdict / caveats"]
    for j, h in enumerate(hdr, start=1):
        _put(ws, 2, j, h, font=F_BOLD, fill=FILL_HDR)

    entries = []
    for line, sub in df.groupby("model_line"):
        entries.append((str(line), sub))
        for var, tsub in sub.dropna(subset=["variant"]).groupby("variant"):
            if tsub["price_aud"].notna().sum() >= 8:
                entries.append((f"{line} {var}", tsub))

    r = 3
    for name, sub in entries:
        prepped = analyze.prep(sub)
        if prepped.empty:
            continue
        mfit = analyze.fit_mileage(prepped)
        prepped = analyze.apply_mileage_adjustment(prepped, mfit)
        pcol = "price_adj" if mfit.get("usable") else "price_aud"
        stats = analyze.bucket_stats(prepped, pcol)
        fc = forecast.forecast_segment(
            prepped, stats, horizon=3, price_col=pcol,
            extra_caveats=analyze.data_caveats(prepped, stats))
        _put(ws, r, 1, name, font=F_PASTED)
        _put(ws, r, 2, fc["method"], font=F_PASTED)
        _put(ws, r, 3, fc["n_total"], font=F_PASTED)
        _put(ws, r, 4, fc["n_buckets"], font=F_PASTED)
        _put(ws, r, 5, fc.get("level"), font=F_PASTED, fmt=FMT_MONEY)
        _put(ws, r, 6, fc.get("direction"), font=F_PASTED)
        pts = fc.get("points")
        for h in range(3):
            base = 7 + h * 4
            if pts is not None and h < len(pts):
                p = pts.iloc[h]
                _put(ws, r, base, f"{pd.Timestamp(p['bucket']):%Y-%m}", font=F_PASTED)
                for k, key in enumerate(("forecast", "lo", "hi"), start=1):
                    _put(ws, r, base + k, float(p[key]), font=F_PASTED, fmt=FMT_MONEY)
        _put(ws, r, 19, fc.get("verdict", "")
             + (" | " + "; ".join(fc["caveats"]) if fc["caveats"] else ""),
             font=F_PASTED, wrap=True)
        r += 1
    ws.auto_filter.ref = f"A2:S{max(r - 1, 3)}"
    ws.freeze_panes = "A3"
    _widths(ws, dict({"A": 30, "B": 12, "C": 6, "D": 8, "E": 12, "F": 9,
                      "S": 70},
                     **{get_column_letter(7 + h * 4): 10 for h in range(3)},
                     **{get_column_letter(8 + h * 4 + k): 11
                        for h in range(3) for k in range(3)}))
    return r - 2                                    # number of forecast rows


def _sheet_picks(ws, makes, mm, mmv, vpairs, selections, days=()):
    _backend_banner(ws, "BACKEND - dropdown sources and index lists. Sorted; "
                        "do not reorder (the pickers use OFFSET/MATCH).")
    for col, title in ((1, "brand"), (3, "make"), (4, "model"),
                       (6, "make|model"), (7, "series / trim"),
                       (9, "selection index"), (12, "model_line"),
                       (13, "variant"), (15, "capture day")):
        _put(ws, 2, col, title, font=F_BOLD, fill=FILL_HDR)
    # every distinct capture day, so a view can ask "on how many of these
    # does my selection appear?" by testing the LIST rather than trying to
    # distinct-count the row-level day column
    for i, d in enumerate(days, start=3):
        _put(ws, i, 15, d)
    for i, mk in enumerate(makes, start=3):
        _put(ws, i, 1, mk)
    for i, row in enumerate(mm.itertuples(index=False), start=3):
        _put(ws, i, 3, row.make)
        _put(ws, i, 4, row.model)
    for i, row in enumerate(mmv.itertuples(index=False), start=3):
        _put(ws, i, 6, f"{row.make}|{row.model}")
        _put(ws, i, 7, row.variant)
    for i, s in enumerate(selections, start=3):
        _put(ws, i, 9, s)
    for i, row in enumerate(vpairs.itertuples(index=False), start=3):
        _put(ws, i, 12, row.model_line)
        _put(ws, i, 13, row.variant)
    _widths(ws, {"A": 16, "C": 14, "D": 16, "F": 26, "G": 22, "I": 34,
                 "L": 22, "M": 22})


def _sheet_listings(ws, full_df: pd.DataFrame):
    _backend_banner(ws, "BACKEND - full imported rows only (brief quick-add "
                        "rows have none of these fields). Filter any column; "
                        "the strip below follows your filters.")
    n = len(full_df)
    first, lastr = 5, 4 + n
    _put(ws, 2, 1, "visible rows", font=F_NOTE)
    _put(ws, 2, 2, f"=SUBTOTAL(103,$B${first}:$B${lastr})" if n else 0, font=F_BOLD)
    _put(ws, 2, 3, "median", font=F_NOTE)
    _put(ws, 2, 4, f'=IF($B$2=0,"-",_xlfn.AGGREGATE(12,5,$F${first}:$F${lastr}))'
         if n else '="-"', font=F_BOLD, fmt=FMT_MONEY)
    _put(ws, 2, 5, "lowest", font=F_NOTE)
    _put(ws, 2, 6, f'=IF($B$2=0,"-",_xlfn.AGGREGATE(5,5,$F${first}:$F${lastr}))'
         if n else '="-"', fmt=FMT_MONEY)
    _put(ws, 2, 7, "highest", font=F_NOTE)
    _put(ws, 2, 8, f'=IF($B$2=0,"-",_xlfn.AGGREGATE(4,5,$F${first}:$F${lastr}))'
         if n else '="-"', fmt=FMT_MONEY)
    _put(ws, 3, 1, '=IF($B$2=0,"no full listings match the current filters - a '
                   'model with only brief quick-add data has no rows here","")',
         font=F_WARN)
    hdr = ["capture_date", "model_line", "variant", "year", "odometer_km",
           "price_aud", "state", "seller_type", "source", "listing_id"]
    for j, h in enumerate(hdr, start=1):
        _put(ws, 4, j, h, font=F_BOLD, fill=FILL_HDR)
    for i, row in enumerate(full_df.itertuples(index=False), start=first):
        d = row._asdict()
        _put(ws, i, 1, d["capture_date"].date(), fmt=FMT_DATE)
        _put(ws, i, 2, d["model_line"])
        _put(ws, i, 3, d["variant"])
        _put(ws, i, 4, int(d["year"]) if pd.notna(d["year"]) else None)
        _put(ws, i, 5, d["odometer_km"] if pd.notna(d["odometer_km"]) else None,
             fmt=FMT_KM)
        _put(ws, i, 6, d["price_aud"] if pd.notna(d["price_aud"]) else None,
             fmt=FMT_MONEY)
        _put(ws, i, 7, d["state"])
        _put(ws, i, 8, d["seller_type"])
        _put(ws, i, 9, d["source"])
        _put(ws, i, 10, d["listing_id"])
    if n:
        ws.auto_filter.ref = f"A4:J{lastr}"
    ws.freeze_panes = "A5"
    _widths(ws, {"A": 11, "B": 20, "C": 18, "D": 6, "E": 11, "F": 10,
                 "G": 6, "H": 9, "I": 10, "J": 24})


def _sheet_chart(ws, last: int):
    _backend_banner(ws, "BACKEND - scatter feed for RichStats. The #N/A cells "
                        "are intentional: charts skip them.")
    _put(ws, 2, 1, "odometer_km", font=F_BOLD, fill=FILL_HDR)
    _put(ws, 2, 2, "price_aud", font=F_BOLD, fill=FILL_HDR)
    for i in range(3, last + 1):
        ok = (f"((('_Data'!$A{i}={SEL})+('_Data'!$S{i}={SEL})"
              f"+('_Data'!$E{i}={SEL}))*('_Data'!$B{i}=\"full\")"
              f"*(N('_Data'!$I{i})>0)*(N('_Data'!$J{i})>0))>0")
        _put(ws, i, 1, f"=IF({ok},'_Data'!$J{i},NA())", fmt=FMT_KM)
        _put(ws, i, 2, f"=IF({ok},'_Data'!$I{i},NA())", fmt=FMT_MONEY)
    _widths(ws, {"A": 14, "B": 14})


def _sheet_sandbox(ws):
    """Created once, never written again - build your own layout here."""
    _put(ws, 1, 1, "MyView - this sheet is yours", font=F_TITLE)
    _put(ws, 2, 1, "Nothing ever overwrites this sheet (or any other sheet "
                   "you add yourself). Build the layout you want here and "
                   "pull numbers in with the named ranges below - they are "
                   "re-pointed at the new data on every refresh, so your "
                   "formulas keep working as the dataset grows.",
         font=F_NOTE, wrap=True)
    ws.row_dimensions[2].height = 30
    _put(ws, 4, 1, "named range", font=F_BOLD, fill=FILL_HDR)
    _put(ws, 4, 2, "what it holds", font=F_BOLD, fill=FILL_HDR)
    rows = [
        ("vpa_selection", "the Picker's current selection (one cell)"),
        ("vpa_make / vpa_line / vpa_key", "make, 'Make Model', 'Make Model Trim' "
         "per row - match a selection against these three, exactly"),
        ("vpa_price", "price of each observation"),
        ("vpa_date / vpa_day / vpa_month", "capture date, 'YYYY-MM-DD', 'YYYY-MM'"),
        ("vpa_odo / vpa_year / vpa_state", "odometer, year, state"),
        ("vpa_seller / vpa_source / vpa_detail", "dealer or private, source, "
         "'full' (imported) or 'brief' (quick add)"),
        ("vpa_variant / vpa_lnprice", "trim, and ln(price) for regressions"),
        ("vpa_sel_list / vpa_vp_line + vpa_vp_var", "every selection; every "
         "model_line + variant pair"),
        ("vpa_fc_sel / vpa_fc_all", "forecast rows: selection column, whole table"),
    ]
    for i, (name, what) in enumerate(rows, start=5):
        _put(ws, i, 1, name, font=F_BOLD)
        _put(ws, i, 2, what, wrap=True)
    _put(ws, 15, 1, "Example - median price of the current selection:",
         font=F_NOTE)
    _put(ws, 16, 1, "{=MEDIAN(IF(((vpa_make=vpa_selection)+(vpa_line="
                    "vpa_selection)+(vpa_key=vpa_selection))*(vpa_price>0),"
                    "vpa_price))}", font=F_NOTE)
    _put(ws, 17, 1, "(array formula - type it without the braces and press "
                    "Ctrl+Shift+Enter)", font=F_NOTE)
    _widths(ws, {"A": 34, "B": 80})


# ------------------------------------------------------------------- views
def _view_note(ws, row=1):
    _put(ws, row, 8, "Refreshed each update, but your fonts/fills/widths are "
                     "kept. Want a layout nothing touches? Use the MyView "
                     "sheet.", font=F_NOTE)


def _view_picker(ws, top):
    _put(ws, 1, 1, "Pick a vehicle", font=F_TITLE)
    _put(ws, 2, 1, "Everything on Trend, Supply, Comparison, RichStats and "
                   "Forecasts follows this choice. Pick left to right: "
                   "changing the brand does not clear the model you already "
                   "chose, so re-pick it. Blank trim = all trims; blank model "
                   "as well = the whole brand.", font=F_NOTE, wrap=True)
    ws.row_dimensions[2].height = 28

    _put(ws, 4, 1, "brand", font=F_LABEL)
    _input_cell(ws, 4, 2, top["make"])
    _put(ws, 5, 1, "model", font=F_LABEL)
    _input_cell(ws, 5, 2, top["model"])
    _put(ws, 4, 3, "series / trim", font=F_LABEL)
    _input_cell(ws, 4, 4, "")
    ws.row_dimensions[4].height = 20
    ws.row_dimensions[5].height = 20

    dv_b = DataValidation(type="list", formula1="=vpa_brands",
                          allow_blank=True, showDropDown=False)
    dv_m = DataValidation(
        type="list", allow_blank=True, showDropDown=False,
        formula1=("=OFFSET(INDEX(vpa_mm_model,1),MATCH($B$4,vpa_mm_make,0)-1,0,"
                  "COUNTIF(vpa_mm_make,$B$4),1)"))
    dv_t = DataValidation(
        type="list", allow_blank=True, showDropDown=False,
        formula1=('=OFFSET(INDEX(vpa_var_name,1),'
                  'MATCH($B$4&"|"&$B$5,vpa_var_key,0)-1,0,'
                  'COUNTIF(vpa_var_key,$B$4&"|"&$B$5),1)'))
    for dv, cell in ((dv_b, "B4"), (dv_m, "B5"), (dv_t, "D4")):
        ws.add_data_validation(dv)
        dv.add(ws[cell])

    _put(ws, 6, 1, "selection", font=F_BOLD)
    _put(ws, 6, 2, '=IF($B$4="","",TRIM($B$4&IF($B$5=""," "," "&$B$5)'
                   '&IF($D$4=""," "," "&$D$4)))',
         font=Font(name=ARIAL, size=12, bold=True))
    m = Match()
    _put(ws, 7, 1, "prices recorded", font=F_LABEL)
    _put(ws, 7, 2, f'={m.count(",vpa_price,\">0\"")}', font=F_BOLD)
    _put(ws, 8, 1, f'=IF($B$7=0,"nothing recorded for this selection yet - '
                   f'check the trim spelling on the Models sheet","")',
         font=F_WARN)
    _put(ws, 10, 1, "The named range vpa_selection points at B6; every view "
                    "reads it. Sheets whose names start with _ are backend "
                    "data, rewritten on each refresh.", font=F_NOTE, wrap=True)
    _widths(ws, {"A": 16, "B": 26, "C": 14, "D": 26})


def _view_trend(ws, months, days):
    m = Match()
    pooled = f"{m.cond()}*(vpa_price>0)"
    _put(ws, 1, 1, "Price trend", font=F_TITLE)
    _view_note(ws)
    _put(ws, 2, 1, "selection", font=F_LABEL)
    _put(ws, 2, 2, f"={SEL}", font=F_BOLD).font = F_LINK

    _put(ws, 2, 5, "prices (n)", font=F_NOTE)
    _put(ws, 2, 6, f'={m.count(",vpa_price,\">0\"")}', font=F_BOLD)
    _put(ws, 3, 5, "full", font=F_NOTE)
    _put(ws, 3, 6, f'={m.count(",vpa_price,\">0\",vpa_detail,\"full\"")}')
    _put(ws, 4, 5, "brief", font=F_NOTE)
    _put(ws, 4, 6, f'={m.count(",vpa_price,\">0\",vpa_detail,\"brief\"")}')
    _put(ws, 3, 1, '=IF($F$2=0,"no prices recorded for this selection","")',
         font=F_WARN)
    _put(ws, 4, 1, '=IF(AND($F$2>0,$F$4>$F$3),"note: most of this sample is '
                   'brief quick-add data - rougher than it looks","")',
         font=F_WARN)

    comp = '="from "&$F$2&" prices: "&$F$3&" full, "&$F$4&" brief"'
    _put(ws, 5, 1, "median", font=F_BOLD)
    _arr(ws, "B5", f'=IFERROR(MEDIAN(IF({pooled},vpa_price)),"-")',
         font=F_BOLD, fmt=FMT_MONEY)
    _put(ws, 5, 3, comp, font=F_NOTE)
    _put(ws, 6, 1, "mode ($500 buckets)", font=F_BOLD)
    _arr(ws, "B6", f'=IFERROR(_xlfn.MODE.SNGL(IF({pooled},'
                   f'ROUND(vpa_price/500,0)*500)),"none (all unique)")',
         fmt=FMT_MONEY)
    _put(ws, 6, 3, comp, font=F_NOTE)
    _put(ws, 7, 1, "highest", font=F_BOLD)
    _arr(ws, "B7", f'=IF($F$2=0,"-",MAX(IF({pooled},vpa_price)))', fmt=FMT_MONEY)
    _put(ws, 7, 3, comp, font=F_NOTE)
    _put(ws, 8, 1, "lowest", font=F_BOLD)
    _arr(ws, "B8", f'=IF($F$2=0,"-",MIN(IF({pooled},vpa_price)))', fmt=FMT_MONEY)
    _put(ws, 8, 3, comp, font=F_NOTE)

    def block(hdr_row, slots, title, ref, fmt_txt):
        for j, h in enumerate([title, "median", "prices", "full", "brief",
                               "(helper)"], start=1):
            _put(ws, hdr_row, j, h, font=F_BOLD if j < 6 else F_NOTE,
                 fill=FILL_HDR if j < 6 else None)
        for k in range(slots):
            i = hdr_row + 1 + k
            if k == 0:
                nxt, guard = f"MIN(IF({m.cond()},vpa_date))", "FALSE"
            else:
                step = (f"EOMONTH($F{i - 1},0)+1" if fmt_txt == "YYYY-MM"
                        else f"$F{i - 1}")
                op = ">=" if fmt_txt == "YYYY-MM" else ">"
                nxt = f"MIN(IF({m.cond()}*(vpa_date{op}{step}),vpa_date))"
                guard = f'$F{i - 1}=""'
            _arr(ws, f"F{i}", f'=IF({guard},"",IF({nxt}=0,"",{nxt}))',
                 font=F_NOTE, fmt=FMT_DATE)
            ws[f"F{i}"].number_format = FMT_DATE
            _put(ws, i, 1, f'=IF($F{i}="","",TEXT($F{i},"{fmt_txt}"))')
            _arr(ws, f"B{i}", f'=IF($A{i}="","",IFERROR(MEDIAN(IF({pooled}'
                              f'*({ref}=$A{i}),vpa_price)),""))', fmt=FMT_MONEY)
            per = f',vpa_price,">0",{ref},$A{i}'
            _put(ws, i, 3, f'=IF($A{i}="","",{m.count(per)})')
            _put(ws, i, 4, f'=IF($A{i}="","",{m.count(per + ",vpa_detail,\"full\"")})')
            _put(ws, i, 5, f'=IF($A{i}="","",{m.count(per + ",vpa_detail,\"brief\"")})')
        return hdr_row + slots

    mon_hdr = 10
    mon_end = block(mon_hdr, len(months), "month", "vpa_month", "YYYY-MM")
    day_hdr = mon_end + 2
    day_end = day_hdr + len(days)
    # Say how many of the dataset's capture days this selection actually has.
    # Without the "x of y", a trim that was only swept once or twice looks
    # identical to lost data - which is exactly how it got read.
    _put(ws, day_hdr - 1, 1,
         f'="by capture day - this selection appears on "'
         f'&COUNTIF($A${day_hdr + 1}:$A${day_end},"?*")&" of {len(days)} '
         f'capture days in the dataset (narrow trims were swept fewer times '
         f'- that is not missing data)"', font=F_BOLD)
    written = block(day_hdr, len(days), "capture day", "vpa_day", "YYYY-MM-DD")
    if written != day_end:  # the header formula spans day_hdr+1..day_end
        raise RuntimeError(f"capture-day block ended at {written}, "
                           f"header counts to {day_end}")

    for title, col, row_a, row_b, anchor, styler, color, xt in (
            ("median by month", 2, mon_hdr, mon_end, "H3", _style_line, RED, "month"),
            ("median by capture day", 2, day_hdr, day_end, "H18", _style_line, BLUE,
             "capture day"),
            ("volume by month", 3, mon_hdr, mon_end, "Q3", _style_bar, GREEN, "month"),
            ("volume by capture day", 3, day_hdr, day_end, "Q18", _style_bar, SAND,
             "capture day")):
        ch = LineChart() if styler is _style_line else BarChart()
        ch.title = title
        ch.displayBlanksAs = "gap"
        ch.add_data(Reference(ws, min_col=col, min_row=row_a, max_row=row_b),
                    titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=row_a + 1, max_row=row_b))
        styler(ch, color, "median (AUD)" if col == 2 else "prices", xt)
        ws.add_chart(ch, anchor)

    _widths(ws, {"A": 22, "B": 16, "C": 30, "D": 8, "E": 12, "F": 12})


def _view_supply(ws, days, n_variants):
    m = Match()
    _put(ws, 1, 1, "Supply - how many are for sale", font=F_TITLE)
    _view_note(ws)
    _put(ws, 2, 1, "Each row counts the listings captured that day for the "
                   "selection: how many were on the market when you looked. "
                   "Capture the same search each time - a smaller number can "
                   "mean fewer cars OR a smaller capture.",
         font=F_NOTE, wrap=True)
    ws.row_dimensions[2].height = 26
    _put(ws, 3, 1, "selection", font=F_LABEL)
    _put(ws, 3, 2, f"={SEL}").font = F_LINK

    hdr_row, first = 6, 7
    end = hdr_row + len(days)
    B = f"$B${first}:$B${end}"
    for r, c, label, formula, fmt in (
            (4, 1, "captures", f"=COUNT($E${first}:$E${end})", None),
            (4, 3, "for sale now", f'=IF($B$4=0,"-",INDEX({B},$B$4))', None),
            (4, 5, "average", f'=IFERROR(AVERAGE({B}),"-")', "0.0"),
            (5, 1, "most", f'=IFERROR(MAX({B}),"-")', None),
            (5, 3, "fewest", f'=IFERROR(MIN({B}),"-")', None),
            (5, 5, "change vs last",
             f'=IF($B$4<2,"-",INDEX({B},$B$4)-INDEX({B},$B$4-1))', None)):
        _put(ws, r, c, label, font=F_NOTE)
        _put(ws, r, c + 1, formula, font=F_BOLD, fmt=fmt)

    for j, h in enumerate(["capture day", "for sale", "change", "median price",
                           "(helper)"], start=1):
        _put(ws, hdr_row, j, h, font=F_BOLD if j < 5 else F_NOTE,
             fill=FILL_HDR if j < 5 else None)
    for k in range(len(days)):
        i = first + k
        if k == 0:
            nxt, guard = f"MIN(IF({m.cond()},vpa_date))", "FALSE"
        else:
            nxt = f"MIN(IF({m.cond()}*(vpa_date>$E{i - 1}),vpa_date))"
            guard = f'$E{i - 1}=""'
        _arr(ws, f"E{i}", f'=IF({guard},"",IF({nxt}=0,"",{nxt}))', font=F_NOTE,
             fmt=FMT_DATE)
        ws[f"E{i}"].number_format = FMT_DATE
        _put(ws, i, 1, f'=IF($E{i}="","",TEXT($E{i},"YYYY-MM-DD"))')
        _put(ws, i, 2, f'=IF($A{i}="","",'
                       f'{m.count(",vpa_price,\">0\",vpa_day,$A" + str(i))})')
        _put(ws, i, 3, '="-"' if k == 0 else
             f'=IF(OR($A{i}="",$A{i - 1}=""),"-",$B{i}-$B{i - 1})')
        _arr(ws, f"D{i}", f'=IF($A{i}="","",IFERROR(MEDIAN(IF({m.cond()}'
                          f'*(vpa_day=$A{i})*(vpa_price>0),vpa_price)),""))',
             fmt=FMT_MONEY)

    bc = BarChart()
    bc.type = "col"
    bc.title = "how many for sale at each capture"
    bc.displayBlanksAs = "gap"
    bc.add_data(Reference(ws, min_col=2, min_row=hdr_row, max_row=end),
                titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=1, min_row=first, max_row=end))
    _style_bar(bc, GREEN, "listings", "capture day")
    ws.add_chart(bc, "G3")

    lc = LineChart()
    lc.title = "median price at each capture"
    lc.displayBlanksAs = "gap"
    lc.add_data(Reference(ws, min_col=4, min_row=hdr_row, max_row=end),
                titles_from_data=True)
    lc.set_categories(Reference(ws, min_col=1, min_row=first, max_row=end))
    _style_line(lc, RED, "median (AUD)", "capture day")
    ws.add_chart(lc, "G19")

    row0 = end + 2
    _put(ws, row0 - 1, 1, "For sale by variant, at each variant's own latest "
                          "capture (filter column A)", font=F_BOLD)
    for j, h in enumerate(["model_line", "variant", "latest capture",
                           "for sale", "median price"], start=1):
        _put(ws, row0, j, h, font=F_BOLD, fill=FILL_HDR)
    for k in range(n_variants):
        i = row0 + 1 + k
        _put(ws, i, 1, f'=IFERROR(INDEX(vpa_vp_line,{k + 1}),"")')
        _put(ws, i, 2, f'=IFERROR(INDEX(vpa_vp_var,{k + 1}),"")')
        pair = f'$A{i}&" "&$B{i}'
        _put(ws, i, 3, f'=IF($A{i}="","",IFERROR(TEXT(_xlfn.MAXIFS(vpa_date,'
                       f'vpa_key,{pair}),"YYYY-MM-DD"),""))')
        _put(ws, i, 4, f'=IF($A{i}="","",COUNTIFS(vpa_key,{pair},vpa_day,$C{i},'
                       f'vpa_price,">0"))')
        _arr(ws, f"E{i}", f'=IF($A{i}="","",IFERROR(MEDIAN(IF((vpa_key={pair})'
                          f'*(vpa_day=$C{i})*(vpa_price>0),vpa_price)),"-"))',
             fmt=FMT_MONEY)
    ws.auto_filter.ref = f"A{row0}:E{row0 + n_variants}"
    _widths(ws, {"A": 26, "B": 22, "C": 14, "D": 13, "E": 13})


#: Comparison stat rows: (row, label, kind). Kept as data so the labels, the
#: formulas and the chart ranges below can never drift apart.
_CMP_SLOTS = 6
_CMP_ROWS = [
    (5,  "prices (n)",            "hdr"),
    (6,  "full detail",           None),
    (7,  "price only (brief)",    None),
    (8,  "distinct capture days", None),
    (10, "median",                "hdr"),
    (11, "average",               None),
    (12, "25th percentile",       None),
    (13, "75th percentile",       None),
    (14, "lowest",                None),
    (15, "highest",               None),
    (16, "spread (high - low)",   None),
    (18, "median odometer",       "hdr"),
    (19, "$ per 10,000 km",       None),
    (20, "median year",           None),
    (22, "dealer listings",       "hdr"),
    (23, "private listings",      None),
    (25, "first capture",         "hdr"),
    (26, "latest capture",        None),
    (27, "median @ first",        None),
    (28, "median @ latest",       None),
    (29, "change first -> latest", None),
    (31, "rank by median",        "hdr"),
    (32, "vs cheapest selection", None),
]


def _view_comparison(ws, top, second, months):
    _put(ws, 1, 1, "Compare vehicles", font=F_TITLE)
    _view_note(ws)
    _put(ws, 2, 1, "Pick up to six selections in row 3 (dropdown, or type "
                   "one - the Models sheet lists every valid selection). A "
                   "model line like 'Ford Falcon' pools every trim; add a "
                   "trim to narrow it.", font=F_NOTE, wrap=True)

    _put(ws, 3, 1, "selection", font=F_BOLD)
    for r, lab, kind in _CMP_ROWS:
        _put(ws, r, 1, lab, font=F_BOLD if kind == "hdr" else F_BASE,
             fill=FILL_HDR if kind == "hdr" else None)

    prefill = [f"{top['make']} {top['model']}", f"{second['make']} {second['model']}"]
    dv = DataValidation(type="list", formula1="=vpa_sel_list", allow_blank=True)
    ws.add_data_validation(dv)

    for s in range(_CMP_SLOTS):
        col = get_column_letter(2 + s)
        _input_cell(ws, 3, 2 + s, prefill[s] if s < len(prefill) else "")
        dv.add(ws.cell(row=3, column=2 + s))
        m = Match(f"{col}$3")
        cond = f"{m.cond()}*(vpa_price>0)"
        n = f"{col}$5"
        # -- counts
        _put(ws, 5, 2 + s, f'={m.count(",vpa_price,\">0\"")}', font=F_BOLD)
        _put(ws, 6, 2 + s, f'={m.count(",vpa_price,\">0\",vpa_detail,\"full\"")}')
        _put(ws, 7, 2 + s, f'={m.count(",vpa_price,\">0\",vpa_detail,\"brief\"")}')
        # Distinct capture days: ask each KNOWN day whether this selection
        # appears on it. The 1/COUNTIF distinct-count idiom was tried first
        # and gave 25 instead of 3 - COUNTIFS does not broadcast a range
        # criteria the way SUMPRODUCT needs. Testing the day LIST is both
        # exact and easy to read.
        per_day = "+".join(f'COUNTIFS({c},{col}$3,vpa_day,vpa_day_list,'
                           f'vpa_price,">0")' for c in Match.COLS)
        _put(ws, 8, 2 + s, f'=IF({n}=0,0,SUMPRODUCT(--(({per_day})>0)))')
        # -- price shape
        _arr(ws, f"{col}10", f'=IFERROR(MEDIAN(IF({cond},vpa_price)),"-")',
             font=F_BOLD, fmt=FMT_MONEY)
        _arr(ws, f"{col}11", f'=IFERROR(AVERAGE(IF({cond},vpa_price)),"-")',
             fmt=FMT_MONEY)
        _arr(ws, f"{col}12", f'=IFERROR(QUARTILE(IF({cond},vpa_price),1),"-")',
             fmt=FMT_MONEY)
        _arr(ws, f"{col}13", f'=IFERROR(QUARTILE(IF({cond},vpa_price),3),"-")',
             fmt=FMT_MONEY)
        _arr(ws, f"{col}14", f'=IF({n}=0,"-",MIN(IF({cond},vpa_price)))',
             fmt=FMT_MONEY)
        _arr(ws, f"{col}15", f'=IF({n}=0,"-",MAX(IF({cond},vpa_price)))',
             fmt=FMT_MONEY)
        _put(ws, 16, 2 + s,
             f'=IF({n}=0,"-",{col}$15-{col}$14)', fmt=FMT_MONEY)
        # -- wear and age
        _arr(ws, f"{col}18", f'=IFERROR(MEDIAN(IF({cond}*(vpa_odo>0),vpa_odo)),"-")',
             fmt=FMT_KM)
        _put(ws, 19, 2 + s,
             f'=IF(OR({n}=0,NOT(ISNUMBER({col}$18)),{col}$18=0),"-",'
             f'{col}$10/({col}$18/10000))', fmt=FMT_MONEY)
        _arr(ws, f"{col}20", f'=IFERROR(MEDIAN(IF({cond}*(vpa_year>0),vpa_year)),"-")')
        # -- who is selling
        _put(ws, 22, 2 + s, f'={m.count(",vpa_price,\">0\",vpa_seller,\"dealer\"")}')
        _put(ws, 23, 2 + s, f'={m.count(",vpa_price,\">0\",vpa_seller,\"private\"")}')
        # -- movement over the captures
        _arr(ws, f"{col}25", f'=IF({n}=0,"",MIN(IF({cond},vpa_date)))', fmt=FMT_DATE)
        _arr(ws, f"{col}26", f'=IF({n}=0,"",MAX(IF({cond},vpa_date)))', fmt=FMT_DATE)
        _arr(ws, f"{col}27", f'=IFERROR(MEDIAN(IF({cond}*(vpa_date={col}$25),'
                             f'vpa_price)),"")', fmt=FMT_MONEY)
        _arr(ws, f"{col}28", f'=IFERROR(MEDIAN(IF({cond}*(vpa_date={col}$26),'
                             f'vpa_price)),"")', fmt=FMT_MONEY)
        _put(ws, 29, 2 + s,
             f'=IF(OR({col}$27="",{col}$28="",{col}$27=0,{col}$25={col}$26),"-",'
             f'({col}$28-{col}$27)/{col}$27)', fmt=FMT_PCT)
        # -- how it sits against the others on screen
        _put(ws, 31, 2 + s,
             f'=IF({n}=0,"-",RANK({col}$10,$B$10:$G$10,1))')
        _put(ws, 32, 2 + s,
             f'=IF(OR({n}=0,MIN($B$10:$G$10)=0),"-",'
             f'{col}$10/MIN($B$10:$G$10)-1)', fmt=FMT_PCT)

    # ---- median by month for every selection, and a chart over it
    hdr = 35
    _put(ws, hdr - 1, 1, "median by month - the block behind the chart",
         font=F_BOLD)
    _put(ws, hdr, 1, "month", font=F_BOLD, fill=FILL_HDR)
    for s in range(_CMP_SLOTS):
        _put(ws, hdr, 2 + s, f"=IF({get_column_letter(2 + s)}$3=\"\",\"(empty)\","
                             f"{get_column_letter(2 + s)}$3)",
             font=F_BOLD, fill=FILL_HDR)
    for i, mon in enumerate(months, start=hdr + 1):
        _put(ws, i, 1, mon)
        for s in range(_CMP_SLOTS):
            col = get_column_letter(2 + s)
            m = Match(f"{col}$3")
            _arr(ws, f"{col}{i}",
                 f'=IFERROR(MEDIAN(IF({m.cond()}*(vpa_price>0)*'
                 f'(vpa_month=$A{i}),vpa_price)),NA())', fmt=FMT_MONEY)
    end = hdr + len(months)

    ch = LineChart()
    ch.title = "median price by month - selections compared"
    ch.displayBlanksAs = "gap"
    ch.add_data(Reference(ws, min_col=2, max_col=1 + _CMP_SLOTS,
                          min_row=hdr, max_row=end), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=end))
    _style_line(ch, RED, "median (AUD)", "month")
    ch.height, ch.width = 9.5, 22
    ws.add_chart(ch, "I3")

    _put(ws, 30, 1, "if 'prices (n)' changed a lot between those two captures "
                    "the medians describe different stock, not a price move",
                    font=F_NOTE, wrap=True)
    # Turnover is deliberately NOT a row here. It is the one block of the
    # app's Compare page a formula cannot reproduce: it has to recognise the
    # same listing across capture days, split the days wherever that
    # recognition fails, and rate exits per car-day - set operations over a
    # selection the reader picks in the dropdown AFTER this file was built.
    # A rate computed from two capture counts would be wrong precisely where
    # it looks most interesting, so the sheet points at the app instead of
    # printing a number it cannot stand behind. Same reasoning as the
    # de-duplication note below.
    _put(ws, 33, 1, "change compares the median on the first capture day vs the "
                    "latest; '-' = only one capture day so far. Empty columns "
                    "are ignored by the chart. How fast each selection CLEARS "
                    "- what left view, what arrived, and how long a car sits "
                    "- is on the app's Compare and Explore pages: it needs the "
                    "same listing recognised across days, which a worksheet "
                    "formula cannot do.", font=F_NOTE)
    _put(ws, hdr - 2, 1, "This block counts every sighting, so a car listed "
                         "twice in a month counts twice - a formula cannot "
                         "de-duplicate. The app's Compare page counts each car "
                         "once per month, so the two can differ slightly here "
                         "(and only here).", font=F_NOTE, wrap=True)
    ws.row_dimensions[hdr - 2].height = 26
    _widths(ws, dict({"A": 24}, **{get_column_letter(2 + s): 19
                                   for s in range(_CMP_SLOTS)}))


def _view_richstats(ws, last: int, n_variants: int):
    m = Match()
    _put(ws, 1, 1, f'="Rich stats (full rows only) for: "&{SEL}', font=F_LINK)
    _view_note(ws)
    _put(ws, 2, 1, f'=IF({m.count(",vpa_detail,\"full\",vpa_price,\">0\"")}=0,'
                   f'"no full listings yet for this selection","")', font=F_WARN)

    _put(ws, 4, 1, "seller split", font=F_BOLD, fill=FILL_HDR)
    _put(ws, 4, 2, "n", font=F_BOLD, fill=FILL_HDR)
    _put(ws, 4, 3, "median", font=F_BOLD, fill=FILL_HDR)
    for i, seller in ((5, "dealer"), (6, "private")):
        _put(ws, i, 1, seller)
        _put(ws, i, 2, f'={m.count(f",vpa_detail,\"full\",vpa_seller,\"{seller}\","
                        f"vpa_price,\">0\"")}')
        _arr(ws, f"C{i}", f'=IFERROR(MEDIAN(IF({m.cond()}*(vpa_detail="full")'
                          f'*(vpa_seller="{seller}")*(vpa_price>0),vpa_price)),"-")',
             fmt=FMT_MONEY)

    _put(ws, 8, 1, "mileage fit (ln price vs km, full rows)", font=F_BOLD,
         fill=FILL_HDR)
    _put(ws, 9, 1, "rows with price + odometer", font=F_NOTE)
    _put(ws, 9, 2, f'={m.count(",vpa_detail,\"full\",vpa_price,\">0\",vpa_odo,\">0\"")}')
    cond = f'{m.cond()}*(vpa_detail="full")*(vpa_price>0)*(vpa_odo>0)'
    _put(ws, 10, 1, "slope (ln$/km)", font=F_NOTE)
    _arr(ws, "B10", f'=IF($B$9<20,"not enough data (need 20 with odometer)",'
                    f'SLOPE(IF({cond},vpa_lnprice),IF({cond},vpa_odo)))')
    _put(ws, 11, 1, "price effect per 10,000 km", font=F_NOTE)
    _put(ws, 11, 2, '=IF(ISNUMBER($B$10),EXP($B$10*10000)-1,"-")', fmt=FMT_PCT)
    _put(ws, 12, 1, "median odometer", font=F_NOTE)
    _arr(ws, "B12", f'=IFERROR(MEDIAN(IF({m.cond()}*(vpa_detail="full")'
                    f'*(vpa_odo>0),vpa_odo)),"-")', fmt=FMT_KM)
    _put(ws, 13, 1, "least-squares here so it stays a formula; the CLI report "
                    "uses a robust (Huber) fit - small differences expected",
         font=F_NOTE)

    row0 = 16
    _put(ws, row0 - 1, 1, "variant breakdown, full rows (filter column A)",
         font=F_BOLD)
    for j, h in enumerate(["model_line", "variant", "n", "median", "lowest",
                           "highest"], start=1):
        _put(ws, row0, j, h, font=F_BOLD, fill=FILL_HDR)
    for k in range(n_variants):
        i = row0 + 1 + k
        _put(ws, i, 1, f'=IFERROR(INDEX(vpa_vp_line,{k + 1}),"")')
        _put(ws, i, 2, f'=IFERROR(INDEX(vpa_vp_var,{k + 1}),"")')
        pair = f'$A{i}&" "&$B{i}'
        _put(ws, i, 3, f'=IF($A{i}="","",COUNTIFS(vpa_key,{pair},vpa_detail,'
                       f'"full",vpa_price,">0"))')
        _arr(ws, f"D{i}", f'=IF($A{i}="","",IFERROR(MEDIAN(IF((vpa_key={pair})'
                          f'*(vpa_detail="full")*(vpa_price>0),vpa_price)),"-"))',
             fmt=FMT_MONEY)
        _put(ws, i, 5, f'=IF($C{i}=0,"-",_xlfn.MINIFS(vpa_price,vpa_key,{pair},'
                       f'vpa_detail,"full",vpa_price,">0"))', fmt=FMT_MONEY)
        _put(ws, i, 6, f'=IF($C{i}=0,"-",_xlfn.MAXIFS(vpa_price,vpa_key,{pair},'
                       f'vpa_detail,"full"))', fmt=FMT_MONEY)
    ws.auto_filter.ref = f"A{row0}:F{row0 + n_variants}"

    sc = ScatterChart()
    sc.title = "price vs odometer (full rows, current selection)"
    _show_axes(sc, "odometer (km)", "price (AUD)", y_money=True)
    chart_ws = ws.parent["_Chart"]
    xref = Reference(chart_ws, min_col=1, min_row=3, max_row=last)
    yref = Reference(chart_ws, min_col=2, min_row=3, max_row=last)
    ser = Series(yref, xref, title="listings")
    ser.marker = Marker(symbol="circle", size=5)
    ser.graphicalProperties.line.noFill = True
    sc.series.append(ser)
    sc.legend = None
    sc.height, sc.width = 9, 15
    ws.add_chart(sc, "H3")

    sb = BarChart()
    sb.title = "dealer vs private median"
    sb.add_data(Reference(ws, min_col=3, min_row=4, max_row=6), titles_from_data=True)
    sb.set_categories(Reference(ws, min_col=1, min_row=5, max_row=6))
    _style_bar(sb, BLUE, "median (AUD)", "seller", y_money=True)
    sb.height, sb.width = 7, 9
    ws.add_chart(sb, "H22")
    _widths(ws, {"A": 26, "B": 20, "C": 6, "D": 12, "E": 12, "F": 12})


def _view_forecasts(ws, n_months: int):
    _put(ws, 1, 1, "Forecast for the current selection", font=F_TITLE)
    _view_note(ws)
    _put(ws, 2, 1, "Values come from the forecast ladder on the _Forecasts "
                   "sheet (insufficient -> robust trend -> ETS). Trims with "
                   "fewer than 8 prices get no row - use their model line.",
         font=F_NOTE, wrap=True)
    _put(ws, 3, 1, "selection", font=F_LABEL)
    _put(ws, 3, 2, f"={SEL}").font = F_LINK
    _put(ws, 3, 4, "(row in _Forecasts)", font=F_NOTE)
    _put(ws, 3, 5, f'=IFERROR(MATCH({SEL},vpa_fc_sel,0),"")', font=F_NOTE)

    def idx(col):
        return f'IF($E$3="","-",INDEX(vpa_fc_all,$E$3,{col}))'

    for r, (label, col, fmt) in enumerate((
            ("method", 2, None), ("n", 3, None), ("buckets", 4, None),
            ("median now", 5, FMT_MONEY), ("direction", 6, None)), start=4):
        _put(ws, r, 1, label, font=F_NOTE)
        _put(ws, r, 2, f"={idx(col)}", font=F_BOLD, fmt=fmt)
    _put(ws, 9, 1, "verdict", font=F_NOTE)
    _put(ws, 9, 2, f'={idx(19)}', wrap=True)

    _put(ws, 11, 1, "horizon", font=F_BOLD, fill=FILL_HDR)
    for j, h in enumerate(["month", "forecast", "low (80%)", "high (80%)",
                           "interval +-"], start=2):
        _put(ws, 11, j, h, font=F_BOLD, fill=FILL_HDR)
    for h in range(3):
        r = 12 + h
        base = 7 + h * 4
        _put(ws, r, 1, f"m+{h + 1}", font=F_NOTE)
        _put(ws, r, 2, f"={idx(base)}")
        for k in range(3):
            _put(ws, r, 3 + k, f"={idx(base + 1 + k)}", fmt=FMT_MONEY)
        _put(ws, r, 6, f'=IF(ISNUMBER($C{r}),($E{r}-$D{r})/2/$C{r},"-")',
             fmt=FMT_PCT)

    # chart feed: monthly history from Trend, then the three forecast points
    _put(ws, 16, 1, "(chart feed)", font=F_NOTE)
    for j, h in enumerate(["history", "forecast", "low", "high"], start=2):
        _put(ws, 16, j, h, font=F_NOTE)
    for k in range(n_months):
        r = 17 + k
        tr = 11 + k                                   # Trend monthly rows
        _put(ws, r, 1, f'=IF(Trend!$A{tr}="","",Trend!$A{tr})', font=F_NOTE)
        _put(ws, r, 2, f'=IF(Trend!$B{tr}="","",Trend!$B{tr})', font=F_NOTE,
             fmt=FMT_MONEY)
    for h in range(3):
        r = 17 + n_months + h
        _put(ws, r, 1, f'=IF($B{12 + h}="-","",$B{12 + h})', font=F_NOTE)
        for k, src in enumerate(("C", "D", "E")):
            _put(ws, r, 3 + k, f'=IF(ISNUMBER(${src}{12 + h}),${src}{12 + h},"")',
                 font=F_NOTE, fmt=FMT_MONEY)
    feed_end = 17 + n_months + 2

    ch = LineChart()
    ch.title = "history + 3-month forecast (80% interval)"
    ch.displayBlanksAs = "gap"
    for col in (2, 3, 4, 5):
        ch.add_data(Reference(ws, min_col=col, min_row=16, max_row=feed_end),
                    titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=17, max_row=feed_end))
    _show_axes(ch, None, "median (AUD)", y_money=True)
    for s, (color, dash, size) in zip(ch.series, ((RED, None, 6), (BLUE, "dash", 6),
                                                  ("999999", "sysDash", 3),
                                                  ("999999", "sysDash", 3))):
        s.graphicalProperties.line = LineProperties(solidFill=color, w=20000,
                                                    prstDash=dash)
        s.marker = Marker(symbol="circle", size=size)
        s.marker.graphicalProperties.solidFill = color
        s.smooth = False
    ch.y_axis.majorGridlines = None
    ch.height, ch.width = 8.5, 16
    ws.add_chart(ch, "H3")
    _widths(ws, {"A": 14, "B": 26, "C": 14, "D": 14, "E": 14, "F": 12})


def _view_models(ws, n_selections: int):
    _put(ws, 1, 1, "Every selection you can pick", font=F_TITLE)
    _view_note(ws)
    _put(ws, 2, 1, "Three levels: a make ('Ford') pools every model, a model "
                   "line ('Ford Falcon') pools every trim, a trim row is that "
                   "trim only. Any of them can be typed into Comparison or "
                   "picked here. Near-duplicates mean the same car was entered "
                   "with different text - fix it at entry, not here.",
         font=F_NOTE, wrap=True)
    ws.row_dimensions[2].height = 26
    for j, h in enumerate(["selection", "prices (n)", "full", "brief",
                           "last capture", "median"], start=1):
        _put(ws, 3, j, h, font=F_BOLD, fill=FILL_HDR)
    for k in range(n_selections):
        i = 4 + k
        _put(ws, i, 1, f'=IFERROR(INDEX(vpa_sel_list,{k + 1}),"")')
        m = Match(f"$A{i}")
        _put(ws, i, 2, f'=IF($A{i}="","",{m.count(",vpa_price,\">0\"")})')
        _put(ws, i, 3, f'=IF($A{i}="","",'
                       f'{m.count(",vpa_price,\">0\",vpa_detail,\"full\"")})')
        _put(ws, i, 4, f'=IF($A{i}="","",'
                       f'{m.count(",vpa_price,\">0\",vpa_detail,\"brief\"")})')
        _put(ws, i, 5, f'=IF($A{i}="","",TEXT(MAX(_xlfn.MAXIFS(vpa_date,vpa_line,'
                       f'$A{i}),_xlfn.MAXIFS(vpa_date,vpa_key,$A{i}),'
                       f'_xlfn.MAXIFS(vpa_date,vpa_make,$A{i})),"YYYY-MM-DD"))')
        _arr(ws, f"F{i}", f'=IF($A{i}="","",IFERROR(MEDIAN(IF({m.cond()}'
                          f'*(vpa_price>0),vpa_price)),"-"))', fmt=FMT_MONEY)
    ws.auto_filter.ref = f"A3:F{3 + n_selections}"
    ws.freeze_panes = "A4"
    _widths(ws, {"A": 36, "B": 10, "C": 8, "D": 8, "E": 13, "F": 14})
