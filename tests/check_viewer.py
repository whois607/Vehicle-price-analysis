"""Verification of the Excel viewer against real Excel (COM automation).

    python -m tests.check_viewer     (from vehicle-price-analysis/)

Seeds a throwaway store with a mixed model (24 full carsales rows with
odometer/year/variant/state/seller + 6 brief quick-add rows) and a
brief-only model, generates viewer.xlsx, then opens it in actual Excel to
read what the formulas COMPUTE (not what Python intended): pooled trend
stats + composition, monthly medians, full-only listings with a live
seller filter, the mileage SLOPE vs numpy OLS, dealer/private medians,
brief-only empty states, Quick Add pool equivalence, pasted forecasts vs
forecast.py, fresh-commit-then-regenerate, and a zero-formula-error scan
of every sheet. ASCII output; exit 0 = all pass.
"""
from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

TODAY = date.today().isoformat()

import numpy as np
import pandas as pd

from vpa import analyze, forecast, quickadd
from vpa.schema import make_record
from vpa.segments import add_segment_cols
from vpa.store import Store
from vpa.viewer_xlsx import LAYOUT_VERSION, generate

ROOT = Path(__file__).resolve().parent
DB = ROOT / "synthetic" / "viewer_test.db"
XLSX = ROOT / "synthetic" / "viewer_test.xlsx"
XLSX2 = ROOT / "synthetic" / "viewer_test2.xlsx"

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def approx(a, b, rel=1e-6, abs_tol=0.51) -> bool:
    if a is None or b is None:
        return False
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(abs_tol, rel * abs(b))


# ------------------------------------------------------------------ seeding
def _cleanup_files():
    """Kill hidden (windowless) Excel zombies from crashed prior runs, then
    remove old outputs. Never touches Excel windows the user has open."""
    import time
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process EXCEL -ErrorAction SilentlyContinue | "
                    "Where-Object {$_.MainWindowTitle -eq ''} | "
                    "Stop-Process -Force"], capture_output=True)
    for p in (XLSX, XLSX2):
        for attempt in range(6):                     # handle release lags the kill
            try:
                p.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(1)


def seed() -> dict:
    _cleanup_files()
    if DB.exists():
        DB.unlink()
    full = []
    months = ["2026-05", "2026-06", "2026-07"]
    j = 0
    for m in months:
        for k in range(8):
            odo = 40_000 + j * 6_100.0
            price = round(30_000 * math.exp(-3e-6 * (odo - 100_000)) + (j % 5) * 137 - 274)
            # "XR" vs "XR Turbo": one trim is a word-prefix of the other -
            # the regression shape for the selection-matching collision bug
            full.append(make_record({
                "listing_id": f"VF-{j}", "make": "Vtest", "model": "Vmodel",
                "variant": "XR" if j % 3 else "XR Turbo", "year": 2014 + (j % 2),
                "price": price, "odometer": odo,
                "state": "NSW" if j % 2 else "VIC",
                "seller_type": "dealer" if j % 4 == 0 else "private",
                "capture_date": f"{m}-{3 + k * 3:02d}"}, source="carsales"))
            j += 1
    # Supply fixture - 3 listings captured on each of 3 days, so the counts
    # and per-day medians are hand-checkable:
    #   2026-05-03: M1 M2 M3 -> median $21,000
    #   2026-06-03: M2 M3 M4 -> median $21,500
    #   2026-07-03: M3 M5 M6 -> median $22,500
    mover = []
    for day, ids in (("2026-05-03", ["M1", "M2", "M3"]),
                     ("2026-06-03", ["M2", "M3", "M4"]),
                     ("2026-07-03", ["M3", "M5", "M6"])):
        for lid in ids:
            mover.append(make_record({
                "listing_id": f"MV-{lid}", "make": "Mtest", "model": "Mmodel",
                "year": 2015, "price": 20000 + int(lid[1:]) * 500,
                "odometer": 100_000, "state": "NSW", "seller_type": "private",
                "capture_date": day}, source="carsales"))
    with Store(DB) as store:
        store.ingest(full, source_file="(seed-full)", adapter="seed")
        store.ingest(mover, source_file="(seed-supply)", adapter="seed")
    r1 = quickadd.commit_payload({"make": "Vtest", "model": "Vmodel",
                                  "prices": ["27,500", "$27,500", "27600"],
                                  "capture_date": "2026-06-15"}, DB)
    r2 = quickadd.commit_payload({"make": "Vtest", "model": "Vmodel",
                                  "prices": ["24990", "31000", "26500"]}, DB)
    # buckets 5000/6000/7500/8500 - genuinely all unique (5900 would join 6100
    # in the 6000 bucket and create a real mode; Excel caught that mistake)
    r3 = quickadd.commit_payload({"make": "Bonly", "model": "Bmodel",
                                  "prices": ["5000", "6100", "7300", "8600"]}, DB)
    assert r1["committed"] == 3 and r2["committed"] == 3 and r3["committed"] == 4
    return {"full_prices": [r.price_aud for r in full],
            "full_odo": [r.odometer_km for r in full]}


