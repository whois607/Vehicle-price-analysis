"""Charts: matplotlib PNGs (for notes/documents) and offline plotly HTML
(for exploring). Same content in both: trend = median + IQR band + raw
listings + volume; forecast = history + interval-shaded projection with
the method and sample size written on the chart, not hidden in a footnote.

The plotly bundle is ~4.6 MB. Inlining it in every page (include_plotlyjs
=True) made each report file 4.7 MB for ~40 kB of actual chart. The HTML
writers therefore take a `plotlyjs` reference: pass a relative ".js" path
and the pages share one copy written by write_plotly_bundle(). Still fully
offline - no CDN - just not duplicated per file.
"""
from __future__ import annotations

from pathlib import Path

# Shared bundle written once per report tree; charts live one level deeper
# (output/reports/<segment>/trend.html), hence the "../".
PLOTLY_BUNDLE = "plotly.min.js"
SHARED_PLOTLYJS = "../" + PLOTLY_BUNDLE


def write_plotly_bundle(dirpath: Path) -> Path:
    """Write the bundled plotly.min.js into dirpath (once) and return it.

    Rewrites only when missing or a different size, so repeated report runs
    don't churn a 4.6 MB file through OneDrive.
    """
    from plotly.offline import get_plotlyjs
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / PLOTLY_BUNDLE
    # write_bytes, not write_text: on Windows text mode turns every \n into
    # \r\n, so the file would never match the length we compare against and
    # 4.6 MB would be rewritten (and re-synced) on every single report run.
    src = get_plotlyjs().encode("utf-8")
    if not path.exists() or path.stat().st_size != len(src):
        path.write_bytes(src)
    return path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

_AUD = mticker.FuncFormatter(lambda v, _: f"${v:,.0f}")


def _fc_note(fc: dict) -> str:
    return (f"method: {fc['method']} | n={fc['n_total']} listings, "
            f"{fc['n_buckets']} buckets | {fc['interval_level']}% interval")


# ---------------------------------------------------------------- matplotlib
def trend_png(prepped: pd.DataFrame, stats: pd.DataFrame, title: str,
              path: Path, price_col: str = "price_aud") -> None:
    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True, height_ratios=[3, 1],
        gridspec_kw={"hspace": 0.08})
    ax.scatter(prepped["capture_date"], prepped[price_col], s=14, alpha=0.35,
               color="#4878a8", label="listings", zorder=2)
    if not stats.empty:
        ax.fill_between(stats["bucket"], stats["q25"], stats["q75"],
                        alpha=0.18, color="#c04040", label="IQR (25-75%)")
        ax.plot(stats["bucket"], stats["median"], "o-", color="#c04040",
                lw=2, label="median", zorder=3)
        for _, r in stats.iterrows():
            ax.annotate(f"n={int(r['n'])}", (r["bucket"], r["median"]),
                        textcoords="offset points", xytext=(0, 8),
                        fontsize=7, ha="center", color="#666666")
        axv.bar(stats["bucket"], stats["n"], width=20, color="#88aa88")
    ax.yaxis.set_major_formatter(_AUD)
    ax.set_title(title)
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    axv.set_ylabel("volume")
    axv.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def forecast_png(stats: pd.DataFrame, fc: dict, title: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    if not stats.empty:
        ax.plot(stats["bucket"], stats["median"], "o-", color="#c04040",
                lw=2, label="median (history)")
    pts = fc.get("points")
    if pts is not None and len(pts):
        # connect history to forecast
        lastx, lasty = stats.iloc[-1]["bucket"], stats.iloc[-1]["median"]
        fx = pd.concat([pd.DataFrame({"bucket": [lastx], "forecast": [lasty],
                                      "lo": [lasty], "hi": [lasty]}), pts])
        ax.plot(fx["bucket"], fx["forecast"], "o--", color="#3060a0",
                lw=2, label="forecast")
        ax.fill_between(fx["bucket"], fx["lo"], fx["hi"], alpha=0.20,
                        color="#3060a0",
                        label=f"{fc['interval_level']}% interval")
    else:
        ax.text(0.5, 0.12, "NO FORECAST: " + fc.get("verdict", "insufficient data"),
                transform=ax.transAxes, ha="center", fontsize=9,
                color="#a03030", wrap=True,
                bbox=dict(boxstyle="round", fc="#fff2f2", ec="#a03030"))
    ax.text(0.01, 0.02, _fc_note(fc), transform=ax.transAxes, fontsize=8,
            color="#444444",
            bbox=dict(boxstyle="round", fc="#f4f4f4", ec="#bbbbbb"))
    ax.yaxis.set_major_formatter(_AUD)
    ax.set_title(f"{title} - {fc['horizon_months']}-month forecast")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------- plotly
def trend_html(prepped: pd.DataFrame, stats: pd.DataFrame, title: str,
               path: Path, price_col: str = "price_aud",
               plotlyjs: bool | str = True) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.75, 0.25], vertical_spacing=0.06)
    hover = (prepped["raw_title"].fillna(prepped["make"] + " " + prepped["model"])
             + "<br>" + prepped["source"]
             + " | odo: " + prepped["odometer_km"].map(
                 lambda v: f"{v:,.0f} km" if pd.notna(v) else "?"))
    fig.add_trace(go.Scatter(
        x=prepped["capture_date"], y=prepped[price_col], mode="markers",
        name="listings", marker=dict(size=6, opacity=0.45), text=hover,
        hovertemplate="%{text}<br>$%{y:,.0f}<extra></extra>"), row=1, col=1)
    if not stats.empty:
        fig.add_trace(go.Scatter(x=stats["bucket"], y=stats["q75"], mode="lines",
                                 line=dict(width=0), showlegend=False,
                                 hoverinfo="skip"), row=1, col=1)
        fig.add_trace(go.Scatter(x=stats["bucket"], y=stats["q25"], mode="lines",
                                 line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(192,64,64,0.15)",
                                 name="IQR (25-75%)", hoverinfo="skip"), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=stats["bucket"], y=stats["median"], mode="lines+markers",
            name="median", line=dict(color="#c04040", width=2),
            customdata=stats["n"],
            hovertemplate="median $%{y:,.0f} (n=%{customdata})<extra></extra>"),
            row=1, col=1)
        fig.add_trace(go.Bar(x=stats["bucket"], y=stats["n"], name="volume",
                             marker_color="#88aa88"), row=2, col=1)
    fig.update_yaxes(tickformat="$,.0f", row=1, col=1)
    fig.update_layout(title=title, height=560, template="plotly_white",
                      legend=dict(orientation="h", y=1.06))
    fig.write_html(path, include_plotlyjs=plotlyjs)


