"""Redfin Data Center source: county market tracker (price, DOM, price drops).

The Data Center page renders its download links via JavaScript and does not
expose the file URL in the served HTML, so we use Redfin's documented, stable
public S3 file — but we still confirm it resolves before downloading (spec
requirement) and attempt page discovery first in case Redfin starts exposing it.

County-level only: Redfin's free data is reliable at county/metro and thin at
ZIP, so this provides the county price-cut and days-on-market signals.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from ..config import Config
from ..http import (
    DownloadError,
    cache_path,
    download,
    find_cached,
    get_text,
    last_cached,
    url_resolves,
)
from ..logging_setup import get_logger
from ..validate import DataValidationError, require_columns, validate_frame

log = get_logger("remon.redfin")

REDFIN_DATA_PAGE = "https://www.redfin.com/news/data-center/"
# Documented stable public file (county level, gzipped TSV).
REDFIN_COUNTY_URL = (
    "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
    "redfin_market_tracker/county_market_tracker.tsv000.gz"
)
CACHE_NAME = "redfin_county_market_tracker"

# Columns we rely on, matched by NAME (Redfin reorders/extends columns over time).
REQUIRED_COLS = [
    "region", "region_type", "property_type", "period_begin", "period_end",
    "median_sale_price", "median_dom", "price_drops",
]
PROPERTY_TYPE = "All Residential"


def _discover_url(html: str) -> Optional[str]:
    hits = re.findall(r"https://[^\"' <>]*redfin-public-data[^\"' <>]*county_market_tracker[^\"' <>]*\.gz", html)
    return hits[0] if hits else None


def fetch_redfin(config: Config) -> Dict[str, Optional[Path]]:
    raw_dir = config.raw_dir
    fresh = find_cached(raw_dir, CACHE_NAME, "gz", config.max_age_days)
    if fresh:
        log.info("[redfin] reusing fresh cache: %s", fresh.name)
        return {"redfin_county": fresh}

    url = REDFIN_COUNTY_URL
    try:
        discovered = _discover_url(get_text(REDFIN_DATA_PAGE))
        if discovered:
            log.info("[redfin] discovered file link on data center page")
            url = discovered
        else:
            log.info("[redfin] page exposes no link; using documented S3 file")
    except DownloadError as exc:
        log.warning("[redfin] could not load data center page (%s); using S3 file", exc)

    if not url_resolves(url):
        log.error("[redfin] file URL did not resolve: %s", url)
        return {"redfin_county": last_cached(raw_dir, CACHE_NAME, "gz")}

    try:
        dest = download(url, cache_path(raw_dir, CACHE_NAME, "gz"))
        return {"redfin_county": dest}
    except DownloadError as exc:
        log.error("[redfin] download failed: %s", exc)
        stale = last_cached(raw_dir, CACHE_NAME, "gz")
        if stale:
            log.warning("[redfin] using STALE cache: %s", stale.name)
        return {"redfin_county": stale}


def _expected_region(config: Config) -> Dict[str, str]:
    """Map each market's county to Redfin's 'County, ST' region string."""
    out = {}
    for m in config.markets.values():
        base = m.county.name.split(",")[0].strip()  # "Lucas County"
        out[m.key] = f"{base}, {m.county.state_abbr}"  # "Lucas County, OH"
    return out


def load_counties(path: Path, config: Config) -> pd.DataFrame:
    """Load the county tracker, validate by name, filter to tracked counties.

    The national file is ~240 MB, so we read it in chunks and keep only the
    tracked counties' "All Residential" rows (constraint #4: don't hold the
    whole country). Redfin column names are normalized to lowercase so the
    loader survives Redfin's periodic upper/lower case changes.
    """
    wanted = set(_expected_region(config).values())
    kept: List[pd.DataFrame] = []
    validated = False

    for chunk in pd.read_csv(
        path, sep="\t", compression="gzip", low_memory=False, chunksize=400_000
    ):
        chunk.columns = [c.strip().lower() for c in chunk.columns]
        if not validated:
            require_columns(chunk, REQUIRED_COLS, "Redfin county tracker")
            validated = True
        mask = (
            chunk["region"].isin(wanted)
            & (chunk["property_type"] == PROPERTY_TYPE)
            & (chunk["region_type"] == "county")
        )
        if mask.any():
            kept.append(chunk[mask].copy())

    if not kept:
        raise DataValidationError(
            f"[Redfin] no rows for tracked counties {sorted(wanted)} "
            f"(property_type='{PROPERTY_TYPE}')"
        )
    sub = pd.concat(kept, ignore_index=True)
    validate_frame(sub, "Redfin county tracker", min_rows=1)
    sub["period_end"] = pd.to_datetime(sub["period_end"], errors="coerce")
    sub = sub.sort_values("period_end").reset_index(drop=True)
    log.info("[redfin] %d county-month rows for %d tracked counties",
             len(sub), sub["region"].nunique())
    return sub


def summary(config: Config, paths: Dict[str, Optional[Path]]) -> None:
    print("\n" + "=" * 70)
    print("Redfin summary — latest county figures (All Residential)")
    print("=" * 70)
    path = paths.get("redfin_county")
    if not path:
        print("  NO DATA")
        print("=" * 70)
        return
    sub = load_counties(path, config)
    for region, g in sub.groupby("region"):
        latest = g.dropna(subset=["period_end"]).iloc[-1]
        price = latest.get("median_sale_price")
        dom = latest.get("median_dom")
        drops = latest.get("price_drops")
        price_s = f"${int(price):,}" if pd.notna(price) else "n/a"
        dom_s = f"{int(dom)}d" if pd.notna(dom) else "n/a"
        drops_s = f"{drops:.0%}" if pd.notna(drops) else "n/a"
        print(f"  {region}: median sale {price_s}, days-on-market {dom_s}, "
              f"price-drop share {drops_s}  (as of {latest['period_end'].date()})")
    print("=" * 70)
