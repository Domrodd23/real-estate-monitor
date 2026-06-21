"""FRED source: download configured series via the free API, cache, summarize.

Free API key read from env FRED_API_KEY (constraint #2). Series ids come from
config.yaml so they can be changed without touching code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from ..config import Config, get_api_key
from ..http import DownloadError, cache_path, find_cached, get_json, last_cached
from ..logging_setup import get_logger
from ..validate import validate_frame

log = get_logger("remon.fred")


def fetch_fred(config: Config) -> Dict[str, Optional[Path]]:
    """Download each configured FRED series to a date-stamped CSV cache."""
    raw_dir = config.raw_dir
    base = config.sources["fred"]["api_base"]
    series: Dict[str, str] = config.sources["fred"]["series"]
    key = get_api_key("fred")
    results: Dict[str, Optional[Path]] = {}

    if not key:
        log.error("FRED_API_KEY not set — skipping all FRED series.")
        return {name: None for name in series}

    for name, series_id in series.items():
        cache_name = f"fred_{series_id}"
        fresh = find_cached(raw_dir, cache_name, "csv", config.max_age_days)
        if fresh:
            log.info("[%s] reusing fresh cache: %s", name, fresh.name)
            results[name] = fresh
            continue
        try:
            data = get_json(
                f"{base}/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": key,
                    "file_type": "json",
                },
            )
            obs = data.get("observations", [])
            if not obs:
                raise DownloadError(f"FRED returned no observations for {series_id}")
            df = pd.DataFrame(obs)[["date", "value"]]
            df["value"] = pd.to_numeric(df["value"], errors="coerce")  # "." -> NaN
            validate_frame(df, f"FRED:{series_id}", required_columns=["date", "value"])
            dest = cache_path(raw_dir, cache_name, "csv")
            df.to_csv(dest, index=False)
            log.info("[%s] %s: %d observations cached", name, series_id, len(df))
            results[name] = dest
        except (DownloadError, KeyError, ValueError) as exc:
            log.error("[%s] FRED fetch failed: %s", name, exc)
            stale = last_cached(raw_dir, cache_name, "csv")
            if stale:
                log.warning("[%s] using STALE cache: %s", name, stale.name)
            results[name] = stale

    return results


def load_series(path: Path, source: str) -> pd.DataFrame:
    """Load a cached FRED series CSV (columns: date, value), date-sorted."""
    df = pd.read_csv(path, parse_dates=["date"])
    validate_frame(df, source, required_columns=["date", "value"], metric_columns=["value"])
    return df.sort_values("date").reset_index(drop=True)


def summary(config: Config, paths: Dict[str, Optional[Path]]) -> None:
    print("\n" + "=" * 70)
    print("FRED summary — latest observation per series")
    print("=" * 70)
    series: Dict[str, str] = config.sources["fred"]["series"]
    for name, series_id in series.items():
        path = paths.get(name)
        if not path:
            print(f"  {name:<18} ({series_id}): NO DATA")
            continue
        df = load_series(path, f"FRED:{series_id}")
        latest = df.dropna(subset=["value"]).iloc[-1]
        print(f"  {name:<18} ({series_id}): {latest['value']:.2f} "
              f"as of {latest['date'].date()}  ({len(df)} obs)")
    print("=" * 70)
