"""Load and validate config.yaml and the .env API keys.

This is the single source of truth for "what do we track and how". Every other
script imports `load_config()` from here, so markets and sources are defined in
exactly one place (config.yaml).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from .logging_setup import get_logger

log = get_logger("remon.config")

# Repo root = two levels up from this file (src/remon/config.py -> repo/).
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.yaml"

# API keys are read from the environment, never hardcoded (hard constraint #2).
API_KEY_ENV_VARS = {
    "fred": "FRED_API_KEY",
    "census": "CENSUS_API_KEY",
}


@dataclass
class County:
    name: str
    fips: str
    state_fips: str
    county_fips: str
    state_abbr: str


@dataclass
class Market:
    key: str
    name: str
    zips: List[str]
    county: County
    metro_name: str = ""


@dataclass
class Config:
    markets: Dict[str, Market]
    sources: Dict[str, Any]
    cache: Dict[str, Any]
    output: Dict[str, Any]
    analysis: Dict[str, Any]
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    # ---- convenience accessors ------------------------------------------------
    def all_zips(self) -> List[str]:
        seen: List[str] = []
        for m in self.markets.values():
            for z in m.zips:
                if z not in seen:
                    seen.append(z)
        return seen

    def all_counties(self) -> List[County]:
        return [m.county for m in self.markets.values()]

    def path(self, *parts: str) -> Path:
        """Resolve a config-relative path against the repo root."""
        return REPO_ROOT.joinpath(*parts)

    @property
    def raw_dir(self) -> Path:
        return self.path(self.cache["raw_dir"])

    @property
    def processed_dir(self) -> Path:
        return self.path(self.cache["processed_dir"])

    @property
    def docs_dir(self) -> Path:
        return self.path(self.output["docs_dir"])

    @property
    def max_age_days(self) -> int:
        return int(self.cache.get("max_age_days", 35))


def get_api_key(source: str) -> Optional[str]:
    """Return the API key for a source from the environment, or None if unset."""
    env_var = API_KEY_ENV_VARS.get(source)
    if not env_var:
        return None
    val = os.environ.get(env_var, "").strip()
    return val or None


def _require(d: Dict[str, Any], key: str, ctx: str) -> Any:
    if key not in d:
        raise ValueError(f"config.yaml: missing required key '{key}' in {ctx}")
    return d[key]


def _parse_county(d: Dict[str, Any], market_key: str) -> County:
    ctx = f"markets.{market_key}.county"
    return County(
        name=str(_require(d, "name", ctx)),
        fips=str(_require(d, "fips", ctx)),
        state_fips=str(_require(d, "state_fips", ctx)),
        county_fips=str(_require(d, "county_fips", ctx)),
        state_abbr=str(_require(d, "state_abbr", ctx)),
    )


def load_config(path: Optional[Path] = None, load_env: bool = True) -> Config:
    """Load config.yaml (+ .env) and validate its structure. Fails loudly."""
    if load_env:
        load_dotenv(REPO_ROOT / ".env")

    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError("config.yaml did not parse to a mapping")

    raw_markets = _require(raw, "markets", "config root")
    if not raw_markets:
        raise ValueError("config.yaml: 'markets' is empty — nothing to track")

    markets: Dict[str, Market] = {}
    for key, m in raw_markets.items():
        ctx = f"markets.{key}"
        zips = _require(m, "zips", ctx)
        if not isinstance(zips, list) or not zips:
            raise ValueError(f"config.yaml: {ctx}.zips must be a non-empty list")
        zips = [str(z).strip() for z in zips]
        for z in zips:
            if not (z.isdigit() and len(z) == 5):
                raise ValueError(
                    f"config.yaml: {ctx}.zips has invalid ZIP '{z}' "
                    f"(expected a 5-digit code)"
                )
        markets[key] = Market(
            key=key,
            name=str(_require(m, "name", ctx)),
            zips=zips,
            county=_parse_county(_require(m, "county", ctx), key),
            metro_name=str(m.get("metro_name", "")),
        )

    config = Config(
        markets=markets,
        sources=_require(raw, "sources", "config root"),
        cache=_require(raw, "cache", "config root"),
        output=_require(raw, "output", "config root"),
        analysis=raw.get("analysis", {}),
        raw=raw,
    )

    # Ensure cache/output dirs exist so later steps can write freely.
    config.raw_dir.mkdir(parents=True, exist_ok=True)
    config.processed_dir.mkdir(parents=True, exist_ok=True)
    config.docs_dir.mkdir(parents=True, exist_ok=True)

    return config


def summary(config: Config) -> str:
    """Human-readable summary for the `check` command (phase-1 acceptance)."""
    lines: List[str] = []
    lines.append("Markets:")
    for m in config.markets.values():
        lines.append(
            f"  - {m.name}: {len(m.zips)} ZIPs  "
            f"[{', '.join(m.zips[:6])}{'...' if len(m.zips) > 6 else ''}]"
        )
        lines.append(f"      county: {m.county.name} (FIPS {m.county.fips})")
    lines.append(f"  total unique ZIPs: {len(config.all_zips())}")

    lines.append("API keys (from environment / .env):")
    for source in API_KEY_ENV_VARS:
        present = "present" if get_api_key(source) else "MISSING"
        lines.append(f"  - {source:<7} ({API_KEY_ENV_VARS[source]}): {present}")

    lines.append("Paths:")
    lines.append(f"  - raw cache:  {config.raw_dir}")
    lines.append(f"  - processed:  {config.processed_dir}")
    lines.append(f"  - docs/out:   {config.docs_dir}")
    lines.append(f"  - reuse cached downloads newer than {config.max_age_days} days")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary(load_config()))
