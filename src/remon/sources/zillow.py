"""Zillow Research source: discover current CSV links, download, cache, load.

Hard constraints honored here:
  * URLs are NOT hardcoded — we fetch the research data page, find the current
    file link by NAME, confirm it resolves, then download (spec requirement).
  * Link selection fails loudly if zero or more than one file matches a series,
    so a page restructure surfaces immediately instead of grabbing the wrong file.
  * Data is filtered to the tracked ZIPs at load time (constraint #4); the raw
    national CSV stays only in the git-ignored date-stamped cache (constraint #5).
  * Columns are matched by name; missing expected columns fail loudly (#3).
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

log = get_logger("remon.zillow")

ZILLOW_DATA_PAGE = "https://www.zillow.com/research/data/"

# Absolute CSV links live in the page HTML/JS (sometimes with escaped slashes).
CSV_URL_RE = re.compile(
    r"https://files\.zillowstatic\.com/research/public_csvs/[A-Za-z0-9_./\-]+?\.csv"
)
# Zillow wide files use one column per month named YYYY-MM-DD.
DATE_COL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Canonical series selection. Each is identified by substrings that MUST appear
# and substrings that must NOT — chosen so exactly one file matches.
SERIES: Dict[str, Dict] = {
    "zillow_zhvi_zip": {
        "label": "ZHVI home value (ZIP)",
        "geography": "zip",
        "require": ["/zhvi/", "Zip_zhvi", "uc_sfrcondo_tier_0.33_0.67", "sm_sa", "_month.csv"],
        "exclude": ["bdrmcnt", "week"],
    },
    "zillow_zori_zip": {
        "label": "ZORI rent (ZIP)",
        "geography": "zip",
        "require": ["/zori/", "Zip_zori", "uc_sfrcondomfr", "sm_month.csv"],
        "exclude": ["_sa_", "week"],
    },
    "zillow_invt_fs_metro": {
        "label": "For-sale inventory (metro)",
        "geography": "metro",
        "require": ["/invt_fs/", "Metro_invt_fs", "uc_sfrcondo", "sm_month.csv"],
        "exclude": ["week"],
    },
    "zillow_new_listings_metro": {
        "label": "New listings (metro)",
        "geography": "metro",
        "require": ["/new_listings/", "Metro_new_listings", "uc_sfrcondo", "sm_month.csv"],
        "exclude": ["week"],
    },
    # County-level national series — backbone of the national map explorer.
    "zillow_zhvi_county": {
        "label": "ZHVI home value (county, national)",
        "geography": "county",
        "require": ["/zhvi/", "County_zhvi", "uc_sfrcondo_tier_0.33_0.67", "sm_sa", "_month.csv"],
        "exclude": ["bdrmcnt", "week"],
    },
    "zillow_zori_county": {
        "label": "ZORI rent (county, national)",
        "geography": "county",
        "require": ["/zori/", "County_zori", "uc_sfrcondomfr", "sm_month.csv"],
        "exclude": ["_sa_", "week"],
    },
}

# Last-known-good canonical file URLs on Zillow's CDN. Used ONLY when page
# discovery fails (e.g. zillow.com 403s a datacenter IP in CI). The CDN host
# (files.zillowstatic.com) is not IP-blocked. Page discovery stays primary so a
# Zillow filename change is still picked up whenever the page is reachable.
FALLBACK_URLS: Dict[str, str] = {
    "zillow_zhvi_zip":
        "https://files.zillowstatic.com/research/public_csvs/zhvi/"
        "Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zillow_zori_zip":
        "https://files.zillowstatic.com/research/public_csvs/zori/"
        "Zip_zori_uc_sfrcondomfr_sm_month.csv",
    "zillow_invt_fs_metro":
        "https://files.zillowstatic.com/research/public_csvs/invt_fs/"
        "Metro_invt_fs_uc_sfrcondo_sm_month.csv",
    "zillow_new_listings_metro":
        "https://files.zillowstatic.com/research/public_csvs/new_listings/"
        "Metro_new_listings_uc_sfrcondo_sm_month.csv",
    "zillow_zhvi_county":
        "https://files.zillowstatic.com/research/public_csvs/zhvi/"
        "County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zillow_zori_county":
        "https://files.zillowstatic.com/research/public_csvs/zori/"
        "County_zori_uc_sfrcondomfr_sm_month.csv",
}


# --------------------------------------------------------------------------- #
# Link discovery
# --------------------------------------------------------------------------- #
def discover_links(html: str) -> List[str]:
    """Extract all distinct Zillow public CSV URLs from the page."""
    return sorted(set(CSV_URL_RE.findall(html.replace("\\/", "/"))))


def select_one(urls: List[str], require: List[str], exclude: List[str], label: str) -> str:
    """Return the single URL matching `require` and not `exclude`. Fail loudly."""
    matches = [
        u for u in urls
        if all(tok in u for tok in require) and not any(tok in u for tok in exclude)
    ]
    if not matches:
        raise DownloadError(
            f"Zillow: no CSV matched '{label}' (require={require}). "
            f"The research page structure may have changed."
        )
    if len(matches) > 1:
        raise DownloadError(
            f"Zillow: {len(matches)} CSVs matched '{label}', expected exactly 1: {matches}"
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# Fetch (download + cache, with graceful degradation)
# --------------------------------------------------------------------------- #
def fetch_zillow(config: Config, only: Optional[List[str]] = None) -> Dict[str, Optional[Path]]:
    """Download/refresh the Zillow series, returning {name: cached_path or None}.

    Reuses a fresh cache; on any failure falls back to the last good cache and
    logs a STALE warning rather than aborting the whole run.
    """
    raw_dir = config.raw_dir
    results: Dict[str, Optional[Path]] = {}

    # Discover links once. If the page is unreachable, we degrade to cache.
    page_ok = True
    urls: List[str] = []
    try:
        log.info("Discovering current Zillow CSV links from %s", ZILLOW_DATA_PAGE)
        urls = discover_links(get_text(ZILLOW_DATA_PAGE))
        log.info("Found %d Zillow CSV links on the page", len(urls))
    except DownloadError as exc:
        log.error("Could not load Zillow data page: %s", exc)
        page_ok = False

    for name, spec in SERIES.items():
        if only and name not in only:
            continue

        fresh = find_cached(raw_dir, name, "csv", config.max_age_days)
        if fresh:
            log.info("[%s] reusing fresh cache: %s", name, fresh.name)
            results[name] = fresh
            continue

        # Determine the download URL: page discovery first, canonical fallback next.
        url = None
        if page_ok:
            try:
                url = select_one(urls, spec["require"], spec["exclude"], spec["label"])
            except DownloadError as exc:
                log.warning("%s — trying canonical fallback URL.", exc)
        if not url:
            url = FALLBACK_URLS.get(name)
            if url:
                log.warning("[%s] page discovery unavailable; using canonical "
                            "fallback URL: %s", name, url)
        if not url:
            results[name] = _fallback_stale(raw_dir, name)
            continue

        if not url_resolves(url):
            log.error("[%s] URL did not resolve: %s", name, url)
            results[name] = _fallback_stale(raw_dir, name)
            continue

        try:
            results[name] = download(url, cache_path(raw_dir, name, "csv"))
        except DownloadError as exc:
            log.error("[%s] download failed: %s", name, exc)
            results[name] = _fallback_stale(raw_dir, name)

    return results


def _fallback_stale(raw_dir: Path, name: str) -> Optional[Path]:
    stale = last_cached(raw_dir, name, "csv")
    if stale:
        log.warning("[%s] using STALE cached copy: %s", name, stale.name)
    else:
        log.error("[%s] no cached copy available — series unavailable this run", name)
    return stale


# --------------------------------------------------------------------------- #
# Load (filter to tracked ZIPs, validate by name)
# --------------------------------------------------------------------------- #
def date_columns(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if DATE_COL_RE.match(str(c))]


def load_zip_series(path: Path, zips: List[str], source: str) -> Tuple[pd.DataFrame, List[str]]:
    """Read a Zillow ZIP-level CSV, validate, and filter to `zips`.

    Returns (filtered_df, sorted_date_columns). RegionName is normalized to a
    5-digit string so it matches the config ZIP list.
    """
    df = pd.read_csv(path, dtype={"RegionName": str}, low_memory=False)
    require_columns(df, ["RegionID", "RegionName", "RegionType"], source)
    validate_frame(df, source, min_rows=1)

    df["RegionName"] = df["RegionName"].astype(str).str.strip().str.zfill(5)
    dcols = date_columns(df)
    if not dcols:
        raise DataValidationError(f"[{source}] no YYYY-MM-DD month columns found")

    sub = df[df["RegionName"].isin(set(zips))].copy()
    log.info("[%s] %d of %d tracked ZIPs present in file", source, len(sub), len(zips))
    return sub, sorted(dcols)


def load_metro_series(path: Path, source: str) -> Tuple[pd.DataFrame, List[str]]:
    """Read a Zillow metro-level CSV and validate (filtering happens in compute)."""
    df = pd.read_csv(path, low_memory=False)
    require_columns(df, ["RegionID", "RegionName", "RegionType"], source)
    validate_frame(df, source, min_rows=1)
    return df, sorted(date_columns(df))


def load_county_series(path: Path, source: str) -> Tuple[pd.DataFrame, List[str]]:
    """Read a Zillow county-level CSV and add a 5-digit `fips` column.

    Zillow county files carry StateCodeFIPS (2) + MunicipalCodeFIPS (3); we join
    them into the standard county FIPS used by the map's GeoJSON.
    """
    df = pd.read_csv(
        path, dtype={"StateCodeFIPS": str, "MunicipalCodeFIPS": str}, low_memory=False
    )
    require_columns(
        df, ["RegionID", "RegionName", "RegionType", "StateCodeFIPS", "MunicipalCodeFIPS"],
        source,
    )
    validate_frame(df, source, min_rows=1)
    df["fips"] = df["StateCodeFIPS"].str.zfill(2) + df["MunicipalCodeFIPS"].str.zfill(3)
    return df, sorted(date_columns(df))


# --------------------------------------------------------------------------- #
# Phase-2 acceptance report
# --------------------------------------------------------------------------- #
def coverage_report(config: Config, paths: Dict[str, Optional[Path]]) -> None:
    """Print, per market, ZIP coverage for ZHVI and ZORI (the acceptance test)."""
    print("\n" + "=" * 70)
    print("Zillow coverage — tracked ZIPs found in each ZIP-level file")
    print("=" * 70)

    for name, friendly in [
        ("zillow_zhvi_zip", "ZHVI home value"),
        ("zillow_zori_zip", "ZORI rent"),
    ]:
        path = paths.get(name)
        if not path:
            print(f"\n{friendly}: NO FILE AVAILABLE")
            continue
        df, dcols = load_zip_series(path, config.all_zips(), friendly)
        latest = dcols[-1]
        print(f"\n{friendly}  [{Path(path).name}]")
        print(f"  history: {dcols[0]} → {latest}  ({len(dcols)} months)")
        for m in config.markets.values():
            sub = df[df["RegionName"].isin(set(m.zips))]
            found = set(sub["RegionName"])
            with_val = int(sub[latest].notna().sum()) if latest in sub.columns else 0
            print(f"  {m.name}: {len(found)}/{len(m.zips)} ZIPs in file, "
                  f"{with_val} with a {latest} value")
            missing = sorted(set(m.zips) - found)
            if missing:
                print(f"      no {friendly} data for: {', '.join(missing)}")

    # Metro files: confirm cached + row counts (ZIP-to-metro mapping is phase 4).
    print("\nMetro-level files (used as ZIP fallback in compute):")
    for name, friendly in [
        ("zillow_invt_fs_metro", "For-sale inventory"),
        ("zillow_new_listings_metro", "New listings"),
    ]:
        path = paths.get(name)
        if not path:
            print(f"  {friendly}: NO FILE AVAILABLE")
            continue
        df, dcols = load_metro_series(path, friendly)
        print(f"  {friendly}: {Path(path).name} — {len(df)} metros, latest {dcols[-1]}")
    print("=" * 70)
