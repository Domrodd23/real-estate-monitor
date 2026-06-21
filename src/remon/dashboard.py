"""Render the static dashboard (docs/index.html) from metrics.csv + raw series.

Single self-contained page: Plotly is written once to docs/assets/plotly.min.js
and referenced locally, so charts work both on GitHub Pages and when the file is
opened directly from disk (offline). Also writes the flat per-ZIP CSV export.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from plotly.offline import get_plotlyjs

from .config import Config
from .http import last_cached
from .logging_setup import get_logger
from .sources import zillow

log = get_logger("remon.dashboard")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STALE_DAYS = 75


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _f(v) -> bool:
    return v is not None and not (isinstance(v, float) and pd.isna(v))


def usd(v):       return f"${v:,.0f}" if _f(v) else None
def usd_mo(v):    return f"${v:,.0f}/mo" if _f(v) else None
def pct(v):       return f"{v:+.1f}%" if _f(v) else None
def ratio(v):     return f"{v:.1f}" if _f(v) else None
def count(v):     return f"{int(round(v)):,}" if _f(v) else None
def signed(v):    return f"{int(round(v)):+,}" if _f(v) else None
def days(v):      return f"{int(round(v))} days" if _f(v) else None
def share(v):     return f"{v*100:.1f}%" if _f(v) else None


def _is_full_date(s: str) -> bool:
    return isinstance(s, str) and len(s) == 10 and s[4] == "-" and s[7] == "-"


def _stale(as_of: str) -> bool:
    if not _is_full_date(as_of):
        return False  # annual sources (year-only) aren't "stale"
    try:
        return (datetime.now() - datetime.fromisoformat(as_of)).days > STALE_DAYS
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def _series(row: pd.Series, date_cols: List[str]):
    pts = [(pd.to_datetime(d), row[d]) for d in date_cols if pd.notna(row[d])]
    pts.sort()
    return [p[0] for p in pts], [p[1] for p in pts]


def _zip_chart(zip_code, zhvi_row, zhvi_dates, zori_row, zori_dates) -> Optional[str]:
    if zhvi_row is None:
        return None
    fig = go.Figure()
    hx, hy = _series(zhvi_row, zhvi_dates)
    fig.add_trace(go.Scatter(x=hx, y=hy, name="Home value (ZHVI)",
                             line=dict(color="#1f5fa8", width=2)))
    if zori_row is not None:
        rx, ry = _series(zori_row, zori_dates)
        if rx:
            fig.add_trace(go.Scatter(x=rx, y=ry, name="Rent (ZORI)", yaxis="y2",
                                     line=dict(color="#c8741a", width=2)))
    fig.update_layout(
        title=dict(text=f"ZIP {zip_code}", x=0.01, font=dict(size=13)),
        height=300, margin=dict(l=10, r=10, t=34, b=24),
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=10)),
        yaxis=dict(title="Home value ($)", rangemode="tozero"),
        yaxis2=dict(title="Rent ($/mo)", overlaying="y", side="right", rangemode="tozero"),
        plot_bgcolor="white", hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eef1f4")
    fig.update_yaxes(showgrid=True, gridcolor="#eef1f4")
    return pio.to_html(fig, include_plotlyjs=False, full_html=False,
                       config={"displayModeBar": False})


# --------------------------------------------------------------------------- #
# Build view model
# --------------------------------------------------------------------------- #
def _zip_pivot(df: pd.DataFrame) -> pd.DataFrame:
    z = df[df["geography"] == "zip"]
    return z.pivot_table(index="region", columns="metric", values="value", aggfunc="first")


def _other_lookup(df: pd.DataFrame) -> Dict:
    """{(market, metric): row} for county/metro/national metrics."""
    out = {}
    for _, r in df[df["geography"].isin(["county", "metro", "national"])].iterrows():
        out[(r["market"], r["metric"])] = r
    return out


SNAPSHOT_SPECS = [
    ("median_household_income", "Median household income", usd, None),
    ("population", "Population", count, None),
    ("net_migration", "Net migration / yr", signed, "signed"),
    ("median_sale_price", "Median sale price", usd, None),
    ("median_dom", "Days on market", days, None),
    ("price_drop_share", "Price-drop share", share, None),
    ("inventory", "For-sale inventory", count, None),
    ("new_listings", "New listings / mo", count, None),
]


def render_dashboard(config: Config) -> Path:
    raw_dir = config.raw_dir
    metrics_path = config.processed_dir / "metrics.csv"
    if not metrics_path.exists():
        raise FileNotFoundError(
            f"{metrics_path} not found. Run `python compute.py` first."
        )
    df = pd.read_csv(metrics_path, dtype={"region": str})

    pivot = _zip_pivot(df)
    other = _other_lookup(df)

    # Raw series for charts.
    zhvi, zhvi_dates = zillow.load_zip_series(
        last_cached(raw_dir, "zillow_zhvi_zip", "csv"), config.all_zips(), "Zillow ZHVI")
    zori, zori_dates = zillow.load_zip_series(
        last_cached(raw_dir, "zillow_zori_zip", "csv"), config.all_zips(), "Zillow ZORI")
    zhvi = zhvi.set_index("RegionName"); zhvi = zhvi[~zhvi.index.duplicated()]
    zori = zori.set_index("RegionName"); zori = zori[~zori.index.duplicated()]

    markets_vm = []
    for m in config.markets.values():
        # County/metro snapshot cards
        snapshot = []
        for metric, label, fmt, kind in SNAPSHOT_SPECS:
            r = other.get((m.key, metric))
            if r is None:
                continue
            val = fmt(r["value"])
            if val is None:
                continue
            cls = ""
            if kind == "signed":
                cls = "pos" if r["value"] > 0 else "neg"
            snapshot.append({"label": label, "value": val, "cls": cls,
                             "source": r["source"], "as_of": r["as_of"]})

        # Per-ZIP rows + charts
        zip_rows = []
        for z in m.zips:
            row = pivot.loc[z] if z in pivot.index else None

            def g(metric):
                return row[metric] if row is not None and metric in row and pd.notna(row[metric]) else None

            hv12 = g("home_value_12m_pct")
            rt12 = g("rent_12m_pct")
            ov = g("overvaluation_pr_pct")
            zip_rows.append({
                "zip": z,
                "home_value": usd(g("home_value")) or "—",
                "home_value_12m": pct(hv12) or "—",
                "hv_cls": ("pos" if _f(hv12) and hv12 > 0 else "neg") if _f(hv12) else "na",
                "rent": usd_mo(g("rent")) or "—",
                "rent_12m": pct(rt12) or "—",
                "rent_cls": ("pos" if _f(rt12) and rt12 > 0 else "neg") if _f(rt12) else "na",
                "price_to_rent": ratio(g("price_to_rent")) or "—",
                "overvaluation": pct(ov) or "—",
                # positive proxy = pricier than its own norm → caution (red)
                "ov_cls": ("neg" if _f(ov) and ov > 0 else "pos") if _f(ov) else "na",
                "chart": _zip_chart(
                    z,
                    zhvi.loc[z] if z in zhvi.index else None, zhvi_dates,
                    zori.loc[z] if z in zori.index else None, zori_dates,
                ),
            })

        markets_vm.append({
            "name": m.name, "county_name": m.county.name,
            "snapshot": snapshot, "zips": zip_rows,
        })

    # National + sources footer
    mort = other.get(("national", "mortgage_30yr"))
    src_tbl = (
        df.groupby("source")["as_of"].max().reset_index().sort_values("source")
    )
    sources = [{"source": r["source"], "as_of": r["as_of"], "stale": _stale(r["as_of"])}
               for _, r in src_tbl.iterrows()]

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html"]))
    html = env.get_template("dashboard.html.j2").render(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        mortgage=(pct(mort["value"]).lstrip("+") if mort is not None else None),
        mortgage_asof=(mort["as_of"] if mort is not None else None),
        markets=markets_vm, sources=sources,
    )

    # Write outputs
    docs = config.docs_dir
    (docs / "assets").mkdir(parents=True, exist_ok=True)
    (docs / "assets" / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    index = docs / "index.html"
    index.write_text(html, encoding="utf-8")
    _write_csv_export(config, df)
    log.info("Wrote dashboard -> %s", index)
    return index


def _write_csv_export(config: Config, df: pd.DataFrame) -> None:
    z = df[df["geography"] == "zip"].copy()
    wide = z.pivot_table(index=["market_name", "region"], columns="metric",
                         values="value", aggfunc="first").reset_index()
    wide = wide.rename(columns={"region": "zip", "market_name": "market"})
    dest = config.path(config.output.get("csv_export", "docs/metrics_latest.csv"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    wide.to_csv(dest, index=False)
    log.info("Wrote CSV export -> %s (%d ZIPs)", dest, len(wide))