def forecast_html(stats: pd.DataFrame, fc: dict, title: str, path: Path,
                  plotlyjs: bool | str = True) -> None:
    import plotly.graph_objects as go
    fig = go.Figure()
    if not stats.empty:
        fig.add_trace(go.Scatter(
            x=stats["bucket"], y=stats["median"], mode="lines+markers",
            name="median (history)", line=dict(color="#c04040", width=2),
            customdata=stats["n"],
            hovertemplate="median $%{y:,.0f} (n=%{customdata})<extra></extra>"))
    pts = fc.get("points")
    if pts is not None and len(pts):
        lastx, lasty = stats.iloc[-1]["bucket"], stats.iloc[-1]["median"]
        fx = pd.concat([pd.DataFrame({"bucket": [lastx], "forecast": [lasty],
                                      "lo": [lasty], "hi": [lasty]}), pts])
        fig.add_trace(go.Scatter(x=fx["bucket"], y=fx["hi"], mode="lines",
                                 line=dict(width=0), showlegend=False,
                                 hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=fx["bucket"], y=fx["lo"], mode="lines",
                                 line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(48,96,160,0.18)",
                                 name=f"{fc['interval_level']}% interval",
                                 hoverinfo="skip"))
        fig.add_trace(go.Scatter(
            x=fx["bucket"], y=fx["forecast"], mode="lines+markers",
            name="forecast", line=dict(color="#3060a0", width=2, dash="dash"),
            hovertemplate="forecast $%{y:,.0f}<extra></extra>"))
    else:
        fig.add_annotation(text="NO FORECAST: " + fc.get("verdict", "insufficient data"),
                           xref="paper", yref="paper", x=0.5, y=0.1,
                           showarrow=False, font=dict(color="#a03030"))
    fig.add_annotation(text=_fc_note(fc), xref="paper", yref="paper",
                       x=0.01, y=0.01, showarrow=False, align="left",
                       font=dict(size=10, color="#444444"),
                       bgcolor="#f4f4f4", bordercolor="#bbbbbb")
    fig.update_yaxes(tickformat="$,.0f")
    fig.update_layout(title=f"{title} - {fc['horizon_months']}-month forecast",
                      height=480, template="plotly_white",
                      legend=dict(orientation="h", y=1.08))
    fig.write_html(path, include_plotlyjs=plotlyjs)
