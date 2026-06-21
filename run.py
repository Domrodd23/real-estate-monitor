#!/usr/bin/env python3
"""run.py — orchestrate the pipeline: fetch -> compute -> build.

This is what the scheduled GitHub Actions job runs each month.

    python run.py            # full pipeline (fetch, compute, build)
    python run.py check      # validate config + API keys, print a summary
    python run.py fetch      # just fetch
    python run.py compute    # just compute
    python run.py build      # just build
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from remon.config import load_config, summary  # noqa: E402
from remon.logging_setup import get_logger  # noqa: E402

import fetch  # noqa: E402
import compute  # noqa: E402
import build  # noqa: E402

log = get_logger("run")


def cmd_check() -> int:
    """Phase-1 acceptance: load + validate config and report status."""
    config = load_config()
    print("\n" + "=" * 70)
    print("real estate monitor — configuration check")
    print("=" * 70)
    print(summary(config))
    print("=" * 70 + "\n")
    log.info("Config OK.")
    return 0


def cmd_all() -> int:
    started = time.time()
    log.info("=== full pipeline starting ===")
    fetch.fetch_all("all")
    compute.compute_all()
    build.build_all()
    log.info("=== pipeline finished in %.1fs ===", time.time() - started)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Real estate monitor pipeline.")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "check", "fetch", "compute", "build"],
        help="Step to run (default: all).",
    )
    args = parser.parse_args()

    dispatch = {
        "check": cmd_check,
        "all": cmd_all,
        "fetch": lambda: fetch.fetch_all("all") or 0,
        "compute": lambda: compute.compute_all() or 0,
        "build": lambda: build.build_all() or 0,
    }
    return dispatch[args.command]() or 0


if __name__ == "__main__":
    raise SystemExit(main())
