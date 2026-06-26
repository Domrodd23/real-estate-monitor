"""National county map explorer (docs/map.html).

Renders an interactive US county choropleth from county-level public data
(Zillow ZHVI/ZORI by county, Census ACS by county). A dropdown switches the
displayed metric entirely client-side. The user's tracked counties are outlined.

This is a separate page; the core dashboard is unchanged. Built best-effort:
build.py logs a warning and continues if the national data isn't available.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import plotly.colors as pcol
import plotly.io as pio
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .http import cache_path, get_text, last_cached
from .logging_setup import get_logger
from .sources import zillow

log = get_logger("remon.mapview")

GEOJSON_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Displayed metrics. `diverging` centers the scale on 0 (for % change).
METRICS = [
    {"key": "home_value", "label": "Home value", "fmt": "usd", "scale": "Viridis"},
    {"key": "home_value_12m_pct", "label": "Home value, 1-yr change", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True},
    {"key": "rent", "label": "Rent (where available)", "fmt": "usd_mo", "scale": "Viridis"},
    {"key": "price_to_rent", "label": "Price-to-rent", "fmt": "ratio", "scale": "Plasma"},
    {"key": "median_income", "label": "Median household income", "fmt": "usd", "scale": "Viridis"},
    {"key": "population", "label": "Population", "fmt": "count", "scale": "Cividis"},
]


def _fmt(kind, v):
    if v is None or pd.isna(v):
        return "n/a"
    if kind == "usd":
        return f"${v:,.0f}"
    if kind == "usd_mo":
        return f"${v:,.0f}/mo"
    if kind == "pct":
        return f"{v:+.1f}%"
    if kind == "ratio":
        return f"{v:.1f}x"
    if kind == "count":
        return f"{v:,.0f}"
    return str(v)


def _scale_array(name: str):
    """Named plotly colorscale -> explicit [[pos, 'rgb(...)'], ...] array."""
    try:
        return pcol.get_colorscale(name)
    except Exception:  # noqa: BLE001
        return pcol.get_colorscale("Viridis")


def _load_geojson(config: Config):
    raw = config.raw_dir
    path = last_cached(raw, "us_counties_geojson", "json")
    if not path:
        log.info("Fetching US counties GeoJSON (one-time, ~3 MB)")
        text = get_text(GEOJSON_URL)
        path = cache_path(raw, "us_counties_geojson", "json")
        path.write_text(text, encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def build_county_table(config: Config) -> pd.DataFrame:
    """One row per US county (indexed by FIPS) with the map metrics."""
    raw = config.raw_dir

    zhvi_path = last_cached(raw, "zillow_zhvi_county", "csv")
    if not zhvi_path:
        raise FileNotFoundError("county ZHVI not cached — run fetch.py first")
    zhvi, zd = zillow.load_county_series(zhvi_path, "Zillow county ZHVI")
    latest = zd[-1]
    prior = zd[-13] if len(zd) >= 13 else zd[0]
    zhvi = zhvi.drop_duplicates(subset="fips").set_index("fips")
    df = pd.DataFrame(index=zhvi.index)
    df["name"] = zhvi["RegionName"].astype(str) + ", " + zhvi.get("State", "").astype(str)
    df["home_value"] = pd.to_numeric(zhvi[latest], errors="coerce")
    prior_v = pd.to_numeric(zhvi[prior], errors="coerce")
    df["home_value_12m_pct"] = (df["home_value"] / prior_v - 1.0) * 100.0

    # Rent (ZORI) — partial county coverage
    zori_path = last_cached(raw, "zillow_zori_county", "csv")
    if zori_path:
        zori, zod = zillow.load_county_series(zori_path, "Zillow county ZORI")
        zori = zori.drop_duplicates(subset="fips").set_index("fips")
        df["rent"] = pd.to_numeric(zori[zod[-1]], errors="coerce").reindex(df.index)
        df["price_to_rent"] = df["home_value"] / (df["rent"] * 12.0)
    else:
        df["rent"] = pd.NA
        df["price_to_rent"] = pd.NA

    # Census ACS national income + population
    acs_path = last_cached(raw, "census_acs_national", "csv")
    if acs_path:
        acs = pd.read_csv(acs_path, dtype={"county_fips": str}).drop_duplicates("county_fips")
        acs = acs.set_index("county_fips")
        df["median_income"] = pd.to_numeric(acs.get("median_household_income"), errors="coerce").reindex(df.index)
        df["population"] = pd.to_numeric(acs.get("population"), errors="coerce").reindex(df.index)
    else:
        df["median_income"] = pd.NA
        df["population"] = pd.NA

    tracked = {m.county.fips for m in config.markets.values()}
    df["is_tracked"] = df.index.isin(tracked)
    log.info("[map] built county table: %d counties", len(df))
    return df


def _prep(df: pd.DataFrame, m: Dict):
    vals = pd.to_numeric(df[m["key"]], errors="coerce")
    clean = vals.dropna()
    if m.get("diverging"):
        a = float(clean.abs().quantile(0.95)) if len(clean) else 1.0
        zmin, zmax = -a, a
    else:
        zmin = float(clean.quantile(0.05)) if len(clean) else 0.0
        zmax = float(clean.quantile(0.95)) if len(clean) else 1.0
    z = [None if pd.isna(v) else float(v) for v in vals]
    text = [f"{nm}<br>{m['label']}: {_fmt(m['fmt'], v)}"
            for nm, v in zip(df["name"], vals)]
    return z, text, zmin, zmax, m["scale"]


def render_map_page(config: Config) -> Path:
    df = build_county_table(config)
    geojson = _load_geojson(config)
    fips = df.index.tolist()

    # Per-metric data for the pill control bar (client-side switch via restyle).
    metric_data = {}
    for m in METRICS:
        z, text, zmin, zmax, scale = _prep(df, m)
        metric_data[m["key"]] = {"z": z, "text": text, "zmin": zmin, "zmax": zmax,
                                 "colorscale": _scale_array(scale), "label": m["label"]}
    first = METRICS[0]
    d0 = metric_data[first["key"]]

    fig = go.Figure(go.Choropleth(
        geojson=geojson, locations=fips, featureidkey="id",
        z=d0["z"], text=d0["text"], hovertemplate="%{text}<extra></extra>",
        colorscale=d0["colorscale"], zmin=d0["zmin"], zmax=d0["zmax"],
        marker_line_width=0.12, marker_line_color="rgba(255,255,255,0.6)",
        colorbar=dict(title=dict(text=first["label"], side="right"), thickness=13,
                      len=0.9, x=0.99, outlinewidth=0),
    ))
    tracked = df[df["is_tracked"]]
    if len(tracked):
        fig.add_trace(go.Choropleth(
            geojson=geojson, locations=tracked.index.tolist(), featureidkey="id",
            z=[0] * len(tracked), showscale=False,
            colorscale=[[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0)"]],
            marker_line_color="#111", marker_line_width=1.6,
            hovertext=tracked["name"], hoverinfo="text",
        ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0), height=720, autosize=True,
        paper_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(bgcolor="white", bordercolor="#d0d5da", font_size=12),
    )
    fig.update_geos(scope="usa", projection_type="albers usa",
                    showlakes=True, lakecolor="#eef3f6", landcolor="#f6f7f5",
                    bgcolor="rgba(0,0,0,0)", subunitcolor="rgba(255,255,255,0.75)",
                    showsubunits=True, showcountries=False, framewidth=0)

    chart = pio.to_html(fig, include_plotlyjs=False, full_html=False, div_id="usmap",
                        config={"displayModeBar": False, "responsive": True})

    coverage = {
        "home_value": int(df["home_value"].notna().sum()),
        "rent": int(df["rent"].notna().sum()),
        "income": int(df["median_income"].notna().sum()),
        "total": len(df),
    }
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html"]))
    html = env.get_template("map.html.j2").render(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        chart=chart, coverage=coverage,
        tracked=sorted(tracked["name"].tolist()),
        metrics=[{"key": m["key"], "label": m["label"]} for m in METRICS],
        metric_data_json=json.dumps(metric_data, separators=(",", ":")),
    )
    dest = config.docs_dir / "map.html"
    dest.write_text(html, encoding="utf-8")
    assets = config.docs_dir / "assets" / "plotly.min.js"
    if not assets.exists():
        from plotly.offline import get_plotlyjs
        assets.parent.mkdir(parents=True, exist_ok=True)
        assets.write_text(get_plotlyjs(), encoding="utf-8")
    log.info("[map] wrote %s (%d counties)", dest, len(df))
    return dest
