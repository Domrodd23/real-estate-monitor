"""PDF + Excel exports of the computed metrics.

Excel: one workbook with a per-market ZIP sheet, a counties sheet, and a sources
sheet. PDF: one shareable report per market (county snapshot, per-ZIP metrics, a
home-value chart, and a source-and-date footer). Charts are rendered headless via
matplotlib (no browser), so this runs unattended in CI.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .config import Config
from .http import last_cached
from .logging_setup import get_logger
from .sources import zillow

log = get_logger("remon.exports")

# Display formatting per metric for the export tables.
ZIP_METRIC_ORDER = [
    ("home_value", "Home value", lambda v: f"${v:,.0f}"),
    ("home_value_12m_pct", "Value 12-mo %", lambda v: f"{v:+.1f}%"),
    ("rent", "Rent/mo", lambda v: f"${v:,.0f}"),
    ("rent_12m_pct", "Rent 12-mo %", lambda v: f"{v:+.1f}%"),
    ("price_to_rent", "Price-to-rent", lambda v: f"{v:.1f}"),
    ("overvaluation_pr_pct", "Overvaluation %", lambda v: f"{v:+.1f}%"),
]


def _sheet_name(name: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", "", name)[:31]


def _safe_file(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def write_excel(config: Config, df: pd.DataFrame, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(dest, engine="openpyxl") as xw:
        for m in config.markets.values():
            z = df[(df["market"] == m.key) & (df["geography"] == "zip")]
            if z.empty:
                continue
            wide = z.pivot_table(index="region", columns="metric", values="value",
                                 aggfunc="first").reset_index().rename(columns={"region": "zip"})
            wide.to_excel(xw, sheet_name=_sheet_name(f"{m.name} ZIPs"), index=False)
        county = df[df["geography"] == "county"]
        if not county.empty:
            cw = county.pivot_table(index=["market_name", "region_label"], columns="metric",
                                    values="value", aggfunc="first").reset_index()
            cw.to_excel(xw, sheet_name="Counties", index=False)
        src = (df.groupby("source")["as_of"].max().reset_index()
               .rename(columns={"as_of": "most_recent"}))
        src.to_excel(xw, sheet_name="Sources", index=False)
    log.info("Wrote Excel export -> %s", dest)
    return dest


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _market_chart_png(config: Config, market, raw_dir: Path) -> io.BytesIO:
    """Multi-line home-value chart for a market's ZIPs as a PNG buffer."""
    path = last_cached(raw_dir, "zillow_zhvi_zip", "csv")
    df, dates = zillow.load_zip_series(path, market.zips, "Zillow ZHVI")
    df = df.set_index("RegionName")
    fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=150)
    x = pd.to_datetime(dates)
    for z in market.zips:
        if z in df.index:
            ax.plot(x, df.loc[z, dates].values, linewidth=1, label=z)
    ax.set_title(f"{market.name} — home values (ZHVI)", fontsize=10)
    ax.set_ylabel("Home value ($)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=6, ncol=5, loc="upper left")
    ax.yaxis.set_major_formatter(lambda v, _: f"${v/1000:.0f}k")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


def _fmt(metric: str, value, fmt) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return fmt(value)


def write_market_pdf(config: Config, df: pd.DataFrame, market, dest: Path, raw_dir: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    story: List = []

    story.append(Paragraph(f"Real estate monitor — {market.name}", styles["Title"]))
    story.append(Paragraph(
        f"Public-data market report. Generated {datetime.now():%Y-%m-%d}. "
        f"Sources: Zillow, Redfin, FRED, Census.", small))
    story.append(Spacer(1, 14))

    # County snapshot
    county = df[(df["market"] == market.key) & (df["geography"] == "county")]
    metro = df[(df["market"] == market.key) & (df["geography"] == "metro")]
    snap = pd.concat([county, metro])
    if not snap.empty:
        story.append(Paragraph(f"County snapshot — {market.county.name}", styles["Heading2"]))
        rows = [["Metric", "Value", "Source", "As of"]]
        for _, r in snap.iterrows():
            val = r["value"]
            disp = f"{val:,.0f}" if r["unit"] in ("USD", "people", "people/yr", "listings") \
                else (f"{val:.3f}" if r["unit"] in ("share", "ratio") else f"{val}")
            rows.append([r["metric"], f"{disp} {r['unit']}", r["source"], str(r["as_of"])])
        t = Table(rows, hAlign="LEFT", colWidths=[1.7*inch, 1.7*inch, 1.9*inch, 0.9*inch])
        t.setStyle(_table_style())
        story.append(t)
        story.append(Spacer(1, 12))

    # Per-ZIP metrics
    story.append(Paragraph("Metrics by ZIP", styles["Heading2"]))
    pivot = (df[(df["market"] == market.key) & (df["geography"] == "zip")]
             .pivot_table(index="region", columns="metric", values="value", aggfunc="first"))
    header = ["ZIP"] + [label for _, label, _ in ZIP_METRIC_ORDER]
    rows = [header]
    for z in market.zips:
        row = [z]
        for metric, _label, fmt in ZIP_METRIC_ORDER:
            v = pivot.loc[z][metric] if (z in pivot.index and metric in pivot.columns) else None
            row.append(_fmt(metric, v, fmt))
        rows.append(row)
    t = Table(rows, hAlign="LEFT", repeatRows=1)
    t.setStyle(_table_style())
    story.append(t)
    story.append(Spacer(1, 14))

    # Chart
    try:
        story.append(Image(_market_chart_png(config, market, raw_dir), width=6.8*inch, height=3.0*inch))
    except Exception as exc:  # noqa: BLE001 — chart is nice-to-have
        log.warning("[pdf] chart skipped for %s: %s", market.name, exc)

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "Overvaluation proxy is a directional public-ratio indicator (current "
        "price-to-rent vs the ZIP's own 5-year median), not a forecast. Blanks "
        "mean the free data does not support that metric for that ZIP.", small))

    SimpleDocTemplate(str(dest), pagesize=letter, title=f"Real estate monitor — {market.name}").build(story)
    log.info("Wrote PDF export -> %s", dest)
    return dest


def _table_style() -> TableStyle:
    return TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f5fa8")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9dee3")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def generate_exports(config: Config) -> List[dict]:
    """Write the xlsx workbook + per-market PDFs into docs/exports/.

    Returns a list of {label, href} (href relative to docs/) for footer links.
    """
    metrics_path = config.processed_dir / "metrics.csv"
    if not metrics_path.exists():
        raise FileNotFoundError(f"{metrics_path} not found. Run compute.py first.")
    df = pd.read_csv(metrics_path, dtype={"region": str})

    out_dir = config.docs_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    links: List[dict] = []

    xlsx = write_excel(config, df, out_dir / "metrics_full.xlsx")
    links.append({"label": "Excel workbook (all metrics)", "href": f"exports/{xlsx.name}"})

    for m in config.markets.values():
        pdf = write_market_pdf(config, df, m, out_dir / f"{_safe_file(m.name)}.pdf", config.raw_dir)
        links.append({"label": f"PDF report — {m.name}", "href": f"exports/{pdf.name}"})

    return links
