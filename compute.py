#!/usr/bin/env python3
"""compute.py — read cached raw files, filter to my markets, compute metrics.

Writes data/processed/metrics.csv. Every metric row carries the source name and
the as-of date it came from (hard constraint #6: no invented numbers).

Implemented in phase 4. Run standalone for debugging:
    python compute.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import json  # noqa: E402

from remon.config import load_config  # noqa: E402
from remon.logging_setup import get_logger  # noqa: E402
from remon import forecast, metrics  # noqa: E402

log = get_logger("compute")


def compute_all() -> None:
    config = load_config()
    log.info("compute.py starting")
    df = metrics.build_metrics(config)

    dest = config.processed_dir / "metrics.csv"
    df.to_csv(dest, index=False)
    log.info("Wrote %d metric rows -> %s", len(df), dest)

    # Forecasts (two backtested public-data models).
    fc = forecast.build_forecasts(config)
    (config.processed_dir / "forecasts.json").write_text(json.dumps(fc, indent=2))
    log.info("Wrote forecasts -> %s (%d ZIPs)",
             config.processed_dir / "forecasts.json", len(fc["zips"]))

    metrics.print_table(df)


def main() -> int:
    compute_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