# ------------------------------------------------------- Excel COM reader
PS_READER = r"""
$ErrorActionPreference = "Stop"
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false; $xl.DisplayAlerts = $false
try {
$wb = $xl.Workbooks.Open("__XLSX__")
Start-Sleep -Seconds 1
function Recalc { $xl.CalculateFullRebuild(); Start-Sleep -Milliseconds 600 }
$pk = $wb.Sheets("Picker"); $trend = $wb.Sheets("Trend")
$list = $wb.Sheets("_Listings"); $rich = $wb.Sheets("RichStats")
$fcv = $wb.Sheets("Forecasts"); $fcd = $wb.Sheets("_Forecasts")
$sp = $wb.Sheets("Supply"); $cmp = $wb.Sheets("Comparison")

$out = @{}
function Pick($b, $m, $t) {
  $pk.Range("B4").Value2 = $b; $pk.Range("B5").Value2 = $m
  $pk.Range("D4").Value2 = $t; Recalc
}
# --- Trend: monthly block header row 10 (data 11+), day block header 60 ---
Pick "Vtest" "Vmodel" ""
$out.resolved = $pk.Range("B6").Value2
$out.v_n = $trend.Range("F2").Value2; $out.v_full = $trend.Range("F3").Value2
$out.v_brief = $trend.Range("F4").Value2
$out.v_median = $trend.Range("B5").Value2; $out.v_mode = $trend.Range("B6").Value2
$out.v_hi = $trend.Range("B7").Value2; $out.v_lo = $trend.Range("B8").Value2
$out.v_comp = $trend.Range("C5").Value2
$out.v_warn = $trend.Range("A3").Value2
# blocks are sized to the data, so find the day header instead of assuming
$dayHdr = 0
for ($r = 11; $r -le 400; $r++) {
  if ($trend.Cells($r,1).Value2 -eq "capture day") { $dayHdr = $r; break }
}
$out.day_hdr_row = $dayHdr
$out.v_months = @(11..($dayHdr - 2) | ForEach-Object {
  @{m=$trend.Range("A$_").Value2; med=$trend.Range("B$_").Value2;
    n=$trend.Range("C$_").Value2; full=$trend.Range("D$_").Value2;
    brief=$trend.Range("E$_").Value2}})
$out.day_hdr = $trend.Cells($dayHdr,1).Value2
$out.day1 = @{d=$trend.Cells($dayHdr+1,1).Value2; med=$trend.Cells($dayHdr+1,2).Value2;
              n=$trend.Cells($dayHdr+1,3).Value2}

Pick "Vtest" "Vmodel" "XR"
$out.xr_n = $trend.Range("F2").Value2; $out.xr_full = $trend.Range("F3").Value2
$out.xr_median = $trend.Range("B5").Value2
Pick "Vtest" "" ""
$out.brand_n = $trend.Range("F2").Value2
Pick "Vtest" "Vmodel" ""

# --- RichStats ---
$out.rich_dealer_n = $rich.Range("B5").Value2; $out.rich_dealer_med = $rich.Range("C5").Value2
$out.rich_private_n = $rich.Range("B6").Value2; $out.rich_private_med = $rich.Range("C6").Value2
$out.rich_nodo = $rich.Range("B9").Value2; $out.rich_slope = $rich.Range("B10").Value2
$out.rich_pct10k = $rich.Range("B11").Value2

# --- _Listings backend: strip row 2, headers row 4, data row 5 ---
$out.list_count = $list.Range("B2").Value2; $out.list_median = $list.Range("D2").Value2
$out.list_lo = $list.Range("F2").Value2; $out.list_hi = $list.Range("H2").Value2
$out.list_empty = $list.Range("A3").Value2
$list.Range("A4:J" + (4 + __NFULL__)).AutoFilter(8, "dealer") | Out-Null
Recalc
$out.filt_count = $list.Range("B2").Value2; $out.filt_median = $list.Range("D2").Value2
$list.AutoFilter.ShowAllData()

# --- Comparison: selections in row 3; see _CMP_ROWS in viewer_xlsx.py for
# the row map (n=5, days=8, median=10, avg=11, q25/q75=12/13, lo/hi=14/15,
# spread=16, odo=18, $/10k=19, year=20, dealer/private=22/23, change=29,
# rank=31, vs cheapest=32) ---
$cmp.Range("B3").Value2 = "Vtest Vmodel"
$cmp.Range("C3").Value2 = "Bonly Bmodel"
Recalc
$out.cmp = @{b_n=$cmp.Range("B5").Value2; b_avg=$cmp.Range("B11").Value2;
             b_med=$cmp.Range("B10").Value2; b_lo=$cmp.Range("B14").Value2;
             b_hi=$cmp.Range("B15").Value2; b_chg=$cmp.Range("B29").Value2;
             b_days=$cmp.Range("B8").Value2; b_q25=$cmp.Range("B12").Value2;
             b_q75=$cmp.Range("B13").Value2; b_spread=$cmp.Range("B16").Value2;
             b_rank=$cmp.Range("B31").Value2;
             c_n=$cmp.Range("C5").Value2; c_med=$cmp.Range("C10").Value2;
             c_chg=$cmp.Range("C29").Value2; c_rank=$cmp.Range("C31").Value2;
             months=$cmp.Range("B36").Value2; charts=$cmp.ChartObjects().Count}

# --- brief-only selection ---
Pick "Bonly" "Bmodel" ""
$out.b_n = $trend.Range("F2").Value2; $out.b_full = $trend.Range("F3").Value2
$out.b_median = $trend.Range("B5").Value2; $out.b_mode = $trend.Range("B6").Value2
$out.b_warn = $trend.Range("A4").Value2
$out.b_rich_empty = $rich.Range("A2").Value2
$out.b_rich_slope = $rich.Range("B10").Value2
$out.b_list_count = $list.Range("B2").Value2
$out.b_day1 = $trend.Cells($dayHdr+1,1).Value2
$out.b_day2 = $trend.Cells($dayHdr+2,1).Value2

# --- Forecasts: view panel + the backend table it reads ---
Pick "Vtest" "Vmodel" ""
$out.panel_method = $fcv.Range("B4").Value2
$out.panel_m1 = @{month=$fcv.Range("B12").Value2; fc=$fcv.Range("C12").Value2;
                  lo=$fcv.Range("D12").Value2; hi=$fcv.Range("E12").Value2}
$out.fc = @()
for ($r = 3; $r -le 60; $r++) {
  if ($fcd.Cells($r,1).Value2) {
    $out.fc += @{line=$fcd.Cells($r,1).Value2; method=$fcd.Cells($r,2).Value2;
                 n=$fcd.Cells($r,3).Value2; level=$fcd.Cells($r,5).Value2;
                 m1=$fcd.Cells($r,8).Value2; m1lo=$fcd.Cells($r,9).Value2;
                 m1hi=$fcd.Cells($r,10).Value2}
  }
}

# --- Supply: header row 6, data 7+ ---
Pick "Mtest" "Mmodel" ""
$out.sp_panel = @{captures=$sp.Range("B4").Value2; now=$sp.Range("D4").Value2;
                  avg=$sp.Range("F4").Value2; most=$sp.Range("B5").Value2;
                  fewest=$sp.Range("D5").Value2; change=$sp.Range("F5").Value2}
$out.sp_rows = @()
for ($r = 7; $r -le 9; $r++) {
  $out.sp_rows += @{day=$sp.Cells($r,1).Value2; forsale=$sp.Cells($r,2).Value2;
                    change=$sp.Cells($r,3).Value2; median=$sp.Cells($r,4).Value2}
}
$out.sp_row4_day = $sp.Cells(10,1).Value2
$out.sheets = @($wb.Sheets | ForEach-Object { $_.Name })
Pick "Vtest" "Vmodel" ""

# Formula-error scan (xlCellTypeFormulas=-4123, xlErrors=16). #N/A is
# separated from the rest: NA() is deliberate - it makes a line chart show a
# gap instead of dropping to zero - whereas #REF!/#VALUE!/#DIV/0!/#NAME? are
# always bugs, on every sheet including the chart feeders.
$out.errors = @{}
$out.hard_errors = @{}
foreach ($ws in $wb.Sheets) {
  $cnt = 0; $hard = 0
  try {
    $cells = $ws.UsedRange.SpecialCells(-4123, 16)
    $cnt = $cells.Count
    foreach ($c in $cells) { if ([string]$c.Text -ne '#N/A') { $hard++ } }
  } catch { $cnt = 0; $hard = 0 }
  $out.errors[$ws.Name] = $cnt
  $out.hard_errors[$ws.Name] = $hard
}
$wb.Close($false)
$out | ConvertTo-Json -Depth 6
} finally {
  $xl.Quit()
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($xl) | Out-Null
}
"""


