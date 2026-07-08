"""Realtor.com Economic Research: Inventory Core Metrics (ZIP + county).

The guru "market heat" trio — median days on market, % of listings with price
cuts, active-inventory change — published monthly as open CSVs on Realtor.com's
public S3 bucket. IMPORTANT: fetch the S3 URLs directly; the research *page*
(realtor.com/research/data) blocks datacenter IPs, the bucket does not.

Attribution requirement: "Housing data: Realtor.com® Economic Research".
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from ..config import Config
from ..http import DownloadError, cache_path, download, find_cached, last_cached
from ..logging_setup import get_logger
from ..validate import require_columns, validate_frame

log = get_logger("remon.realtor")

URLS = {
    "realtor_core_zip":
        "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/"
        "RDC_Inventory_Core_Metrics_Zip.csv",
    "realtor_core_county":
        "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/"
        "RDC_Inventory_Core_Metrics_County.csv",
}

# Below this many active listings a ZIP's or county's medians are small-sample noise.
MIN_ACTIVE_LISTINGS = 10


def fetch_realtor(config: Config) -> Dict[str, Optional[Path]]:
    raw_dir = config.raw_dir
    out: Dict[str, Optional[Path]] = {}
    for name, url in URLS.items():
        fresh = find_cached(raw_dir, name, "csv", config.max_age_days)
        if fresh:
            log.info("[%s] reusing fresh cache: %s", name, fresh.name)
            out[name] = fresh
            continue
        try:
            dest = cache_path(raw_dir, name, "csv")
            download(url, dest)
            log.info("[%s] cached %s (%.1f MB)", name, dest.name,
                     dest.stat().st_size / 1e6)
            out[name] = dest
        except (DownloadError, OSError) as exc:
            log.error("[%s] fetch failed: %s", name, exc)
            stale = last_cached(raw_dir, name, "csv")
            if stale:
                log.warning("[%s] using STALE cache: %s", name, stale.name)
            out[name] = stale
    return out


def _load(path: Path, key_col: str, source: str) -> pd.DataFrame:
    """Load a core-metrics CSV keyed by 5-digit code, metrics as columns.

    Values: days_on_market (days), price_cut_share (%), inventory_yoy (%),
    listings (count, for tooltips/masking).
    """
    df = pd.read_csv(path, dtype={key_col: str}, low_memory=False)
    require_columns(df, [key_col, "median_days_on_market", "price_reduced_share",
                         "active_listing_count", "active_listing_count_yy"], source)
    validate_frame(df, source, min_rows=100)
    # The file's last line is a disclaimer row ("quality_flag ...") — key is NaN.
    df = df[df[key_col].notna()].copy()
    df["key"] = df[key_col].astype(str).str.strip().str.zfill(5)
    df = df.drop_duplicates("key").set_index("key")
    out = pd.DataFrame(index=df.index)
    out["days_on_market"] = pd.to_numeric(df["median_days_on_market"], errors="coerce").round(0)
    # Shares/deltas are stored as decimal fractions (0.0219 = 2.19%).
    out["price_cut_share"] = (pd.to_numeric(df["price_reduced_share"], errors="coerce") * 100).round(1)
    out["inventory_yoy"] = (pd.to_numeric(df["active_listing_count_yy"], errors="coerce") * 100).round(1)
    out["listings"] = pd.to_numeric(df["active_listing_count"], errors="coerce")
    return out


def load_zip_metrics(config: Config) -> Optional[pd.DataFrame]:
    """ZIP-level heat metrics, small-sample ZIPs masked (listings kept for tooltips)."""
    path = last_cached(config.raw_dir, "realtor_core_zip", "csv")
    if not path:
        return None
    df = _load(path, "postal_code", "Realtor.com core ZIP")
    thin = df["listings"].isna() | (df["listings"] < MIN_ACTIVE_LISTINGS)
    df.loc[thin, ["days_on_market", "price_cut_share", "inventory_yoy"]] = pd.NA
    log.info("[realtor] ZIP metrics: %d ZIPs (%d masked as thin)", len(df), int(thin.sum()))
    return df


def load_county_metrics(config: Config) -> Optional[pd.DataFrame]:
    """County heat metrics, small-sample counties masked exactly like ZIPs."""
    path = last_cached(config.raw_dir, "realtor_core_county", "csv")
    if not path:
        return None
    df = _load(path, "county_fips", "Realtor.com core county")
    thin = df["listings"].isna() | (df["listings"] < MIN_ACTIVE_LISTINGS)
    df.loc[thin, ["days_on_market", "price_cut_share", "inventory_yoy"]] = pd.NA
    log.info("[realtor] county metrics: %d counties (%d masked as thin)",
             len(df), int(thin.sum()))
    return df
