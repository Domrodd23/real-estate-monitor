"""IRS SOI county-to-county migration (the "wealth migration" dataset).

Tax-return flows between counties with the adjusted gross income the movers
carry. We use ONLY the per-county summary rows (pseudo state-FIPS 96 =
"Total Migration - US and Foreign"), never sums over the flow detail — the
detail includes 96/97/98 and 57/58/59 aggregate rows that double-count.

Vintage 2022–2023 filings (latest published; ~2-year lag). Public domain.
Bump IRS_VINTAGE when the next release drops (filename pattern countyinflowYYyy.csv).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from ..config import Config
from ..http import DownloadError, cache_path, download, find_cached, last_cached
from ..logging_setup import get_logger
from ..validate import require_columns, validate_frame

log = get_logger("remon.irs")

IRS_VINTAGE = "2223"
URLS = {
    "irs_mig_inflow": f"https://www.irs.gov/pub/irs-soi/countyinflow{IRS_VINTAGE}.csv",
    "irs_mig_outflow": f"https://www.irs.gov/pub/irs-soi/countyoutflow{IRS_VINTAGE}.csv",
}


def fetch_irs(config: Config) -> Dict[str, Optional[Path]]:
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


def _totals(path: Path, county_cols, filter_col: str, source: str) -> pd.Series:
    """AGI ($thousands) from the 'Total Migration-US and Foreign' summary rows,
    indexed by 5-digit county FIPS."""
    dtypes = {c: str for c in ("y1_statefips", "y1_countyfips",
                               "y2_statefips", "y2_countyfips")}
    df = pd.read_csv(path, dtype=dtypes, encoding="latin-1", low_memory=False)
    require_columns(df, list(dtypes) + ["agi"], source)
    validate_frame(df, source, min_rows=1000)
    tot = df[df[filter_col].str.strip() == "96"].copy()
    st, ct = county_cols
    fips = tot[st].str.strip().str.zfill(2) + tot[ct].str.strip().str.zfill(3)
    agi = pd.to_numeric(tot["agi"], errors="coerce")
    out = pd.Series(agi.values, index=fips.values)
    return out[~out.index.duplicated()]


def load_net_agi(config: Config) -> Optional[pd.Series]:
    """Net AGI carried by movers per county, in DOLLARS (inflow - outflow)."""
    ipath = last_cached(config.raw_dir, "irs_mig_inflow", "csv")
    opath = last_cached(config.raw_dir, "irs_mig_outflow", "csv")
    if not ipath or not opath:
        return None
    # Inflow file: destination county is y2_*, totals rows flagged in y1_statefips.
    agi_in = _totals(ipath, ("y2_statefips", "y2_countyfips"), "y1_statefips",
                     "IRS county inflow")
    # Outflow file: origin county is y1_*, totals rows flagged in y2_statefips.
    agi_out = _totals(opath, ("y1_statefips", "y1_countyfips"), "y2_statefips",
                      "IRS county outflow")
    # Intersection only: a county with just one published side is IRS-suppressed
    # data, not a measured zero — leave it NA rather than fabricate a net.
    both = agi_in.index.intersection(agi_out.index)
    net = (agi_in[both] - agi_out[both]) * 1000.0
    dropped = len(agi_in.index.union(agi_out.index)) - len(both)
    log.info("[irs] net AGI computed for %d counties (%d one-sided dropped)",
             len(net), dropped)
    return net
