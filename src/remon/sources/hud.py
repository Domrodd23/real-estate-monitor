"""HUD data loaders: Small Area FMRs (ZIP) + Picture of Subsidized Households (county).

These read ONLY the slim CSVs committed under src/remon/data/ — huduser.gov
blocks scripted fetchers (HTTP 202 empty body), so CI never touches it. Refresh
annually with scripts/prepare_hud.py (instructions in its header).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from ..config import Config  # noqa: F401  (kept for signature parity with other sources)
from ..logging_setup import get_logger

log = get_logger("remon.hud")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _newest(pattern: str) -> Optional[Path]:
    files = sorted(DATA_DIR.glob(pattern))
    return files[-1] if files else None


def load_safmr(config: Config) -> Optional[pd.DataFrame]:
    """ZIP-indexed frame with safmr_2br / safmr_3br (gross rent ceilings, $)."""
    path = _newest("hud_safmr_*.csv")
    if not path:
        log.warning("[hud] no committed SAFMR file — Section 8 metrics skipped")
        return None
    df = pd.read_csv(path, dtype={"zip": str})
    df["zip"] = df["zip"].str.zfill(5)
    df = df.drop_duplicates("zip").set_index("zip")
    log.info("[hud] SAFMR loaded: %s (%d ZIPs)", path.name, len(df))
    return df


def load_psh(config: Config) -> Optional[pd.DataFrame]:
    """County-FIPS-indexed frame with units_all / units_hcv (sentinels pre-nulled)."""
    path = _newest("hud_psh_county_*.csv")
    if not path:
        log.warning("[hud] no committed PSH file — subsidized-housing metrics skipped")
        return None
    df = pd.read_csv(path, dtype={"fips": str})
    df["fips"] = df["fips"].str.zfill(5)
    # Guard against older committed files with HUD's "01XXX" pseudo-counties.
    df = df[df["fips"].str.fullmatch(r"\d{5}")]
    df = df.drop_duplicates("fips").set_index("fips")
    log.info("[hud] PSH loaded: %s (%d counties)", path.name, len(df))
    return df
