"""BLS QCEW: county construction wages (NAICS 23) — the rehab-labor cost index.

Primary data = CSVs COMMITTED to src/remon/data/ (annual vintages). An optional
best-effort refresh hits data.bls.gov's open CSV API with a browser UA; any
failure silently falls back to the committed file.

HARD RULE: only ever fetch data.bls.gov/cew/data/... URLs. Never www.bls.gov —
its CDN 403s scripted fetchers regardless of User-Agent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from ..config import Config
from ..http import DownloadError, cache_path, download, find_cached, last_cached
from ..logging_setup import get_logger

log = get_logger("remon.bls")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
# Newest vintage first; each is indexed against its OWN national baseline.
VINTAGES = [2025, 2024]
QCEW_URL = "https://data.bls.gov/cew/data/api/{year}/a/industry/23.csv"

MIN_EMPLOYMENT = 100  # below this, a county's average wage is noise


QCEW_COLUMNS = {"area_fips", "own_code", "agglvl_code", "annual_avg_wkly_wage",
                "annual_avg_emplvl", "disclosure_code"}


def fetch_bls(config: Config) -> Dict[str, Optional[Path]]:
    """Best-effort refresh of the newest vintage. Never fails the build."""
    year = VINTAGES[0]
    name = f"qcew_naics23_{year}"
    fresh = find_cached(config.raw_dir, name, "csv", config.max_age_days)
    if fresh:
        log.info("[bls] reusing fresh cache: %s", fresh.name)
        return {name: fresh}
    try:
        dest = cache_path(config.raw_dir, name, "csv")
        download(QCEW_URL.format(year=year), dest)
        # data.bls.gov can 200 an HTML error page — never cache junk (a bad
        # cached file would shadow the good committed vintage for 35 days).
        head = dest.open(encoding="utf-8", errors="ignore").readline()
        if "area_fips" not in head:
            log.warning("[bls] refresh returned non-CSV content — discarded")
            dest.unlink()
            return {name: None}
        log.info("[bls] cached %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return {name: dest}
    except (DownloadError, OSError) as exc:
        log.warning("[bls] refresh failed (%s) — committed data file remains primary", exc)
        return {name: None}


def _vintage_frame(config: Config, year: int) -> Optional[pd.DataFrame]:
    """A vintage's QCEW frame: fresh cache first, falling back to the committed
    repo file if the cached one is unreadable/junk."""
    candidates = []
    cached = last_cached(config.raw_dir, f"qcew_naics23_{year}", "csv")
    if cached:
        candidates.append(cached)
    committed = DATA_DIR / f"qcew_naics23_{year}.csv"
    if committed.exists():
        candidates.append(committed)
    for path in candidates:
        try:
            df = pd.read_csv(path, dtype={"area_fips": str}, low_memory=False)
            missing = QCEW_COLUMNS - set(df.columns)
            if missing:
                raise ValueError("missing columns: %s" % sorted(missing))
        except Exception as exc:  # noqa: BLE001 — junk payloads raise many types
            log.warning("[bls] unreadable QCEW file %s (%s) — trying fallback",
                        path.name, exc)
            continue
        df = df[df["own_code"] == 5]  # private ownership only
        wage = pd.to_numeric(df["annual_avg_wkly_wage"], errors="coerce")
        empl = pd.to_numeric(df["annual_avg_emplvl"], errors="coerce")
        disclosed = df["disclosure_code"].astype(str).str.strip().str.upper() != "N"
        return df.assign(wage=wage.where(disclosed & (wage > 0)), empl=empl)
    return None


def load_wage_index(config: Config, fips_index, xwalk=None) -> Optional[pd.Series]:
    """Construction wage index for every FIPS in `fips_index` (1.00 = US avg).

    Fallback chain per county: newest county vintage -> prior county vintage ->
    successor FIPS via `xwalk` (post-2022 vintages key CT on planning regions) ->
    newest state vintage -> prior state vintage -> NaN. Each vintage is indexed
    to its own national baseline, so mixing vintages stays apples-to-apples.
    """
    county_layers, state_layers = [], []
    for year in VINTAGES:
        df = _vintage_frame(config, year)
        if df is None:
            log.warning("[bls] no data for vintage %d", year)
            continue
        us = df[(df["area_fips"] == "US000") & (df["agglvl_code"] == 14)]
        if us.empty or pd.isna(us["wage"].iloc[0]):
            log.warning("[bls] vintage %d missing US baseline — skipped", year)
            continue
        base = float(us["wage"].iloc[0])
        cty = df[(df["agglvl_code"] == 74) & (df["empl"] >= MIN_EMPLOYMENT)]
        cty = cty.dropna(subset=["wage"]).drop_duplicates("area_fips").set_index("area_fips")
        county_layers.append((cty["wage"] / base).round(3))
        st = df[df["agglvl_code"] == 54].dropna(subset=["wage"])
        st = st.drop_duplicates("area_fips")
        st_series = pd.Series((st["wage"] / base).round(3).values,
                              index=st["area_fips"].str[:2].values)
        state_layers.append(st_series)
    if not county_layers and not state_layers:
        return None

    counties = None
    for layer in county_layers:
        counties = layer if counties is None else counties.combine_first(layer)

    out = counties.reindex(fips_index) if counties is not None \
        else pd.Series(float("nan"), index=fips_index)
    if xwalk and counties is not None:
        # Legacy-FIPS map rows (CT/AK) pick up their successor region's value
        # BEFORE the state fallback would flatten them to the state average.
        succ = counties.reindex([xwalk.get(str(f), str(f)) for f in fips_index])
        out = out.fillna(pd.Series(succ.values, index=fips_index))
    direct = int(out.notna().sum())
    for sl in state_layers:
        fill = pd.Series([sl.get(str(f)[:2]) for f in fips_index],
                         index=fips_index, dtype=float)
        out = out.fillna(fill)
    log.info("[bls] wage index: %d/%d counties direct, %d via state fallback",
             direct, len(fips_index), int(out.notna().sum()) - direct)
    return out
