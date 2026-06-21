#!/usr/bin/env python3
"""build.py — render metrics.csv into the static dashboard at docs/index.html.

Reads data/processed/metrics.csv, renders charts + tables with jinja2/plotly,
and writes the single static page GitHub Pages serves. Also writes the flat CSV
export. Implemented in phase 5. Run standalone for debugging:
    python build.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from remon.config import load_config  # noqa: E402
from remon.logging_setup import get_logger  # noqa: E402
from remon import dashboard  # noqa: E402

log = get_logger("build")


def build_all() -> None:
    config = load_config()
    log.info("build.py starting")
    index = dashboard.render_dashboard(config)
    log.info("Open the dashboard: %s", index)


def main() -> int:
    build_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
