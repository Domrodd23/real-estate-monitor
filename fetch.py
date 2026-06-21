#!/usr/bin/env python3
"""fetch.py — download and cache raw public housing data to data/raw/.

Each source is added in its own phase:
  * phase 2: Zillow (ZHVI, ZORI, metro inventory/new listings)
  * phase 3: Redfin, FRED, Census

Run standalone for debugging:
    python fetch.py            # fetch everything implemented so far
    python fetch.py --source zillow
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the src/ package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from remon.config import load_config  # noqa: E402
from remon.logging_setup import get_logger  # noqa: E402
from remon.sources import census, fred, redfin, zillow  # noqa: E402

log = get_logger("fetch")


def fetch_all(source: str = "all") -> None:
    config = load_config()
    log.info("fetch.py starting (source=%s)", source)
    log.info(
        "Tracking %d ZIPs across %d markets.",
        len(config.all_zips()), len(config.markets),
    )

    if source in ("all", "zillow"):
        paths = zillow.fetch_zillow(config)
        zillow.coverage_report(config, paths)

    if source in ("all", "redfin"):
        paths = redfin.fetch_redfin(config)
        redfin.summary(config, paths)

    if source in ("all", "fred"):
        paths = fred.fetch_fred(config)
        fred.summary(config, paths)

    if source in ("all", "census"):
        paths = census.fetch_census(config)
        census.summary(config, paths)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch raw housing data.")
    parser.add_argument(
        "--source",
        default="all",
        choices=["all", "zillow", "redfin", "fred", "census"],
        help="Which source to fetch (default: all).",
    )
    args = parser.parse_args()
    fetch_all(args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