def read_via_excel(xlsx: Path, n_full: int, attempts: int = 3) -> dict:
    """COM is transiently flaky (RPC_E_CALL_REJECTED while Excel warms up) -
    retry the whole session a couple of times."""
    import time
    script = (PS_READER.replace("__XLSX__", str(xlsx))
              .replace("__NFULL__", str(n_full)))
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                     encoding="utf-8-sig") as f:
        f.write(script)
        path = f.name
    try:
        last_err = ""
        for attempt in range(attempts):
            proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy",
                                   "Bypass", "-File", path],
                                  capture_output=True, text=True, timeout=240)
            if proc.returncode == 0:
                return json.loads(proc.stdout)
            last_err = proc.stderr[:2000]
            time.sleep(3)
        raise RuntimeError(f"Excel COM reader failed after {attempts} attempts:\n{last_err}")
    finally:
        Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------- checks
def main() -> int:
    print("== check_viewer: seeding mixed full+brief store ==")
    seeded = seed()
    print("== generating workbook ==")
    generate(DB, XLSX)
    print(f"   {XLSX}")

    # expected values, computed from the same code Quick Add uses
    with Store(DB) as store:
        df = add_segment_cols(store.frame())
    v = df[df["model_line"] == "Vtest Vmodel"]
    pool = v["price_aud"].dropna().tolist()
    exp = quickadd.price_stats(pool)
    qa = quickadd.stats_payload({"make": "Vtest", "model": "Vmodel", "prices": []}, DB)
    full_rows = v[v["detail_level"] == "full"]
    dealer = full_rows[full_rows["seller_type"] == "dealer"]["price_aud"]
    private = full_rows[full_rows["seller_type"] == "private"]["price_aud"]
    ols_slope = float(np.polyfit(seeded["full_odo"],
                                 np.log(seeded["full_prices"]), 1)[0])
    n_full_total = int((df["detail_level"] == "full").sum())

    # view blocks are fixed-size slots now, so the geometry is constant no
    # matter how the data grows - nothing to derive
    print("\n== reading computed values from real Excel ==")
    got = read_via_excel(XLSX, n_full_total)

    print("\n== trend lens: pooled stats + composition ==")
    check("cascading picker resolves brand+model to the selection key",
          got["resolved"] == "Vtest Vmodel", f"resolved='{got['resolved']}'")
    check("pool composition: 30 prices = 24 full + 6 brief",
          got["v_n"] == 30 and got["v_full"] == 24 and got["v_brief"] == 6,
          f"n={got['v_n']}, full={got['v_full']}, brief={got['v_brief']}")
    check("median/highest/lowest match Python over the same pool",
          approx(got["v_median"], exp["median"]) and approx(got["v_hi"], exp["highest"])
          and approx(got["v_lo"], exp["lowest"]),
          f"Excel median={got['v_median']}, hi={got['v_hi']}, lo={got['v_lo']} "
          f"vs Python {exp['median']}/{exp['highest']}/{exp['lowest']}")
    # Excel's MODE.SNGL breaks ties by data order; the app picks the lowest
    # tied bucket - so on ties, accept any top-count bucket.
    buckets: dict[float, int] = {}
    for p in pool:
        b = round(p / 500) * 500
        buckets[b] = buckets.get(b, 0) + 1
    top = max(buckets.values())
    top_set = {b for b, c in buckets.items() if c == top}
    check("mode on $500 buckets matches (tie-tolerant)",
          exp["mode"] is not None and top >= 2
          and any(approx(got["v_mode"], b) for b in top_set),
          f"Excel mode={got['v_mode']}; top buckets (x{top}): {sorted(top_set)}; "
          f"app rule picks {exp['mode']}")
    check("composition string on the figure",
          got["v_comp"] == "from 30 prices: 24 full, 6 brief",
          f"'{got['v_comp']}'")
    mon_ok, mon_msgs = True, []
    for mrow in [r for r in got["v_months"] if r["m"]]:
        mp = v[(v["capture_date"].dt.strftime("%Y-%m") == mrow["m"])]["price_aud"].dropna()
        want = float(statistics.median(mp)) if len(mp) else None
        ok = (approx(mrow["med"], want) if want is not None else mrow["med"] in ("", None)) \
            and mrow["n"] == len(mp)
        mon_ok &= ok
        mon_msgs.append(f"{mrow['m']}: {mrow['med']} (n={mrow['n']}, "
                        f"{mrow['full']}f/{mrow['brief']}b) want {want} (n={len(mp)})")
    check("monthly medians + volumes + composition", mon_ok, "; ".join(mon_msgs))
    first_day = v["capture_date"].min().strftime("%Y-%m-%d")
    day_pool = v[v["capture_date"].dt.strftime("%Y-%m-%d") == first_day]["price_aud"].dropna()
    check("per-capture-day table present with correct first day",
          got["day_hdr"] == "capture day" and got["day1"]["d"] == first_day
          and got["day1"]["n"] == len(day_pool)
          and approx(got["day1"]["med"], float(statistics.median(day_pool))),
          f"hdr='{got['day_hdr']}', day1={got['day1']} vs {first_day} "
          f"n={len(day_pool)} med={float(statistics.median(day_pool))}")
    xr = v[(v["variant"] == "XR") & v["price_aud"].notna()]["price_aud"]
    check("trim selection matches EXACTLY - 'XR' must not swallow its "
          "word-prefix sibling 'XR Turbo' (nor brief rows)",
          got["xr_n"] == len(xr) and got["xr_full"] == len(xr)
          and approx(got["xr_median"], float(statistics.median(xr))),
          f"XR: n={got['xr_n']}, full={got['xr_full']}, median={got['xr_median']} "
          f"vs Python exact-trim n={len(xr)} median={float(statistics.median(xr))}")
    check("brand-only selection pools the whole make",
          got["brand_n"] == 30, f"brand 'Vtest' n={got['brand_n']} (want 30)")
    check("viewer trend agrees with Quick Add's history pool",
          approx(got["v_median"], qa["now"]["median"])
          and got["v_n"] == qa["now"]["n"],
          f"viewer median={got['v_median']} n={got['v_n']} vs quickadd "
          f"{qa['now']['median']} n={qa['now']['n']}")

    print("\n== detailed lens: full rows only, live filters ==")
    check("listings count = full rows only (brief absent)",
          got["list_count"] == n_full_total and got["list_empty"] in ("", None),
          f"count={got['list_count']} (full rows in store: {n_full_total})")
    # the Listings sheet is GLOBAL (every full row), unlike the selection-
    # scoped RichStats below - compare it against the whole full-row set
    all_full = df[df["detail_level"] == "full"]
    all_dealer = all_full[all_full["seller_type"] == "dealer"]["price_aud"].dropna()
    check("listings median over full rows",
          approx(got["list_median"], float(all_full["price_aud"].median())),
          f"{got['list_median']} vs {float(all_full['price_aud'].median())}")
    check("seller filter drives the live stats (SUBTOTAL/AGGREGATE)",
          got["filt_count"] == len(all_dealer)
          and approx(got["filt_median"], float(all_dealer.median())),
          f"filtered count={got['filt_count']} median={got['filt_median']} vs "
          f"dealer-only {len(all_dealer)} / {float(all_dealer.median())}")
    check("dealer vs private medians (full rows, by marker)",
          got["rich_dealer_n"] == len(dealer) and got["rich_private_n"] == len(private)
          and approx(got["rich_dealer_med"], float(dealer.median()))
          and approx(got["rich_private_med"], float(private.median())),
          f"dealer n={got['rich_dealer_n']} med={got['rich_dealer_med']}; "
          f"private n={got['rich_private_n']} med={got['rich_private_med']}")
    check("mileage fit uses only full rows and matches numpy OLS",
          got["rich_nodo"] == 24 and approx(got["rich_slope"], ols_slope, rel=1e-6,
                                            abs_tol=1e-9),
          f"Excel slope={got['rich_slope']} vs numpy {ols_slope} (n={got['rich_nodo']})")
    check("price effect per 10k km consistent with slope",
          approx(got["rich_pct10k"], math.exp(ols_slope * 10_000) - 1, rel=1e-5,
                 abs_tol=1e-6),
          f"{got['rich_pct10k']} vs {math.exp(ols_slope * 10_000) - 1:.5f}")

    print("\n== brief-only model: empty states ==")
    bpool = [5000.0, 6100.0, 7300.0, 8600.0]
    check("trend pools the 4 brief prices",
          got["b_n"] == 4 and got["b_full"] == 0
          and approx(got["b_median"], statistics.median(bpool)),
          f"n={got['b_n']}, full={got['b_full']}, median={got['b_median']}")
    check("all-unique buckets -> mode reads 'none (all unique)'",
          got["b_mode"] == "none (all unique)", f"'{got['b_mode']}'")
    check("brief-heavy warning fires",
          isinstance(got["b_warn"], str) and "rougher" in got["b_warn"],
          f"'{str(got['b_warn'])[:70]}'")
    check("rich stats empty state + mileage gate",
          got["b_rich_empty"] == "no full listings yet for this selection"
          and "not enough data" in str(got["b_rich_slope"]),
          f"'{got['b_rich_empty']}' / '{str(got['b_rich_slope'])[:50]}'")
    check("listings unaffected (it is full-rows-global, not model-scoped)",
          got["b_list_count"] == n_full_total, f"count={got['b_list_count']}")
    check("daily table lists ONLY the selection's capture days (no blank rows)",
          got["b_day1"] == TODAY and got["b_day2"] in ("", None),
          f"slot1='{got['b_day1']}' (Bonly's one capture day), "
          f"slot2='{got['b_day2']}' (empty)")

    print("\n== comparison sheet ==")
    cmp_ = got["cmp"]
    first_pool = v[v["capture_date"] == v["capture_date"].min()]["price_aud"].dropna()
    last_pool = v[v["capture_date"] == v["capture_date"].max()]["price_aud"].dropna()
    want_chg = (float(statistics.median(last_pool))
                / float(statistics.median(first_pool)) - 1)
    check("slot B (Vtest Vmodel): n/avg/median/lo/hi over the pooled rows",
          cmp_["b_n"] == 30 and approx(cmp_["b_avg"], statistics.fmean(pool))
          and approx(cmp_["b_med"], exp["median"]) and approx(cmp_["b_lo"], exp["lowest"])
          and approx(cmp_["b_hi"], exp["highest"]),
          f"n={cmp_['b_n']}, avg={cmp_['b_avg']}, med={cmp_['b_med']}, "
          f"lo={cmp_['b_lo']}, hi={cmp_['b_hi']}")
    check("slot B change: first capture day median -> latest, signed",
          approx(cmp_["b_chg"], want_chg, rel=1e-4, abs_tol=1e-4),
          f"Excel {cmp_['b_chg']:+.4f} vs Python {want_chg:+.4f} "
          f"({statistics.median(first_pool):.0f} -> {statistics.median(last_pool):.0f})")
    import numpy as _np
    check("slot B quartiles and spread match numpy on the same pool",
          approx(cmp_["b_q25"], float(_np.percentile(pool, 25)))
          and approx(cmp_["b_q75"], float(_np.percentile(pool, 75)))
          and approx(cmp_["b_spread"], exp["highest"] - exp["lowest"]),
          f"q25={cmp_['b_q25']}, q75={cmp_['b_q75']}, spread={cmp_['b_spread']}")
    check("slot B distinct capture days counts days, not rows",
          cmp_["b_days"] == v["capture_date"].nunique(),
          f"{cmp_['b_days']} day(s) for {cmp_['b_n']} prices")
    check("rank orders the on-screen selections by median (cheaper = 1)",
          cmp_["c_rank"] == 1 and cmp_["b_rank"] == 2,
          f"B(median {cmp_['b_med']})={cmp_['b_rank']}, "
          f"C(median {cmp_['c_med']})={cmp_['c_rank']}")
    check("month block feeds one comparison chart",
          cmp_["charts"] == 1 and cmp_["months"] not in (None, ""),
          f"{cmp_['charts']} chart, first month cell={cmp_['months']!r}")
    check("slot C (Bonly): single capture day -> change shows '-'",
          cmp_["c_n"] == 4 and approx(cmp_["c_med"], 6700)
          and cmp_["c_chg"] == "-",
          f"n={cmp_['c_n']}, med={cmp_['c_med']}, chg='{cmp_['c_chg']}'")

    print("\n== forecasts sheet: pasted values match forecast.py ==")
    fc_rows = {r["line"]: r for r in got["fc"]}
    exp_fc = {}
    for line, sub in df.groupby("model_line"):
        prepped = analyze.prep(sub)
        mfit = analyze.fit_mileage(prepped)
        prepped = analyze.apply_mileage_adjustment(prepped, mfit)
        pcol = "price_adj" if mfit.get("usable") else "price_aud"
        stats = analyze.bucket_stats(prepped, pcol)
        exp_fc[line] = forecast.forecast_segment(prepped, stats, horizon=3, price_col=pcol)
    vt = fc_rows.get("Vtest Vmodel", {})
    exp_pts = exp_fc["Vtest Vmodel"]["points"]
    ok = (vt.get("method") == exp_fc["Vtest Vmodel"]["method"]
          and approx(vt.get("level"), exp_fc["Vtest Vmodel"]["level"])
          and approx(vt.get("m1"), float(exp_pts.iloc[0]["forecast"]))
          and approx(vt.get("m1lo"), float(exp_pts.iloc[0]["lo"]))
          and approx(vt.get("m1hi"), float(exp_pts.iloc[0]["hi"]))
          and fc_rows.get("Bonly Bmodel", {}).get("method") == "insufficient"
          and fc_rows.get("Bonly Bmodel", {}).get("m1") in ("", None))
    check("forecast rows: numeric m+1 fc/lo/hi match forecast.py; "
          "insufficient row stays empty", ok,
          f"Vtest m+1: {vt.get('m1')} [{vt.get('m1lo')}-{vt.get('m1hi')}] want "
          f"{float(exp_pts.iloc[0]['forecast'])} "
          f"[{float(exp_pts.iloc[0]['lo'])}-{float(exp_pts.iloc[0]['hi'])}]; "
          f"Bonly m+1='{fc_rows.get('Bonly Bmodel', {}).get('m1')}'")
    check("trim-level forecast rows present (XR and XR Turbo qualify at n>=8)",
          "Vtest Vmodel XR" in fc_rows and "Vtest Vmodel XR Turbo" in fc_rows
          and fc_rows["Vtest Vmodel XR"]["method"] == "robust_trend"
          and fc_rows["Vtest Vmodel XR"]["n"] == 16,
          f"XR: {fc_rows.get('Vtest Vmodel XR', {}).get('method')} "
          f"n={fc_rows.get('Vtest Vmodel XR', {}).get('n')}; "
          f"XR Turbo: {fc_rows.get('Vtest Vmodel XR Turbo', {}).get('method')}")
    check("selection panel binds to the Trend picker (INDEX/MATCH)",
          got["panel_method"] == exp_fc["Vtest Vmodel"]["method"],
          f"panel method='{got['panel_method']}' for Vtest Vmodel")

    print("\n== supply: how many for sale at each capture ==")
    p, rows = got["sp_panel"], got["sp_rows"]
    # fixture: 3 listings captured on each of 3 days, prices 20500..23000
    check("panel: 3 captures, 3 for sale now, average 3, most/fewest 3, no change",
          p["captures"] == 3 and p["now"] == 3 and approx(p["avg"], 3)
          and p["most"] == 3 and p["fewest"] == 3 and p["change"] == 0,
          f"captures={p['captures']}, now={p['now']}, avg={p['avg']}, "
          f"most={p['most']}, fewest={p['fewest']}, change={p['change']}")
    check("row per capture day with the right count and median price",
          [r["day"] for r in rows] == ["2026-05-03", "2026-06-03", "2026-07-03"]
          and [r["forsale"] for r in rows] == [3, 3, 3]
          and approx(rows[0]["median"], 21000) and approx(rows[1]["median"], 21500)
          and approx(rows[2]["median"], 22500),
          "; ".join(f"{r['day']}: {r['forsale']} for sale, median {r['median']}"
                    for r in rows))
    check("first row has no change, later rows compare to the previous capture",
          str(rows[0]["change"]) == "-" and rows[1]["change"] == 0
          and rows[2]["change"] == 0,
          f"changes: '{rows[0]['change']}', {rows[1]['change']}, {rows[2]['change']}")
    check("only days this selection was captured appear",
          got["sp_row4_day"] in ("", None), f"row 4 day='{got['sp_row4_day']}'")
    check("old Movement/Stock sheets are gone from the workbook",
          "Movement" not in got["sheets"] and "Stock" not in got["sheets"]
          and "Supply" in got["sheets"],
          f"sheets: {got['sheets']}")
    print("\n== formula errors ==")
    hard = {k: c for k, c in got.get("hard_errors", {}).items() if c}
    check("no #REF!/#DIV/0!/#VALUE!/#NAME? on ANY sheet", not hard,
          f"hard errors per sheet: {got.get('hard_errors')} "
          f"(#N/A excluded - NA() is how a chart shows a gap)")
    # #N/A is only legitimate where it feeds a chart: _Chart's scatter and
    # the Comparison month block.
    naonly = {k: c for k, c in got["errors"].items()
              if c and k not in ("_Chart", "Comparison")}
    check("no #N/A outside the two chart-feeding blocks", not naonly,
          f"error cells per sheet: {got['errors']} (_Chart #N/A = "
          f"intentional scatter gaps)")

    print("\n== hybrid: styling kept, blocks sized to data, MyView untouched ==")
    from openpyxl import load_workbook
    from openpyxl.styles import Font as XFont
    wbx = load_workbook(XLSX)
    wbx["Trend"]["A5"].font = XFont(name="Arial", size=10, bold=True,
                                    color="FFFF0000")     # user restyling
    wbx["Trend"].column_dimensions["A"].width = 33.0
    wbx["MyView"]["A20"] = "my own layout"                # user's sandbox
    wbx.create_sheet("MyNotes")["A1"] = "keep me"         # user's own sheet
    wbx.save(XLSX)
    wbx.close()
    quickadd.commit_payload({"make": "Vtest", "model": "Vmodel",
                             "prices": ["99000"]}, DB)
    generate(DB, XLSX)                                    # ordinary refresh
    wbx = load_workbook(XLSX)
    tr, a5 = wbx["Trend"], wbx["Trend"]["A5"]
    check("styling survives a refresh: manual font + column width kept",
          a5.font.bold and str(a5.font.color.rgb) == "FFFF0000"
          and abs(tr.column_dimensions["A"].width - 33.0) < 0.01,
          f"A5 bold={a5.font.bold}/{a5.font.color.rgb}, "
          f"width A={tr.column_dimensions['A'].width}")
    check("MyView and your own sheets are never written to",
          wbx["MyView"]["A20"].value == "my own layout"
          and wbx["MyNotes"]["A1"].value == "keep me",
          f"MyView A20='{wbx['MyView']['A20'].value}', "
          f"MyNotes='{wbx['MyNotes']['A1'].value}'")
    with Store(DB) as store:
        ddf = store.frame()
    n_days = ddf["capture_date"].dt.strftime("%Y-%m-%d").nunique()
    day_hdr = next(r for r in range(10, 300)
                   if tr[f"A{r}"].value == "capture day")
    filled = sum(1 for r in range(day_hdr + 1, day_hdr + 400)
                 if tr[f"A{r}"].value)
    trailing = tr[f"A{day_hdr + n_days + 1}"].value
    check("capture-day block is exactly as long as the data (charts stay clean)",
          filled == n_days and trailing is None,
          f"{filled} rows for {n_days} capture days; row after block="
          f"{trailing!r}")
    wbx.close()
    got2 = read_via_excel(XLSX, n_full_total)
    check("refreshed views still compute (n 30->31, high $99,000)",
          got2["v_n"] == 31 and approx(got2["v_hi"], 99000)
          and got2["v_comp"] == "from 31 prices: 24 full, 7 brief",
          f"n={got2['v_n']}, hi={got2['v_hi']}, comp='{got2['v_comp']}'")

    print("\n== migration: a workbook from an older layout is repaired ==")
    wbx = load_workbook(XLSX)
    del wbx.defined_names["vpa_layout"]                 # pretend: older build
    wbx["Trend"]["B5"] = "=Data!$I$2"                   # sheet that is gone
    wbx.create_sheet("Data")["A1"] = "old backend"
    wbx.save(XLSX)
    wbx.close()
    generate(DB, XLSX)
    wbx = load_workbook(XLSX)
    check("older-layout workbook: dead sheet dropped, formulas rebuilt",
          "Data" not in wbx.sheetnames
          and "Data!" not in str(wbx["Trend"]["B5"].value)
          and (wbx.defined_names["vpa_layout"].attr_text.strip('"')
               == LAYOUT_VERSION),
          f"stale 'Data' gone={'Data' not in wbx.sheetnames}, "
          f"stamp={wbx.defined_names['vpa_layout'].attr_text}")
    wbx.close()

    print("\n== --reset-styles restores the default look ==")
    generate(DB, XLSX, reset_styles=True)
    wbx = load_workbook(XLSX)
    # A5 is the "median" label, bold by design - the tell is the red colour
    check("reset-styles clears manual styling but keeps your own sheets",
          str(wbx["Trend"]["A5"].font.color and
              wbx["Trend"]["A5"].font.color.rgb) != "FFFF0000"
          and wbx["MyView"]["A20"].value == "my own layout"
          and "MyNotes" in wbx.sheetnames,
          f"A5 colour={wbx['Trend']['A5'].font.color and wbx['Trend']['A5'].font.color.rgb}, "
          f"MyView kept={wbx['MyView']['A20'].value == 'my own layout'}")
    wbx.close()

    fails = [n for ok_, n in RESULTS if not ok_]
    print(f"\n== {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ==")
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
