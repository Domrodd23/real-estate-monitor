"""National county map explorer (docs/map.html).

Renders an interactive US county choropleth from county-level public data
(Zillow ZHVI/ZORI by county, Census ACS by county). A dropdown switches the
displayed metric entirely client-side. The user's tracked counties are outlined.

This is a separate page; the core dashboard is unchanged. Built best-effort:
build.py logs a warning and continues if the national data isn't available.
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


# Smooth heat ramps (ColorBrewer Spectral / RdYlGn). Color STOPS sit at data
# quantiles, so the gradient is both smooth AND evenly spread — no flat blocks,
# no one-shade map. This is how Zillow/Reventure heat maps read.
PALETTE_SEQ = ["#3288bd", "#66c2a5", "#abdda4", "#e6f598", "#fee08b", "#fc8d59", "#d53e4f"]
PALETTE_DIV = ["#d73027", "#f46d43", "#fdae61", "#fee08b", "#d9ef8b", "#a6d96a", "#1a9850"]


def _metric_render(df: pd.DataFrame, m: dict) -> dict:
    """Smooth quantile-stretched gradient: MapLibre interpolate + plotly colorscale."""
    vals = pd.to_numeric(df[m["key"]], errors="coerce").dropna()
    colors = PALETTE_DIV if m.get("diverging") else PALETTE_SEQ
    n = len(colors)
    # Color stops at evenly-spaced quantiles (pulled slightly off the extremes so
    # outliers don't hog the ramp). Smooth interpolation between them.
    stops = []
    for i in range(n):
        q = 0.02 + 0.96 * (i / (n - 1))
        s = float(vals.quantile(q)) if len(vals) else float(i)
        if stops and s <= stops[-1]:
            s = stops[-1] + 1e-6 * (abs(stops[-1]) + 1.0)
        stops.append(s)

    interp = ["interpolate", ["linear"], ["get", m["key"]]]
    for s, c in zip(stops, colors):
        interp += [s, c]
    expr = ["case", ["has", m["key"]], interp, "#e8e8e8"]

    zmin, zmax = stops[0], stops[-1]
    span = (zmax - zmin) or 1.0
    colorscale = [[max(0.0, min(1.0, (s - zmin) / span)), c] for s, c in zip(stops, colors)]
    legend = {"colors": colors, "lo": _fmt(m["fmt"], stops[0]),
              "mid": _fmt(m["fmt"], stops[n // 2]), "hi": _fmt(m["fmt"], stops[-1])}
    return {"key": m["key"], "label": m["label"], "fmt": m["fmt"],
            "expr": expr, "colorscale": colorscale, "zmin": zmin, "zmax": zmax,
            "legend": legend}


# ZIP-level drill-down boundaries (OpenDataDE), per state the user tracks.
ZIP_GEOJSON_BASE = "https://raw.githubusercontent.com/OpenDataDE/State-zip-code-GeoJSON/master/"
STATE_ZIP_FILES = {
    "OH": "oh_ohio_zip_codes_geo.min.json",
    "CA": "ca_california_zip_codes_geo.min.json",
    "MI": "mi_michigan_zip_codes_geo.min.json",
    "TX": "tx_texas_zip_codes_geo.min.json",
    "FL": "fl_florida_zip_codes_geo.min.json",
    "NY": "ny_new_york_zip_codes_geo.min.json",
}
ZIP_SIMPLIFY_TOLERANCE = 0.003  # ~300 m; shapes stay recognizable, file stays small
ZIP_METRIC_KEYS = ["home_value", "home_value_12m_pct", "rent", "price_to_rent"]


def _zip_values(config: Config, zips):
    """Latest home value (+12-mo), rent, price-to-rent per ZIP from the national files."""
    from . import metrics
    raw = config.raw_dir
    out, zhvi, zd, zori, zod = {}, None, None, None, None
    zpath = last_cached(raw, "zillow_zhvi_zip", "csv")
    if zpath:
        zhvi, zd = zillow.load_zip_series(zpath, zips, "Zillow ZHVI zip")
        zhvi = zhvi.set_index("RegionName"); zhvi = zhvi[~zhvi.index.duplicated()]
        for z in zhvi.index:
            res = metrics.latest_and_yoy(zhvi.loc[z], zd)
            if res:
                _, val, yoy = res
                out.setdefault(z, {})["home_value"] = round(val)
                if yoy is not None:
                    out[z]["home_value_12m_pct"] = round(yoy, 1)
    rpath = last_cached(raw, "zillow_zori_zip", "csv")
    if rpath:
        zori, zod = zillow.load_zip_series(rpath, zips, "Zillow ZORI zip")
        zori = zori.set_index("RegionName"); zori = zori[~zori.index.duplicated()]
        for z in zori.index:
            res = metrics.latest_and_yoy(zori.loc[z], zod)
            if res:
                out.setdefault(z, {})["rent"] = round(res[1])
        if zhvi is not None:
            for z in list(out):
                if z in zhvi.index and z in zori.index:
                    pr = metrics.monthly_price_to_rent(zhvi.loc[z], zori.loc[z], zd, zod)
                    if len(pr) >= 24:
                        out[z]["price_to_rent"] = round(float(pr.iloc[-1]), 1)
    return out


def build_zip_layer(config: Config, state_abbr: str):
    """Fetch + simplify a state's ZIP boundaries, merge values, write docs/zips_xx.json."""
    try:
        from shapely.geometry import mapping, shape
    except ImportError:
        log.warning("[map] shapely not installed — ZIP drill-down skipped")
        return None
    fname = STATE_ZIP_FILES.get(state_abbr.upper())
    if not fname:
        log.warning("[map] no ZIP boundary source mapped for state %s — skipping", state_abbr)
        return None

    raw = config.raw_dir
    cache_name = f"zips_raw_{state_abbr.lower()}"
    path = last_cached(raw, cache_name, "json")
    if not path:
        log.info("[map] downloading %s ZIP boundaries (one-time, large)", state_abbr)
        path = cache_path(raw, cache_name, "json")
        path.write_text(get_text(ZIP_GEOJSON_BASE + fname), encoding="utf-8")
    g = json.loads(path.read_text(encoding="utf-8"))

    zips = [f["properties"]["ZCTA5CE10"] for f in g["features"]]
    values = _zip_values(config, zips)

    feats = []
    for f in g["features"]:
        z = f["properties"]["ZCTA5CE10"]
        geom = shape(f["geometry"]).simplify(ZIP_SIMPLIFY_TOLERANCE, preserve_topology=True)
        if geom.is_empty:
            continue
        props = {"cname": "ZIP " + z}
        for k in ZIP_METRIC_KEYS:
            if values.get(z, {}).get(k) is not None:
                props[k] = values[z][k]
        feats.append({"type": "Feature", "id": z, "properties": props, "geometry": mapping(geom)})

    dest = config.docs_dir / f"zips_{state_abbr.lower()}.json"
    dest.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                               separators=(",", ":")), encoding="utf-8")
    log.info("[map] wrote %s (%d ZIPs, %.1f MB)", dest.name, len(feats), dest.stat().st_size / 1e6)
    return {"code": state_abbr.lower(), "file": dest.name, "count": len(feats)}


