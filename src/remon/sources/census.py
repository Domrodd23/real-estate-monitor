"""Census source: ACS county income + population, and county-to-county migration.

Free API key read from env CENSUS_API_KEY. County-level only (the reliable
geography for these series). Migration flows are county-level, annual, and lagged
— a best-effort series that degrades gracefully if a year is unavailable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from ..config import Config, get_api_key
from ..http import DownloadError, cache_path, find_cached, get_json, last_cached
from ..logging_setup import get_logger
from ..validate import validate_frame

log = get_logger("remon.census")

# Census jams missing values with large negatives; treat those as NaN.
JAM_THRESHOLD = -1_000_000


def _clean(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    return s.where(s > JAM_THRESHOLD)


def fetch_census(config: Config) -> Dict[str, Optional[Path]]:
    raw_dir = config.raw_dir
    key = get_api_key("census")
    if not key:
        log.error("CENSUS_API_KEY not set — skipping Census.")
        return {"census_acs": None, "census_migration": None}
    return {
        "census_acs": _fetch_acs(config, key, raw_dir),
        "census_migration": _fetch_migration(config, key, raw_dir),
    }


def _fetch_acs(config: Config, key: str, raw_dir: Path) -> Optional[Path]:
    cache_name = "census_acs"
    fresh = find_cached(raw_dir, cache_name, "csv", config.max_age_days)
    if fresh:
        log.info("[census_acs] reusing fresh cache: %s", fresh.name)
        return fresh

    src = config.sources["census"]
    base, ds, yr = src["api_base"], src["acs_dataset"], src["acs_year"]
    variables: Dict[str, str] = src["variables"]
    get_vars = ",".join(["NAME"] + list(variables.values()))

    rows = []
    try:
        for market in config.markets.values():
            c = market.county
            data = get_json(
                f"{base}/{yr}/{ds}",
                params={
                    "get": get_vars,
                    "for": f"county:{c.county_fips}",
                    "in": f"state:{c.state_fips}",
                    "key": key,
                },
            )
            hdr, *recs = data
            for rec in recs:
                d = dict(zip(hdr, rec))
                row = {"county_fips": c.fips, "name": d["NAME"], "year": yr}
                for logical, code in variables.items():
                    row[logical] = d.get(code)
                rows.append(row)
        df = pd.DataFrame(rows)
        for logical in variables:
            df[logical] = _clean(df[logical])
        validate_frame(df, "Census ACS", required_columns=["county_fips", "name"],
                       metric_columns=list(variables.keys()))
        dest = cache_path(raw_dir, cache_name, "csv")
        df.to_csv(dest, index=False)
        log.info("[census_acs] %d counties cached (ACS %s)", len(df), yr)
        return dest
    except (DownloadError, KeyError, ValueError) as exc:
        log.error("[census_acs] fetch failed: %s", exc)
        stale = last_cached(raw_dir, cache_name, "csv")
        if stale:
            log.warning("[census_acs] using STALE cache: %s", stale.name)
        return stale


def _fetch_migration(config: Config, key: str, raw_dir: Path) -> Optional[Path]:
    cache_name = "census_migration"
    fresh = find_cached(raw_dir, cache_name, "csv", config.max_age_days)
    if fresh:
        log.info("[census_migration] reusing fresh cache: %s", fresh.name)
        return fresh

    src = config.sources["census"]
    base = src["api_base"]
    mig = src.get("migration", {})
    ds, yr = mig.get("dataset", "acs/flows"), mig.get("year")
    if not yr:
        log.warning("[census_migration] no migration year configured — skipping.")
        return None

    rows = []
    for market in config.markets.values():
        c = market.county
        try:
            data = get_json(
                f"{base}/{yr}/{ds}",
                params={
                    "get": "MOVEDIN,MOVEDOUT,MOVEDNET,FULL1_NAME",
                    "for": f"county:{c.county_fips}",
                    "in": f"state:{c.state_fips}",
                    "key": key,
                },
            )
            hdr, *recs = data
            sub = pd.DataFrame([dict(zip(hdr, r)) for r in recs])
            for col in ("MOVEDIN", "MOVEDOUT", "MOVEDNET"):
                sub[col] = _clean(sub[col])
            # MOVEDNET is populated only for county<->county pairs; state/abroad
            # origins carry MOVEDIN only. Net = sum of those county-pair nets.
            net_pairs = int(sub["MOVEDNET"].notna().sum())
            rows.append({
                "county_fips": c.fips,
                "name": c.name,
                "year": yr,
                # gross in-migration from all origins (states, counties, abroad)
                "gross_in": int(sub["MOVEDIN"].sum(skipna=True)),
                # net domestic county-to-county migration
                "net_migration": int(sub["MOVEDNET"].sum(skipna=True)),
                "net_county_pairs": net_pairs,
            })
            if net_pairs == 0:
                log.warning(
                    "[census_migration] %s: no county-to-county net detail for "
                    "year %s — net migration not meaningful for this year.",
                    c.name, yr,
                )
        except (DownloadError, KeyError, ValueError) as exc:
            log.error("[census_migration] %s failed: %s", c.name, exc)

    if not rows:
        log.warning("[census_migration] no migration data obtained.")
        stale = last_cached(raw_dir, cache_name, "csv")
        return stale

    df = pd.DataFrame(rows)
    dest = cache_path(raw_dir, cache_name, "csv")
    df.to_csv(dest, index=False)
    log.info("[census_migration] %d counties cached (flows %s)", len(df), yr)
    return dest


def summary(config: Config, paths: Dict[str, Optional[Path]]) -> None:
    print("\n" + "=" * 70)
    print("Census summary — county income, population, net migration")
    print("=" * 70)

    acs_path = paths.get("census_acs")
    if acs_path:
        acs = pd.read_csv(acs_path)
        for _, r in acs.iterrows():
            inc = r.get("median_household_income")
            pop = r.get("population")
            inc_s = f"${int(inc):,}" if pd.notna(inc) else "n/a"
            pop_s = f"{int(pop):,}" if pd.notna(pop) else "n/a"
            print(f"  {r['name']}: median income {inc_s}, population {pop_s} "
                  f"(ACS {int(r['year'])})")
    else:
        print("  ACS income/population: NO DATA")

    mig_path = paths.get("census_migration")
    if mig_path:
        mig = pd.read_csv(mig_path)
        for _, r in mig.iterrows():
            net = int(r["net_migration"])
            direction = "net INFLOW" if net > 0 else "net OUTFLOW"
            print(f"  {r['name']}: {direction} {net:+,} domestic "
                  f"(gross in-migration {int(r['gross_in']):,}, "
                  f"county-to-county flows {int(r['year'])})")
    else:
        print("  Migration: NO DATA (county-level, lagged source)")
    print("=" * 70)
