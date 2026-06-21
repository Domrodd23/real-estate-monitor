"""Compute market metrics from cached raw files into one tidy, traceable table.

Output is LONG format — one row per (market, geography, region, metric) — so
every value carries its own source and as-of date (hard constraint #6). build.py
pivots this for display.

compute.py reads ONLY cached raw files here; it never downloads. If a required
raw file is missing, we fail loudly telling the user to run fetch.py first.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .config import Config, get_api_key
from .http import last_cached
from .logging_setup import get_logger
from .sources import fred, redfin, zillow

log = get_logger("remon.metrics")

OVERVALUATION_NOTE = (
    "Directional public-ratio proxy (current price-to-rent vs this ZIP's own "
    "5-yr median). Not a forecast or prediction."
)


# --------------------------------------------------------------------------- #
# Cache resolution
# --------------------------------------------------------------------------- #
def _require_cached(raw_dir: Path, name: str, ext: str) -> Path:
    path = last_cached(raw_dir, name, ext)
    if not path:
        raise FileNotFoundError(
            f"No cached '{name}.{ext}' in {raw_dir}. Run `python fetch.py` first."
        )
    return path


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #
def latest_and_yoy(
    row: pd.Series, date_cols: List[str]
) -> Optional[Tuple[pd.Timestamp, float, Optional[float]]]:
    """Return (latest_date, latest_value, yoy_pct) from a wide month row.

    yoy_pct compares the latest value to the value ~12 months earlier; None if
    no comparable prior month exists within tolerance.
    """
    vals = [(pd.to_datetime(d), row[d]) for d in date_cols if pd.notna(row[d])]
    if not vals:
        return None
    vals.sort()
    latest_date, latest_val = vals[-1]
    target = latest_date - pd.DateOffset(years=1)
    prior = None
    for d, v in vals:
        if d <= target:
            prior = (d, v)
    yoy = None
    if prior and prior[1] and prior[1] > 0 and abs((prior[0] - target).days) <= 70:
        yoy = (latest_val / prior[1] - 1.0) * 100.0
    return latest_date, float(latest_val), (float(yoy) if yoy is not None else None)


def monthly_price_to_rent(
    zhvi_row: pd.Series, zori_row: pd.Series,
    zhvi_dates: List[str], zori_dates: List[str],
) -> pd.Series:
    """Monthly price-to-rent series over months present in BOTH ZHVI and ZORI."""
    common = [d for d in zhvi_dates if d in set(zori_dates)]
    out: Dict[pd.Timestamp, float] = {}
    for d in common:
        hv, rt = zhvi_row.get(d), zori_row.get(d)
        if pd.notna(hv) and pd.notna(rt) and rt > 0:
            out[pd.to_datetime(d)] = hv / (rt * 12.0)
    return pd.Series(out).sort_index()


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class _Rows:
    """Accumulates metric rows with consistent schema."""

    def __init__(self) -> None:
        self.rows: List[dict] = []

    def add(self, *, market, market_name, geography, region, region_label,
            metric, value, unit, source, as_of, note=""):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return
        self.rows.append({
            "market": market, "market_name": market_name,
            "geography": geography, "region": region, "region_label": region_label,
            "metric": metric, "value": value, "unit": unit,
            "source": source, "as_of": as_of, "note": note,
        })

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=[
            "market", "market_name", "geography", "region", "region_label",
            "metric", "value", "unit", "source", "as_of", "note",
        ])


def build_metrics(config: Config) -> pd.DataFrame:
    raw_dir = config.raw_dir
    rows = _Rows()
    history_years = int(config.analysis.get("overvaluation_history_years", 5))

    _zip_metrics(config, raw_dir, rows, history_years)
    _metro_metrics(config, raw_dir, rows)
    _county_metrics(config, raw_dir, rows)
    _national_metrics(config, raw_dir, rows)

    df = rows.frame()
    log.info("Computed %d metric rows across %d markets", len(df), len(config.markets))
    return df


def _index_zip(path: Path, zips: List[str], source: str):
    df, dates = zillow.load_zip_series(path, zips, source)
    df = df.set_index("RegionName")
    df = df[~df.index.duplicated(keep="first")]
    return df, dates


def _zip_metrics(config, raw_dir, rows, history_years):
    zhvi, zhvi_dates = _index_zip(
        _require_cached(raw_dir, "zillow_zhvi_zip", "csv"), config.all_zips(), "Zillow ZHVI")
    zori, zori_dates = _index_zip(
        _require_cached(raw_dir, "zillow_zori_zip", "csv"), config.all_zips(), "Zillow ZORI")
    window = history_years * 12

    for m in config.markets.values():
        for z in m.zips:
            common = dict(market=m.key, market_name=m.name, geography="zip",
                          region=z, region_label=z)
            # Home value
            if z in zhvi.index:
                res = latest_and_yoy(zhvi.loc[z], zhvi_dates)
                if res:
                    d, val, yoy = res
                    rows.add(**common, metric="home_value", value=round(val), unit="USD",
                             source="Zillow ZHVI", as_of=d.date().isoformat())
                    rows.add(**common, metric="home_value_12m_pct",
                             value=(round(yoy, 1) if yoy is not None else None),
                             unit="%", source="Zillow ZHVI", as_of=d.date().isoformat())
            # Rent
            if z in zori.index:
                res = latest_and_yoy(zori.loc[z], zori_dates)
                if res:
                    d, val, yoy = res
                    rows.add(**common, metric="rent", value=round(val), unit="USD/mo",
                             source="Zillow ZORI", as_of=d.date().isoformat())
                    rows.add(**common, metric="rent_12m_pct",
                             value=(round(yoy, 1) if yoy is not None else None),
                             unit="%", source="Zillow ZORI", as_of=d.date().isoformat())
            # Price-to-rent + overvaluation proxy (need both series)
            if z in zhvi.index and z in zori.index:
                pr = monthly_price_to_rent(zhvi.loc[z], zori.loc[z], zhvi_dates, zori_dates)
                if len(pr) >= 24:
                    current = float(pr.iloc[-1])
                    as_of = pr.index[-1].date().isoformat()
                    rows.add(**common, metric="price_to_rent", value=round(current, 1),
                             unit="ratio", source="Zillow ZHVI+ZORI", as_of=as_of)
                    median_5yr = float(pr.iloc[-window:].median())
                    if median_5yr > 0:
                        stretch = (current / median_5yr - 1.0) * 100.0
                        rows.add(**common, metric="overvaluation_pr_pct",
                                 value=round(stretch, 1), unit="%",
                                 source="Zillow ZHVI+ZORI", as_of=as_of,
                                 note=OVERVALUATION_NOTE)


def _metro_metrics(config, raw_dir, rows):
    specs = [
        ("zillow_invt_fs_metro", "inventory", "For-sale inventory", "Zillow inventory (metro)"),
        ("zillow_new_listings_metro", "new_listings", "New listings", "Zillow new listings (metro)"),
    ]
    for cache_name, metric, _friendly, source in specs:
        path = last_cached(raw_dir, cache_name, "csv")
        if not path:
            log.warning("metro metric '%s' skipped — no cached file", metric)
            continue
        df, dates = zillow.load_metro_series(path, source)
        df = df.set_index("RegionName")
        df = df[~df.index.duplicated(keep="first")]
        for m in config.markets.values():
            metro = m.metro_name
            if not metro or metro not in df.index:
                log.warning("[%s] no metro row for '%s'", metric, metro)
                continue
            res = latest_and_yoy(df.loc[metro], dates)
            if not res:
                continue
            d, val, yoy = res
            common = dict(market=m.key, market_name=m.name, geography="metro",
                          region=metro, region_label=metro)
            rows.add(**common, metric=metric, value=round(val), unit="listings",
                     source=source, as_of=d.date().isoformat())
            rows.add(**common, metric=f"{metric}_12m_pct",
                     value=(round(yoy, 1) if yoy is not None else None), unit="%",
                     source=source, as_of=d.date().isoformat())


def _county_metrics(config, raw_dir, rows):
    # Census ACS income + population
    acs_path = last_cached(raw_dir, "census_acs", "csv")
    acs = pd.read_csv(acs_path, dtype={"county_fips": str}) if acs_path else None
    mig_path = last_cached(raw_dir, "census_migration", "csv")
    mig = pd.read_csv(mig_path, dtype={"county_fips": str}) if mig_path else None
    # Redfin counties
    redfin_path = last_cached(raw_dir, "redfin_county_market_tracker", "gz")
    rf = redfin.load_counties(redfin_path, config) if redfin_path else None
    expected_region = redfin._expected_region(config) if rf is not None else {}

    for m in config.markets.values():
        c = m.county
        common = dict(market=m.key, market_name=m.name, geography="county",
                      region=c.fips, region_label=c.name)
        if acs is not None:
            r = acs[acs["county_fips"] == c.fips]
            if not r.empty:
                yr = int(r.iloc[0]["year"])
                inc = r.iloc[0].get("median_household_income")
                pop = r.iloc[0].get("population")
                rows.add(**common, metric="median_household_income",
                         value=(round(inc) if pd.notna(inc) else None), unit="USD",
                         source="Census ACS 5-yr", as_of=str(yr))
                rows.add(**common, metric="population",
                         value=(round(pop) if pd.notna(pop) else None), unit="people",
                         source="Census ACS 5-yr", as_of=str(yr))
        if mig is not None:
            r = mig[mig["county_fips"] == c.fips]
            if not r.empty:
                net = int(r.iloc[0]["net_migration"])
                yr = int(r.iloc[0]["year"])
                direction = "net inflow" if net > 0 else "net outflow"
                rows.add(**common, metric="net_migration", value=net, unit="people/yr",
                         source="Census county-to-county flows", as_of=str(yr),
                         note=f"{direction} (domestic county-to-county)")
        if rf is not None:
            region = expected_region.get(m.key)
            g = rf[rf["region"] == region]
            for col, metric, unit in [
                ("median_sale_price", "median_sale_price", "USD"),
                ("median_dom", "median_dom", "days"),
                ("price_drops", "price_drop_share", "share"),
                ("avg_sale_to_list", "sale_to_list_ratio", "ratio"),
                ("sold_above_list", "sold_above_list_share", "share"),
            ]:
                if col not in g.columns:
                    continue
                nn = g.dropna(subset=[col])
                if nn.empty:
                    continue
                last = nn.iloc[-1]
                rows.add(**common, metric=metric, value=round(float(last[col]), 3),
                         unit=unit, source="Redfin county",
                         as_of=pd.to_datetime(last["period_end"]).date().isoformat())


def _national_metrics(config, raw_dir, rows):
    series_map = config.sources["fred"]["series"]
    sid = series_map.get("mortgage30us")
    if not sid:
        return
    path = last_cached(raw_dir, f"fred_{sid}", "csv")
    if not path:
        return
    df = fred.load_series(path, f"FRED:{sid}")
    last = df.dropna(subset=["value"]).iloc[-1]
    rows.add(market="national", market_name="National", geography="national",
             region="US", region_label="United States", metric="mortgage_30yr",
             value=round(float(last["value"]), 2), unit="%", source="FRED MORTGAGE30US",
             as_of=last["date"].date().isoformat())


# --------------------------------------------------------------------------- #
# Acceptance-test pretty printer
# --------------------------------------------------------------------------- #
def print_table(df: pd.DataFrame) -> None:
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 160)
    for market, g in df.groupby("market"):
        print(f"\n{'=' * 78}\nMARKET: {market}\n{'=' * 78}")
        show = g[["geography", "region_label", "metric", "value", "unit", "source", "as_of"]]
        print(show.to_string(index=False))