def _find_tippecanoe() -> Optional[str]:
    p = shutil.which("tippecanoe")
    if p:
        return p
    fallback = Path.home() / ".recmon-tools" / "tippecanoe" / "tippecanoe"
    return str(fallback) if fallback.exists() else None


def _generate_tiles(config: Config, geojson: dict, zip_states: list) -> bool:
    """Bake county + ZIP vector tiles (PMTiles) with tippecanoe. Best-effort:
    if tippecanoe is absent (e.g. a machine without it), keep the committed tiles."""
    tip = _find_tippecanoe()
    if not tip:
        log.warning("[map] tippecanoe not found — keeping existing PMTiles "
                    "(install/build tippecanoe to regenerate)")
        return False
    build = config.raw_dir / "tilebuild"
    build.mkdir(parents=True, exist_ok=True)
    docs = config.docs_dir

    cpath = build / "counties_props.geojson"
    cpath.write_text(json.dumps(geojson, separators=(",", ":")), encoding="utf-8")
    subprocess.run([tip, "-o", str(docs / "counties.pmtiles"), "-Z0", "-z9",
                    "-l", "counties", "-r1", "--no-tile-size-limit", "--force", str(cpath)],
                   check=True, capture_output=True)
    log.info("[map] baked counties.pmtiles")

    if zip_states:
        allz = {"type": "FeatureCollection", "features": []}
        for st in zip_states:
            zf = docs / st["file"]
            if zf.exists():
                allz["features"] += json.loads(zf.read_text(encoding="utf-8"))["features"]
        zpath = build / "zips_all.geojson"
        zpath.write_text(json.dumps(allz, separators=(",", ":")), encoding="utf-8")
        subprocess.run([tip, "-o", str(docs / "zips.pmtiles"), "-Z5", "-z12",
                        "-l", "zips", "-r1", "--no-tile-size-limit", "--force", str(zpath)],
                       check=True, capture_output=True)
        log.info("[map] baked zips.pmtiles (%d ZIPs)", len(allz["features"]))
    return True


def render_map_page(config: Config) -> Path:
    df = build_county_table(config)
    geojson = _load_geojson(config)

    # Build ZIP drill-down layers for each state the user tracks.
    zip_states, seen = [], set()
    for mk in config.markets.values():
        code = mk.county.state_abbr.upper()
        if code in seen:
            continue
        seen.add(code)
        try:
            info = build_zip_layer(config, code)
            if info:
                zip_states.append(info)
        except Exception as exc:  # noqa: BLE001
            log.warning("[map] ZIP layer for %s failed: %s", code, exc)

    tracked_fips = {mk.county.fips for mk in config.markets.values()}
    name_by_fips = df["name"].to_dict()
    keys = [m["key"] for m in METRICS]
    # Merge per-county values + name into each feature's properties (both the
    # MapLibre tile map and the plotly fallback read straight from these).
    for feat in geojson.get("features", []):
        fips = feat.get("id")
        props = {"cname": name_by_fips.get(fips, ""), "tracked": fips in tracked_fips}
        if fips in df.index:
            row = df.loc[fips]
            for k in keys:
                v = row.get(k)
                if pd.notna(v):
                    props[k] = float(v)
        feat["properties"] = props

    metrics_cfg = [_metric_render(df, m) for m in METRICS]
    coverage = {
        "home_value": int(df["home_value"].notna().sum()),
        "rent": int(df["rent"].notna().sum()),
        "income": int(df["median_income"].notna().sum()),
        "total": len(df),
    }

    # Bake the streaming vector tiles (best-effort; keeps committed tiles if no tippecanoe).
    try:
        _generate_tiles(config, geojson, zip_states)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] tile generation failed (%s) — keeping existing PMTiles", exc)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html"]))
    html = env.get_template("map.html.j2").render(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        coverage=coverage, tracked=sorted(df[df["is_tracked"]]["name"].tolist()),
        metrics=[{"key": m["key"], "label": m["label"]} for m in METRICS],
        metrics_json=json.dumps(metrics_cfg, separators=(",", ":")),
        zip_states=zip_states,
    )
    dest = config.docs_dir / "map.html"
    dest.write_text(html, encoding="utf-8")
    log.info("[map] wrote %s (PMTiles vector map)", dest)
    return dest
