"""National county map explorer (docs/map.html).

Renders an interactive US county choropleth from county-level public data
(Zillow ZHVI/ZORI by county, Census ACS by county). A dropdown switches the
displayed metric entirely client-side. The user's tracked counties are outlined.

This is a separate page; the core dashboard is unchanged. Built best-effort:
build.py logs a warning and continues if the national data isn't available.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import plotly.colors as pcol
import plotly.io as pio
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .http import cache_path, get_text, last_cached
from .logging_setup import get_logger
from .sources import zillow

log = get_logger("remon.mapview")

GEOJSON_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Displayed metrics. `diverging` centers the scale on 0 (for % change).
# `desc` is the plain-English one-liner shown under the map legend.
# `group` drives the Reventure-style grouped metric selector.
METRICS = [
    # -- Popular ------------------------------------------------------------
    {"key": "home_value", "label": "Home prices", "fmt": "usd", "scale": "Viridis",
     "group": "Popular",
     "desc": "Typical home value (Zillow ZHVI) — what a mid-range home costs here."},
    {"key": "home_value_12m_pct", "label": "1-yr price change", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Popular",
     "desc": "How much typical home prices rose or fell over the last 12 months."},
    {"key": "cap_rate", "label": "Cap rate (est.)", "fmt": "pct1", "scale": "Viridis",
     "group": "Popular",
     "desc": "Est. net rent ÷ home price, after property tax (this area's ACS "
             "effective rate where available, state average otherwise), insurance "
             "and 20% for vacancy/upkeep. Higher = better cash flow."},
    {"key": "home_value_fc_12m", "label": "1-yr price forecast", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Popular",
     "desc": "Zillow's forecast of home-value growth over the next 12 months. "
             "ZIP-level only — counties show no data."},
    {"key": "growth_score", "label": "Growth score (est.)", "fmt": "score100",
     "scale": "growth", "group": "Popular",
     "desc": "0–100 composite of public fundamentals: migration, affordability "
             "headroom, 5-yr momentum, market tightness, incomes (+ Zillow's "
             "forecast for ZIPs). Percentile-ranked, transparent weights — a "
             "screening aid, NOT a prediction."},
    # -- Price trends --------------------------------------------------------
    {"key": "home_value_5y_pct", "label": "5-yr price change", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Price trends",
     "desc": "Total price growth over the last five years."},
    {"key": "home_value_mom_pct", "label": "1-mo price change", "fmt": "pct2",
     "scale": "RdYlGn", "diverging": True, "group": "Price trends",
     "desc": "Price move in the latest month — the short-term pulse."},
    {"key": "pct_from_peak", "label": "% below peak", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Price trends",
     "desc": "How far today's value sits below this area's highest value on record. "
             "0% = at its all-time high."},
    {"key": "value_income_ratio", "label": "Price vs. income", "fmt": "ratio",
     "scale": "Plasma", "group": "Price trends",
     "desc": "Home value (Zillow, current) ÷ median income (Census ACS 2020–2024 "
             "average — incomes lag ~2 yrs, so ratios read slightly high). "
             "~3x is historically normal; 5x+ = stretched."},
    # -- Market heat -----------------------------------------------------------
    {"key": "days_on_market", "label": "Days on market", "fmt": "days",
     "scale": "Viridis", "group": "Market heat",
     "desc": "Median days a listing sits before sale or pending "
             "(Realtor.com, latest month). Lower = hotter market."},
    {"key": "price_cut_share", "label": "Listings with price cuts", "fmt": "pct1",
     "scale": "Viridis", "group": "Market heat",
     "desc": "% of active listings that took a price reduction this month "
             "(Realtor.com). Rising cuts = softening prices."},
    {"key": "inventory_yoy", "label": "Inventory change (1-yr)", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Market heat",
     "desc": "Change in active for-sale listings vs a year ago (Realtor.com). "
             "Fast-rising inventory usually precedes flat or falling prices."},
    {"key": "median_sale_price", "label": "Sale price (actual)", "fmt": "usd",
     "scale": "Viridis", "group": "Market heat",
     "desc": "Median price homes actually SOLD for, latest month (Redfin). "
             "County-level data."},
    {"key": "sold_above_list_pct", "label": "Sold above asking", "fmt": "pct1",
     "scale": "Viridis", "group": "Market heat",
     "desc": "% of sales that closed above list price (Redfin) — the "
             "bidding-war gauge. County-level data."},
    {"key": "months_supply", "label": "Months of supply", "fmt": "mos",
     "scale": "Viridis", "group": "Market heat",
     "desc": "Active inventory ÷ monthly sales pace (Redfin). Under ~3 months "
             "favors sellers; over ~6 favors buyers. County-level data."},
    # -- Rental & investor ---------------------------------------------------
    {"key": "rent", "label": "Rent", "fmt": "usd_mo", "scale": "Viridis",
     "group": "Rental & investor",
     "desc": "Typical monthly rent (Zillow ZORI). Not every area reports rent."},
    {"key": "rent_12m_pct", "label": "1-yr rent change", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Rental & investor",
     "desc": "How much typical rents rose or fell over the last 12 months."},
    {"key": "price_to_rent", "label": "Price vs. rent", "fmt": "ratio", "scale": "Plasma",
     "group": "Rental & investor",
     "desc": "Home price ÷ one year of rent. Lower = better for rental cash flow."},
    {"key": "gross_yield", "label": "Gross rent yield", "fmt": "pct1", "scale": "Viridis",
     "group": "Rental & investor",
     "desc": "A year of rent ÷ home price, before any expenses."},
    # -- Migration & growth ----------------------------------------------------
    {"key": "inbound_movers_pct", "label": "Movers-in %", "fmt": "pct1",
     "scale": "Viridis", "group": "Migration & growth",
     "desc": "Share of residents who arrived from another county, state or abroad "
             "in the past year (Census ACS 2020–2024 average)."},
    {"key": "net_dom_mig_rate", "label": "Net domestic migration", "fmt": "per1k",
     "scale": "RdYlGn", "diverging": True, "group": "Migration & growth",
     "desc": "Net people moving in from elsewhere in the US per 1,000 residents, "
             "July 2024–July 2025 (Census estimates). County-level data."},
    {"key": "agi_net_percap", "label": "Income moving in (net)", "fmt": "usd_s",
     "scale": "RdYlGn", "diverging": True, "group": "Migration & growth",
     "desc": "Net income (AGI) carried by movers in minus movers out, per resident "
             "(IRS tax returns 2022–2023). County-level data."},
    # -- Demographics ----------------------------------------------------------
    {"key": "median_income", "label": "Household income", "fmt": "usd", "scale": "Viridis",
     "group": "Demographics",
     "desc": "Median household income (Census ACS 2020–2024 average)."},
    {"key": "population", "label": "Population", "fmt": "count", "scale": "Cividis",
     "group": "Demographics",
     "desc": "Population (Census ACS 2020–2024)."},
    # -- Taxes & policy ----------------------------------------------------------
    {"key": "property_tax_rate", "label": "Property tax rate", "fmt": "pct2u",
     "scale": "Viridis", "group": "Taxes & policy",
     "desc": "Median property taxes ÷ median home value, owner-reported (Census ACS "
             "2020–2024; ZIPs are ZCTAs). Levels run ~10–15% low vs assessor data — "
             "rankings hold. Areas at the ACS $10,000 tax cap are hidden."},
    {"key": "constr_wage_index", "label": "Construction labor cost", "fmt": "idx",
     "scale": "Viridis", "group": "Taxes & policy",
     "desc": "County construction wages vs the US average (BLS QCEW 2025, private "
             "NAICS 23; 1.00 = US). A rehab-labor cost proxy. Small/suppressed "
             "counties show the prior year or state figure. County-level data."},
    {"key": "landlord_score", "label": "Landlord friendliness", "fmt": "score",
     "scale": "Viridis", "group": "Taxes & policy",
     "desc": "State landlord-tenant climate, 0–10 (higher = more landlord-friendly): "
             "rent-control preemption, eviction speed, deposit caps, just-cause, "
             "notice burden. STATE law — all counties in a state match (editorial "
             "index, July 2026; some cities add local rules)."},
    {"key": "sec8_premium", "label": "Section 8 rent premium", "fmt": "pct",
     "scale": "RdYlGn", "diverging": True, "group": "Taxes & policy",
     "desc": "HUD voucher rent ceiling (FY2026 Small Area FMR, 2-bed GROSS rent incl. "
             "utilities) vs typical asking rent (Zillow ZORI). Positive = vouchers may "
             "pay above market; utilities bias this UP. ZIP-level only."},
    {"key": "hud_units_per_1k", "label": "Subsidized housing density", "fmt": "per1ku",
     "scale": "Viridis", "group": "Taxes & policy",
     "desc": "HUD-subsidized units, all programs (HUD, Dec 2025) per 1,000 renter "
             "households (Census ACS). County-level data."},
    {"key": "hcv_per_1k", "label": "Vouchers per 1k renters", "fmt": "per1ku",
     "scale": "Viridis", "group": "Taxes & policy",
     "desc": "Housing Choice Vouchers in use (HUD, Dec 2025) per 1,000 renter "
             "households. High = deep Section 8 tenant pool. County-level data."},
]

# Effective property-tax rates on owner-occupied housing by state (approximate,
# Tax Foundation-style figures) — used only for the ESTIMATED cap rate.
STATE_TAX_RATES = {
    "AL": 0.40, "AK": 1.14, "AZ": 0.63, "AR": 0.64, "CA": 0.75, "CO": 0.55,
    "CT": 2.15, "DE": 0.61, "DC": 0.62, "FL": 0.91, "GA": 0.92, "HI": 0.29,
    "ID": 0.67, "IL": 2.23, "IN": 0.84, "IA": 1.57, "KS": 1.43, "KY": 0.85,
    "LA": 0.56, "ME": 1.24, "MD": 1.05, "MA": 1.20, "MI": 1.48, "MN": 1.11,
    "MS": 0.67, "MO": 1.01, "MT": 0.74, "NE": 1.67, "NV": 0.59, "NH": 2.09,
    "NJ": 2.33, "NM": 0.67, "NY": 1.73, "NC": 0.82, "ND": 1.00, "OH": 1.59,
    "OK": 0.89, "OR": 0.93, "PA": 1.53, "RI": 1.40, "SC": 0.57, "SD": 1.17,
    "TN": 0.67, "TX": 1.90, "UT": 0.57, "VT": 1.90, "VA": 0.87, "WA": 0.87,
    "WV": 0.59, "WI": 1.61, "WY": 0.56,
}
INSURANCE_RATE = 0.5    # % of home value per year (est.)
UPKEEP_SHARE = 0.20     # share of gross rent for vacancy + maintenance (est.)


def _fmt(kind, v):
    if v is None or pd.isna(v):
        return "n/a"
    if kind == "usd":
        return f"${v:,.0f}"
    if kind == "usd_mo":
        return f"${v:,.0f}/mo"
    if kind == "pct":
        return f"{v:+.1f}%"
    if kind == "pct1":
        return f"{v:.1f}%"
    if kind == "pct2":
        return f"{v:+.2f}%"
    if kind == "ratio":
        return f"{v:.1f}x"
    if kind == "count":
        return f"{v:,.0f}"
    if kind == "per1k":
        return f"{v:+.1f}/1k"
    if kind == "days":
        return f"{v:,.0f} days"
    if kind == "usd_s":
        # Sign before the dollar sign, mirroring the JS formatter exactly.
        return ("+$" if v >= 0 else "-$") + f"{abs(v):,.0f}"
    if kind == "mos":
        return f"{v:.1f} mo"
    if kind == "pct2u":
        return f"{v:.2f}%"
    if kind == "idx":
        return f"{v:.2f}x US"
    if kind == "score":
        return f"{v:.0f}/10"
    if kind == "per1ku":
        return f"{v:,.0f}/1k"
    if kind == "score100":
        return f"{v:.0f}/100"
    return str(v)


def _investor_metrics(value, rent, state_abbr, tax_rate=None):
    """(gross_yield %, cap_rate %, est. annual expenses $) or Nones.

    Transparent estimate: property tax (the area's ACS effective rate when
    supplied, else the state average) + insurance (0.5%/yr of value) + 20% of
    gross rent for vacancy/maintenance.
    """
    if value is None or rent is None or pd.isna(value) or pd.isna(rent) \
            or value <= 0 or rent <= 0:
        return None, None, None
    gross = float(rent) * 12.0
    if tax_rate is None or pd.isna(tax_rate) or tax_rate <= 0:
        tax_rate = STATE_TAX_RATES.get((state_abbr or "").upper(), 1.0)
    expenses = float(value) * (float(tax_rate) + INSURANCE_RATE) / 100.0 + gross * UPKEEP_SHARE
    return (round(gross / float(value) * 100.0, 2),
            round((gross - expenses) / float(value) * 100.0, 2),
            round(expenses))


def _scale_array(name: str):
    """Named plotly colorscale -> explicit [[pos, 'rgb(...)'], ...] array."""
    try:
        return pcol.get_colorscale(name)
    except Exception:  # noqa: BLE001
        return pcol.get_colorscale("Viridis")


def _load_geojson(config: Config):
    raw = config.raw_dir
    path = last_cached(raw, "us_counties_geojson", "json")
    if not path:
        log.info("Fetching US counties GeoJSON (one-time, ~3 MB)")
        text = get_text(GEOJSON_URL)
        path = cache_path(raw, "us_counties_geojson", "json")
        path.write_text(text, encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


# Growth score: signed weights over percentile ranks of public fundamentals.
# Negative weight = lower is better (e.g. cheap vs income = appreciation headroom).
# Weights are renormalized over the components an area actually has; areas with
# under half the total weight available get no score rather than a shaky one.
GROWTH_W_COUNTY = {
    "net_dom_mig_rate": 20, "agi_net_percap": 10, "inbound_movers_pct": 5,
    "value_income_ratio": -20, "home_value_5y_pct": 15,
    "months_supply": -10, "inventory_yoy": -10, "median_income": 10,
}
GROWTH_W_ZIP = {
    "home_value_fc_12m": 20, "value_income_ratio": -20, "home_value_5y_pct": 15,
    "inbound_movers_pct": 10, "days_on_market": -10, "inventory_yoy": -10,
    "price_cut_share": -10, "median_income": 5,
}
GROWTH_MIN_WEIGHT = 50  # of 100


def _growth_score(vals: pd.DataFrame, weights: dict) -> pd.Series:
    """0–100 weighted mean of percentile ranks (NaN below GROWTH_MIN_WEIGHT)."""
    num = pd.Series(0.0, index=vals.index)
    den = pd.Series(0.0, index=vals.index)
    for k, w in weights.items():
        col = pd.to_numeric(vals.get(k), errors="coerce") if k in vals.columns else None
        if col is None or col.notna().sum() < 20:
            continue
        r = col.rank(pct=True)
        if w < 0:
            r = 1.0 - r
        has = col.notna()
        num += (r * abs(w)).fillna(0.0)
        den += has * abs(w)
    score = (num / den.where(den > 0) * 100).where(den >= GROWTH_MIN_WEIGHT)
    return score.round(0)


MOVERS_MIN_POP = 500  # below this ACS denominator, Movers-in % is sampling noise

# ACS top/bottom-codes: values AT these literals mean "beyond the cap", so the
# ratio would be a known-wrong lower/upper bound — mask instead of mislead.
ACS_TAX_CODES = (10001, 199)         # B25103 median real-estate taxes paid
ACS_VALUE_CODES = (2000001, 9999)    # B25077 median home value
MIN_OWNER_UNITS = 100                # tax-rate sample floor


def _tax_rate(frame: pd.DataFrame) -> pd.Series:
    """Effective property-tax rate % from ACS columns, top-codes/thin masked."""
    cols = ("median_re_taxes", "median_home_value_acs", "owner_occupied_units")
    if not all(c in frame.columns for c in cols):
        return pd.Series(pd.NA, index=frame.index)
    tax = pd.to_numeric(frame[cols[0]], errors="coerce")
    val = pd.to_numeric(frame[cols[1]], errors="coerce")
    own = pd.to_numeric(frame[cols[2]], errors="coerce")
    rate = (tax / val.where(val > 0) * 100).round(2)
    return rate.where(~tax.isin(ACS_TAX_CODES) & ~val.isin(ACS_VALUE_CODES)
                      & (own >= MIN_OWNER_UNITS))

# Post-2022 Census vintages key Connecticut on planning-region FIPS and split
# Valdez-Cordova AK, while the map GeoJSON/Zillow use legacy county FIPS. For
# RATE/SHARE metrics we fill each legacy county from its dominant successor.
LEGACY_FIPS_XWALK = {
    "09001": "09190",  # Fairfield -> Western CT
    "09003": "09110",  # Hartford -> Capitol
    "09005": "09160",  # Litchfield -> Northwest Hills
    "09007": "09130",  # Middlesex -> Lower CT River Valley
    "09009": "09170",  # New Haven -> South Central CT
    "09011": "09180",  # New London -> Southeastern CT
    "09013": "09110",  # Tolland -> Capitol
    "09015": "09150",  # Windham -> Northeastern CT
    "02261": "02063",  # Valdez-Cordova AK -> Chugach (larger successor)
}


def _reindex_xwalk(s: pd.Series, index) -> pd.Series:
    """Reindex onto the map's legacy-FIPS index, filling CT/AK legacy rows from
    their new-vintage successor so rate/share metrics still render there."""
    out = s.reindex(index)
    fill = s.reindex([LEGACY_FIPS_XWALK.get(f, f) for f in index])
    fill.index = index
    return out.fillna(fill)


def _movers_pct(frame: pd.DataFrame) -> pd.Series:
    """Movers-in % from the ACS B07001 mobility columns (5-yr average)."""
    cols = ["mob_pop_1plus", "mob_in_county_samestate", "mob_in_state", "mob_in_abroad"]
    if not all(c in frame.columns for c in cols):
        return pd.Series(pd.NA, index=frame.index)
    denom = pd.to_numeric(frame[cols[0]], errors="coerce")
    total = sum(pd.to_numeric(frame[c], errors="coerce") for c in cols[1:])
    return (total / denom.where(denom >= MOVERS_MIN_POP) * 100).round(1)


def build_county_table(config: Config) -> pd.DataFrame:
    """One row per US county (indexed by FIPS) with the map metrics."""
    raw = config.raw_dir

    zhvi_path = last_cached(raw, "zillow_zhvi_county", "csv")
    if not zhvi_path:
        raise FileNotFoundError("county ZHVI not cached — run fetch.py first")
    zhvi, zd = zillow.load_county_series(zhvi_path, "Zillow county ZHVI")
    latest = zd[-1]
    prior = zd[-13] if len(zd) >= 13 else zd[0]
    zhvi = zhvi.drop_duplicates(subset="fips").set_index("fips")
    df = pd.DataFrame(index=zhvi.index)
    state = zhvi.get("State", "").astype(str)
    df["name"] = zhvi["RegionName"].astype(str) + ", " + state
    df["state"] = state
    df["home_value"] = pd.to_numeric(zhvi[latest], errors="coerce")
    prior_v = pd.to_numeric(zhvi[prior], errors="coerce")
    df["home_value_12m_pct"] = (df["home_value"] / prior_v - 1.0) * 100.0

    # Price-trend metrics from the same ZHVI history.
    vals = zhvi[zd].apply(pd.to_numeric, errors="coerce")
    if len(zd) >= 2:
        df["home_value_mom_pct"] = (df["home_value"] / vals[zd[-2]] - 1.0) * 100.0
    if len(zd) >= 61:
        df["home_value_5y_pct"] = (df["home_value"] / vals[zd[-61]] - 1.0) * 100.0
    peak = vals.max(axis=1)
    df["pct_from_peak"] = (df["home_value"] / peak - 1.0) * 100.0

    # Census ACS national income + population (+ mobility, property tax) —
    # loaded BEFORE the rent block so the tax rate can feed the cap rate.
    acs_path = last_cached(raw, "census_acs_national", "csv")
    acs = None
    if acs_path:
        acs = pd.read_csv(acs_path, dtype={"county_fips": str}).drop_duplicates("county_fips")
        acs = acs.set_index("county_fips")
        df["median_income"] = pd.to_numeric(acs.get("median_household_income"), errors="coerce").reindex(df.index)
        df["population"] = pd.to_numeric(acs.get("population"), errors="coerce").reindex(df.index)
        # Share metric: safe to fill CT/AK legacy counties from their successor region.
        df["inbound_movers_pct"] = _reindex_xwalk(_movers_pct(acs), df.index)
        df["property_tax_rate"] = _reindex_xwalk(_tax_rate(acs), df.index)
    else:
        df["median_income"] = pd.NA
        df["population"] = pd.NA
        df["property_tax_rate"] = pd.NA

    # Rent (ZORI) — partial county coverage
    zori_path = last_cached(raw, "zillow_zori_county", "csv")
    if zori_path:
        zori, zod = zillow.load_county_series(zori_path, "Zillow county ZORI")
        zori = zori.drop_duplicates(subset="fips").set_index("fips")
        df["rent"] = pd.to_numeric(zori[zod[-1]], errors="coerce").reindex(df.index)
        if len(zod) >= 13:
            rent_prior = pd.to_numeric(zori[zod[-13]], errors="coerce").reindex(df.index)
            df["rent_12m_pct"] = (df["rent"] / rent_prior - 1.0) * 100.0
        df["price_to_rent"] = df["home_value"] / (df["rent"] * 12.0)
        inv = [_investor_metrics(v, r, s, t) for v, r, s, t in
               zip(df["home_value"], df["rent"], df["state"], df["property_tax_rate"])]
        df["gross_yield"] = [x[0] for x in inv]
        df["cap_rate"] = [x[1] for x in inv]
        df["exp_est"] = [x[2] for x in inv]
    else:
        for col in ("rent", "rent_12m_pct", "price_to_rent",
                    "gross_yield", "cap_rate", "exp_est"):
            df[col] = pd.NA

    # Price vs. income — computable from columns already on the table.
    income = pd.to_numeric(df["median_income"], errors="coerce")
    df["value_income_ratio"] = (df["home_value"] / income.where(income > 0)).round(1)

    # Census PEP: net domestic migration rate per 1,000 (county, annual).
    pep_path = last_cached(raw, "census_pep_county", "csv")
    if pep_path:
        try:
            pep = pd.read_csv(pep_path, encoding="latin-1",
                              dtype={"STATE": str, "COUNTY": str}, low_memory=False)
            pep = pep[pep["SUMLEV"] == 50].copy()
            pep["fips"] = pep["STATE"].str.zfill(2) + pep["COUNTY"].str.zfill(3)
            rate_col = sorted(c for c in pep.columns if c.startswith("RDOMESTICMIG"))[-1]
            rates = pd.to_numeric(pep.set_index("fips")[rate_col], errors="coerce").round(1)
            df["net_dom_mig_rate"] = _reindex_xwalk(rates, df.index)
            log.info("[map] PEP migration joined (%s)", rate_col)
        except Exception as exc:  # noqa: BLE001
            log.warning("[map] PEP migration skipped: %s", exc)

    # IRS SOI: net AGI carried by movers, per resident (county, ~2-yr lag).
    try:
        from .sources import irs
        net_agi = irs.load_net_agi(config)
        if net_agi is not None:
            if acs is not None:
                # Per-capita on each geography's NATIVE vintage (IRS 2223 uses CT
                # planning regions), then crosswalk the rate to legacy FIPS.
                pop_native = pd.to_numeric(acs.get("population"), errors="coerce")
                percap = (net_agi / pop_native.where(pop_native > 0)).round(0)
                df["agi_net_percap"] = _reindex_xwalk(percap.dropna(), df.index)
            else:
                pop = pd.to_numeric(df["population"], errors="coerce")
                df["agi_net_percap"] = (net_agi.reindex(df.index) / pop.where(pop > 0)).round(0)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] IRS migration skipped: %s", exc)

    # Landlord friendliness (state law, committed editorial index).
    scores = load_landlord_state_scores()
    if scores:
        df["landlord_score"] = df["state"].map(lambda s: scores.get(str(s).upper()))

    # BLS QCEW construction wage index (county; committed data primary).
    # The crosswalk goes INTO the loader so CT/AK pick up their successor
    # region's value before the state-average fallback flattens them.
    try:
        from .sources import bls
        widx = bls.load_wage_index(config, df.index, LEGACY_FIPS_XWALK)
        if widx is not None:
            df["constr_wage_index"] = widx
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] BLS wage index skipped: %s", exc)

    # HUD Picture of Subsidized Households (county; committed data).
    try:
        from .sources import hud
        psh = hud.load_psh(config)
        if psh is not None and acs is not None:
            renters = pd.to_numeric(acs.get("renter_households"), errors="coerce")
            renters = renters.where(renters >= 100)
            per1k_all = (1000 * psh["units_all"] / renters).round(0)
            per1k_hcv = (1000 * psh["units_hcv"] / renters).round(0)
            df["hud_units_per_1k"] = _reindex_xwalk(per1k_all.dropna(), df.index)
            df["hcv_per_1k"] = _reindex_xwalk(per1k_hcv.dropna(), df.index)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] HUD subsidized-housing metrics skipped: %s", exc)

    # Redfin sale-side metrics (county, monthly; joined by county name).
    try:
        from .sources import redfin
        sale = redfin.load_national_sale_metrics(config)
        if sale is not None:
            # Conservative name normalization for the join misses (VA/MD
            # independent cities: Redfin says "Alexandria, VA" / "Baltimore
            # City County, MD" where Zillow says "Alexandria City, VA" /
            # "Baltimore City, MD"). Normalized keys that collide are skipped.
            def _norm(s):
                s = s.lower().replace(".", "")
                s = s.replace(" city county,", " city,")
                if " county," not in s and " city," not in s and "," in s:
                    s = s.replace(",", " city,", 1)  # bare "Alexandria, VA"
                return s
            norm_map = {}
            for region in sale.index:
                k = _norm(region)
                norm_map[k] = None if k in norm_map else region
            for k in ("median_sale_price", "sold_above_list_pct", "months_supply"):
                direct = pd.to_numeric(df["name"].map(sale[k]), errors="coerce")
                fb_region = df["name"].map(lambda n: norm_map.get(_norm(n)))
                fallback = pd.to_numeric(fb_region.map(sale[k]), errors="coerce")
                df[k] = direct.fillna(fallback)
            log.info("[map] Redfin sale metrics joined: %d/%d counties",
                     int(df["median_sale_price"].notna().sum()), len(df))
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] Redfin sale metrics skipped: %s", exc)

    # Realtor.com market-heat trio (county, monthly).
    try:
        from .sources import realtor
        heat = realtor.load_county_metrics(config)
        if heat is not None:
            for k in ("days_on_market", "price_cut_share", "inventory_yoy", "listings"):
                df[k] = pd.to_numeric(heat[k], errors="coerce").reindex(df.index)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] Realtor county metrics skipped: %s", exc)

    tracked = {m.county.fips for m in config.markets.values()}
    df["is_tracked"] = df.index.isin(tracked)
    # Composite growth score LAST — it ranks across every joined fundamental.
    df["growth_score"] = _growth_score(df, GROWTH_W_COUNTY)
    log.info("[map] built county table: %d counties (%d scored)",
             len(df), int(df["growth_score"].notna().sum()))
    return df


def _prep(df: pd.DataFrame, m: Dict):
    vals = pd.to_numeric(df[m["key"]], errors="coerce")
    clean = vals.dropna()
    if m.get("diverging"):
        a = float(clean.abs().quantile(0.95)) if len(clean) else 1.0
        zmin, zmax = -a, a
    else:
        zmin = float(clean.quantile(0.05)) if len(clean) else 0.0
        zmax = float(clean.quantile(0.95)) if len(clean) else 1.0
    z = [None if pd.isna(v) else float(v) for v in vals]
    text = [f"{nm}<br>{m['label']}: {_fmt(m['fmt'], v)}"
            for nm, v in zip(df["name"], vals)]
    return z, text, zmin, zmax, m["scale"]


# Smooth heat ramps (ColorBrewer Spectral / RdYlGn). Color STOPS sit at data
# quantiles, so the gradient is both smooth AND evenly spread — no flat blocks,
# no one-shade map. This is how Zillow/Reventure heat maps read.
# Reventure-style ramps: two hues through a warm near-white mid — far easier to
# read at a glance than a rainbow (adjacent areas separate cleanly, extremes pop).
PALETTE_SEQ = ["#2166ac", "#4393c3", "#92c5de", "#f7f3e8", "#f4a582", "#d6604d", "#b2182b"]
PALETTE_DIV = ["#b2182b", "#d6604d", "#f4a582", "#f7f3e8", "#a6dba0", "#5aae61", "#1b7837"]
# Growth score: cream -> deep green (the Reventure growth-map look).
PALETTE_GROWTH = ["#f7f3e8", "#e3edcd", "#c5e0a5", "#9ccb78", "#6db354", "#3f9337", "#1a6e23"]
NO_DATA_COLOR = "#d4d4d4"  # neutral gray, clearly distinct from the cream mid-ramp


def _ramp(vals: pd.Series, m: dict):
    """(paint expr, zmin, zmax, legend) for one geography level's value pool.

    Color stops sit at evenly-spaced quantiles (pulled slightly off the extremes
    so outliers don't hog the ramp), with smooth interpolation between them.
    """
    if m.get("diverging"):
        colors = PALETTE_DIV
    elif m.get("scale") == "growth":
        colors = PALETTE_GROWTH
    else:
        colors = PALETTE_SEQ
    n = len(colors)
    stops = []
    for i in range(n):
        q = 0.02 + 0.96 * (i / (n - 1))
        s = float(vals.quantile(q)) if len(vals) else float(i)
        if stops and s <= stops[-1]:
            s = stops[-1] + 1e-6 * (abs(stops[-1]) + 1.0)
        stops.append(s)

    interp = ["interpolate", ["linear"], ["get", m["key"]]]
    for s, c in zip(stops, colors):
        interp += [s, c]
    expr = ["case", ["has", m["key"]], interp, NO_DATA_COLOR]

    legend = {"colors": colors, "lo": _fmt(m["fmt"], stops[0]),
              "mid": _fmt(m["fmt"], stops[n // 2]), "hi": _fmt(m["fmt"], stops[-1])}
    if len(vals):
        # Quartile edges for the clickable legend range filter (p2/q25/q50/q75/p98).
        legend["qs"] = [round(float(v), 2) for v in
                        (stops[0], vals.quantile(0.25), vals.quantile(0.5),
                         vals.quantile(0.75), stops[-1])]
    return expr, stops[0], stops[-1], legend


def _metric_render(df: pd.DataFrame, m: dict, zip_vals: Optional[dict] = None) -> dict:
    """Per-metric render config: county color ramp + (when ZIP values exist)
    a separate ZIP-level ramp, so ZIP colors spread across the ZIP distribution
    instead of saturating against the national county scale."""
    # ZIP-only metrics (e.g. Zillow's forecast) have no county column — counties
    # render gray via the ['has', key] paint case and the ramp comes from ZIPs.
    if m["key"] in df.columns:
        vals = pd.to_numeric(df[m["key"]], errors="coerce").dropna()
    else:
        vals = pd.Series(dtype=float)
    expr, zmin, zmax, legend = _ramp(vals, m)
    # Vigintiles power the "higher than N% of US counties" line in the stats panel.
    q = [round(float(vals.quantile(i / 20.0)), 2) for i in range(21)] if len(vals) else []
    out = {"key": m["key"], "label": m["label"], "fmt": m["fmt"],
           "group": m.get("group", "Other"), "desc": m.get("desc", ""), "q": q,
           "expr": expr, "zmin": zmin, "zmax": zmax, "legend": legend}

    zv = pd.Series((zip_vals or {}).get(m["key"], []), dtype=float).dropna()
    if len(zv) >= 20:
        zexpr, zzmin, zzmax, zlegend = _ramp(zv, m)
        out.update({"zexpr": zexpr, "zzmin": zzmin, "zzmax": zzmax, "zlegend": zlegend})
        if not len(vals):
            # No county data at all: show the ZIP ramp in the legend at every zoom,
            # and flag it so the legend never claims a "county scale" for ZIP data.
            out.update({"expr": zexpr, "zmin": zzmin, "zmax": zzmax,
                        "legend": zlegend, "zip_only": True})
    return out


def inject_zip_growth_scores(config: Config, zip_states) -> None:
    """Rank ALL mapped ZIPs together and write growth_score into each state's
    zips_*.json (must run before tiles/geo_index consume those files)."""
    files = {}
    rows = {}
    for st in zip_states or []:
        p = config.docs_dir / st["file"]
        if not p.exists():
            continue
        g = json.loads(p.read_text(encoding="utf-8"))
        files[st["file"]] = (p, g)
        for feat in g.get("features", []):
            z = feat.get("id")
            if z:
                rows[str(z)] = feat.get("properties") or {}
    if not rows:
        return
    vals = pd.DataFrame.from_dict(rows, orient="index")
    scores = _growth_score(vals, GROWTH_W_ZIP)
    scored = 0
    for _, (p, g) in files.items():
        for feat in g.get("features", []):
            s = scores.get(str(feat.get("id")))
            if s is not None and pd.notna(s):
                feat["properties"]["growth_score"] = int(s)
                scored += 1
        p.write_text(json.dumps(g, separators=(",", ":")), encoding="utf-8")
    log.info("[map] growth score injected for %d/%d ZIPs (ranked across all "
             "mapped states)", scored, len(rows))


def gather_zip_values(config: Config, zip_states) -> dict:
    """Pool every metric value across the written docs/zips_*.json files
    (feeds the ZIP-level color ramps)."""
    keyset = {m["key"] for m in METRICS}
    pool: dict = {}
    for st in zip_states or []:
        zf = config.docs_dir / st["file"]
        if not zf.exists():
            continue
        try:
            g = json.loads(zf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for feat in g.get("features", []):
            for k, v in (feat.get("properties") or {}).items():
                if k in keyset and isinstance(v, (int, float)):
                    pool.setdefault(k, []).append(v)
    return pool


# ZIP-level drill-down boundaries (OpenDataDE), per state the user tracks.
ZIP_GEOJSON_BASE = "https://raw.githubusercontent.com/OpenDataDE/State-zip-code-GeoJSON/master/"
STATE_ZIP_FILES = {
    "OH": "oh_ohio_zip_codes_geo.min.json",
    "CA": "ca_california_zip_codes_geo.min.json",
    "MI": "mi_michigan_zip_codes_geo.min.json",
    "TX": "tx_texas_zip_codes_geo.min.json",
    "FL": "fl_florida_zip_codes_geo.min.json",
    "NY": "ny_new_york_zip_codes_geo.min.json",
    "IN": "in_indiana_zip_codes_geo.min.json",
    "NV": "nv_nevada_zip_codes_geo.min.json",
}


def map_zip_state_codes(config: Config):
    """States that get a ZIP drill-down layer: every tracked market's state plus
    any extras listed in config output.map_zip_states."""
    codes = []
    for mk in config.markets.values():
        c = mk.county.state_abbr.upper()
        if c not in codes:
            codes.append(c)
    for c in config.output.get("map_zip_states", []) or []:
        c = str(c).upper()
        if c not in codes:
            codes.append(c)
    return codes


ZIP_SIMPLIFY_TOLERANCE = 0.003  # ~300 m; shapes stay recognizable, file stays small
ZIP_METRIC_KEYS = [
    "home_value", "home_value_12m_pct", "home_value_5y_pct", "home_value_mom_pct",
    "pct_from_peak", "rent", "rent_12m_pct", "price_to_rent", "gross_yield",
    "cap_rate", "exp_est", "median_income", "population",
    # county-only metrics (net_dom_mig_rate, agi_net_percap) stay out — honest geography
    "inbound_movers_pct", "value_income_ratio", "days_on_market",
    "price_cut_share", "inventory_yoy", "listings", "home_value_fc_12m",
    "property_tax_rate", "sec8_premium", "safmr_2br", "safmr_3br",
    "growth_score",
]
# County geo_index entries additionally carry the county-only metrics so the
# on-map labels can print them.
COUNTY_LABEL_KEYS = ZIP_METRIC_KEYS + [
    "net_dom_mig_rate", "agi_net_percap",
    "median_sale_price", "sold_above_list_pct", "months_supply",
    "constr_wage_index", "landlord_score", "hud_units_per_1k", "hcv_per_1k",
]


def _put(out, z, key, v, digits=None):
    if v is None or pd.isna(v):
        return
    out.setdefault(z, {})[key] = round(float(v), digits) if digits else round(float(v))


def _zip_values(config: Config, zips, state_abbr=""):
    """All map metrics per ZIP, from the national ZHVI/ZORI/ACS-ZCTA files."""
    raw = config.raw_dir
    out = {}
    zhvi = None

    # ZCTA property-tax rates first — they feed the per-ZIP cap-rate estimate.
    # Fallback chain: ZCTA rate -> state table (inside _investor_metrics).
    tax_by_zip = {}
    apath0 = last_cached(raw, "census_acs_zcta", "csv")
    if apath0:
        try:
            acs0 = pd.read_csv(apath0, dtype={"zcta": str}).drop_duplicates("zcta").set_index("zcta")
            rates = _tax_rate(acs0)
            for z in set(zips) & set(acs0.index):
                v = rates.get(z)
                if v is not None and pd.notna(v):
                    tax_by_zip[z] = float(v)
                    _put(out, z, "property_tax_rate", v, 2)
        except Exception as exc:  # noqa: BLE001
            log.warning("[map] ZCTA tax rates skipped: %s", exc)

    # HUD SAFMR (committed file) — consumed inside the ZORI block (needs rent).
    try:
        from .sources import hud
        safmr = hud.load_safmr(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] SAFMR skipped: %s", exc)
        safmr = None

    zpath = last_cached(raw, "zillow_zhvi_zip", "csv")
    if zpath:
        zhvi, zd = zillow.load_zip_series(zpath, zips, "Zillow ZHVI zip")
        zhvi = zhvi.set_index("RegionName"); zhvi = zhvi[~zhvi.index.duplicated()]
        vals = zhvi[zd].apply(pd.to_numeric, errors="coerce")
        latest = vals[zd[-1]]
        peak = vals.max(axis=1)
        for z in zhvi.index:
            v = latest.get(z)
            if v is None or pd.isna(v):
                continue
            _put(out, z, "home_value", v)
            if len(zd) >= 13:
                p = vals.at[z, zd[-13]]
                if pd.notna(p) and p > 0:
                    _put(out, z, "home_value_12m_pct", (v / p - 1.0) * 100.0, 1)
            if len(zd) >= 61:
                p = vals.at[z, zd[-61]]
                if pd.notna(p) and p > 0:
                    _put(out, z, "home_value_5y_pct", (v / p - 1.0) * 100.0, 1)
            if len(zd) >= 2:
                p = vals.at[z, zd[-2]]
                if pd.notna(p) and p > 0:
                    _put(out, z, "home_value_mom_pct", (v / p - 1.0) * 100.0, 2)
            pk = peak.get(z)
            if pd.notna(pk) and pk > 0:
                _put(out, z, "pct_from_peak", (v / pk - 1.0) * 100.0, 1)

    rpath = last_cached(raw, "zillow_zori_zip", "csv")
    if rpath:
        zori, zod = zillow.load_zip_series(rpath, zips, "Zillow ZORI zip")
        zori = zori.set_index("RegionName"); zori = zori[~zori.index.duplicated()]
        rvals = zori[zod].apply(pd.to_numeric, errors="coerce")
        rlatest = rvals[zod[-1]]
        for z in zori.index:
            r = rlatest.get(z)
            if r is None or pd.isna(r) or r <= 0:
                continue
            _put(out, z, "rent", r)
            if len(zod) >= 13:
                p = rvals.at[z, zod[-13]]
                if pd.notna(p) and p > 0:
                    _put(out, z, "rent_12m_pct", (r / p - 1.0) * 100.0, 1)
            hv = out.get(z, {}).get("home_value")
            if hv:
                _put(out, z, "price_to_rent", hv / (float(r) * 12.0), 1)
                gy, cap, exp = _investor_metrics(hv, r, state_abbr, tax_by_zip.get(z))
                if gy is not None:
                    out[z]["gross_yield"] = gy
                    out[z]["cap_rate"] = cap
                    out[z]["exp_est"] = exp
            # Section 8: HUD voucher ceiling vs market rent (needs rent, so here).
            if safmr is not None and z in safmr.index:
                s2 = safmr.at[z, "safmr_2br"]
                s3 = safmr.at[z, "safmr_3br"]
                if pd.notna(s2):
                    _put(out, z, "safmr_2br", s2)
                    _put(out, z, "sec8_premium", 100.0 * (float(s2) / float(r) - 1.0), 1)
                if pd.notna(s3):
                    _put(out, z, "safmr_3br", s3)

    # ZIP-level income + population + movers (Census ACS ZCTA file, national).
    apath = last_cached(raw, "census_acs_zcta", "csv")
    if apath:
        acs = pd.read_csv(apath, dtype={"zcta": str}).drop_duplicates("zcta").set_index("zcta")
        movers = _movers_pct(acs)
        wanted = set(zips) & set(acs.index)
        for z in wanted:
            inc = pd.to_numeric(acs.at[z, "median_household_income"], errors="coerce")
            _put(out, z, "median_income", inc)
            _put(out, z, "population", pd.to_numeric(acs.at[z, "population"], errors="coerce"))
            _put(out, z, "inbound_movers_pct", movers.get(z), 1)
            hv = out.get(z, {}).get("home_value")
            if hv and pd.notna(inc) and inc > 0:
                _put(out, z, "value_income_ratio", hv / float(inc), 1)

    # Realtor.com market-heat trio (ZIP, monthly; thin ZIPs pre-masked).
    try:
        from .sources import realtor
        heat = realtor.load_zip_metrics(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] Realtor ZIP metrics skipped: %s", exc)
        heat = None
    if heat is not None:
        for z in set(zips) & set(heat.index):
            row = heat.loc[z]
            _put(out, z, "days_on_market", row["days_on_market"])
            _put(out, z, "price_cut_share", row["price_cut_share"], 1)
            _put(out, z, "inventory_yoy", row["inventory_yoy"], 1)
            _put(out, z, "listings", row["listings"])

    # Zillow 1-yr home-value forecast (ZIP only; the file's horizon columns are
    # date-named and shift monthly — the last sorted date is the 12-month one).
    fpath = last_cached(raw, "zillow_zhvf_zip", "csv")
    if fpath:
        try:
            fc, fdates = zillow.load_zip_series(fpath, zips, "Zillow ZHVF zip")
            fc = fc.set_index("RegionName"); fc = fc[~fc.index.duplicated()]
            col = sorted(fdates)[-1]
            for z, v in pd.to_numeric(fc[col], errors="coerce").items():
                _put(out, z, "home_value_fc_12m", v, 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("[map] ZHVF forecast skipped: %s", exc)
    return out


def build_zip_layer(config: Config, state_abbr: str):
    """Fetch + simplify a state's ZIP boundaries, merge values, write docs/zips_xx.json."""
    try:
        from shapely.geometry import mapping, shape
    except ImportError:
        log.warning("[map] shapely not installed — ZIP drill-down skipped")
        return None
    fname = STATE_ZIP_FILES.get(state_abbr.upper())
    if not fname:
        log.warning("[map] no ZIP boundary source mapped for state %s — skipping", state_abbr)
        return None

    raw = config.raw_dir
    cache_name = f"zips_raw_{state_abbr.lower()}"
    path = last_cached(raw, cache_name, "json")
    if not path:
        log.info("[map] downloading %s ZIP boundaries (one-time, large)", state_abbr)
        path = cache_path(raw, cache_name, "json")
        path.write_text(get_text(ZIP_GEOJSON_BASE + fname), encoding="utf-8")
    g = json.loads(path.read_text(encoding="utf-8"))

    zips = [f["properties"]["ZCTA5CE10"] for f in g["features"]]
    values = _zip_values(config, zips, state_abbr)

    feats = []
    for f in g["features"]:
        z = f["properties"]["ZCTA5CE10"]
        geom = shape(f["geometry"]).simplify(ZIP_SIMPLIFY_TOLERANCE, preserve_topology=True)
        if geom.is_empty:
            continue
        props = {"cname": "ZIP " + z}
        for k in ZIP_METRIC_KEYS:
            if values.get(z, {}).get(k) is not None:
                props[k] = values[z][k]
        feats.append({"type": "Feature", "id": z, "properties": props, "geometry": mapping(geom)})

    dest = config.docs_dir / f"zips_{state_abbr.lower()}.json"
    dest.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                               separators=(",", ":")), encoding="utf-8")
    log.info("[map] wrote %s (%d ZIPs, %.1f MB)", dest.name, len(feats), dest.stat().st_size / 1e6)
    return {"code": state_abbr.lower(), "file": dest.name, "count": len(feats)}


def _geom_bbox(coords) -> List[float]:
    """[w, s, e, n] bounding box of a GeoJSON coordinates array (pure Python)."""
    box = [180.0, 90.0, -180.0, -90.0]

    def visit(c):
        if isinstance(c[0], (int, float)):
            if c[0] < box[0]:
                box[0] = c[0]
            if c[0] > box[2]:
                box[2] = c[0]
            if c[1] < box[1]:
                box[1] = c[1]
            if c[1] > box[3]:
                box[3] = c[1]
        else:
            for cc in c:
                visit(cc)

    visit(coords)
    return box


INT_KEYS = {"home_value", "rent", "exp_est", "median_income", "population",
            "median_sale_price", "safmr_2br", "safmr_3br", "growth_score"}


def _entry_values(props: dict, keys=None) -> dict:
    """Rounded metric values for a geo_index entry (powers the on-map labels)."""
    out = {}
    for k in (keys or ZIP_METRIC_KEYS):
        v = props.get(k)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        out[k] = round(float(v)) if k in INT_KEYS else round(float(v), 2)
    return out


def _centroid(geom: dict, bbox) -> list:
    """A label point inside the shape (shapely representative_point when
    available, bbox center otherwise)."""
    try:
        from shapely.geometry import shape
        p = shape(geom).representative_point()
        return [round(p.x, 3), round(p.y, 3)]
    except Exception:  # noqa: BLE001
        return [round((bbox[0] + bbox[2]) / 2, 3), round((bbox[1] + bbox[3]) / 2, 3)]


def build_geo_index(config: Config, df: pd.DataFrame, geojson: Optional[dict] = None,
                    zip_states: Optional[list] = None) -> Optional[Path]:
    """Write docs/geo_index.json — search index + label points for the map.

    Entries: {"n": name, "b": [w,s,e,n], "c": [x,y], "t": "c"|"z"|"a",
    "v": {metric: value}} — county, ZIP, or market alias (so typing "Toledo"
    finds Lucas County). Static, offline, free.

    Only the zip files for CURRENTLY configured states are indexed (a glob would
    resurrect a removed market's stale values as current-looking labels).
    """
    if zip_states is None:
        states = [c.lower() for c in map_zip_state_codes(config)]
        zip_states = [{"code": s, "file": "zips_%s.json" % s} for s in states]
    try:
        if geojson is None:
            geojson = _load_geojson(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] geo index skipped (no county geojson): %s", exc)
        return None

    name_by_fips = df["name"].to_dict()
    entries, county_bbox = [], {}
    for feat in geojson.get("features", []):
        fips = feat.get("id")
        nm = name_by_fips.get(fips)
        geom = feat.get("geometry") or {}
        if not nm or not geom.get("coordinates"):
            continue
        try:
            b = [round(v, 3) for v in _geom_bbox(geom["coordinates"])]
        except Exception:  # noqa: BLE001
            continue
        county_bbox[fips] = b
        row = df.loc[fips]
        entries.append({"n": nm, "b": b, "c": _centroid(geom, b), "t": "c",
                        "v": _entry_values(row.to_dict(), COUNTY_LABEL_KEYS)})

    for zst in zip_states:
        zfile = config.docs_dir / zst["file"]
        if not zfile.exists():
            continue
        st = zst["code"].upper()
        try:
            g = json.loads(zfile.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for feat in g.get("features", []):
            props = feat.get("properties") or {}
            nm = props.get("cname")
            geom = feat.get("geometry") or {}
            if not nm or not geom.get("coordinates"):
                continue
            try:
                b = [round(v, 3) for v in _geom_bbox(geom["coordinates"])]
            except Exception:  # noqa: BLE001
                continue
            entries.append({"n": nm, "s": st, "b": b, "c": _centroid(geom, b),
                            "t": "z", "v": _entry_values(props)})

    for mk in config.markets.values():
        b = county_bbox.get(mk.county.fips)
        cname = name_by_fips.get(mk.county.fips)
        if b and cname:
            entries.append({"n": mk.name, "cname": cname, "b": b, "t": "a"})

    dest = config.docs_dir / "geo_index.json"
    dest.write_text(json.dumps(entries, separators=(",", ":")), encoding="utf-8")
    log.info("[map] wrote %s (%d entries, %.0f KB)", dest.name, len(entries),
             dest.stat().st_size / 1024)
    return dest


def load_landlord_state_scores() -> dict:
    """{state: 0-10 score} from the committed editorial index ({} on failure)."""
    try:
        data = json.loads((TEMPLATE_DIR.parent / "data" / "landlord_scores.json")
                          .read_text(encoding="utf-8"))
        return {st: v.get("score") for st, v in data.get("states", {}).items()
                if v.get("score") is not None}
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] landlord score file unreadable: %s", exc)
        return {}


def _find_tippecanoe() -> Optional[str]:
    p = shutil.which("tippecanoe")
    if p:
        return p
    fallback = Path.home() / ".recmon-tools" / "tippecanoe" / "tippecanoe"
    return str(fallback) if fallback.exists() else None


def _generate_tiles(config: Config, geojson: dict, zip_states: list) -> bool:
    """Bake county + ZIP vector tiles (PMTiles) with tippecanoe. Best-effort:
    if tippecanoe is absent (e.g. a machine without it), keep the committed tiles."""
    tip = _find_tippecanoe()
    if not tip:
        log.warning("[map] tippecanoe not found — keeping existing PMTiles "
                    "(install/build tippecanoe to regenerate)")
        return False
    build = config.raw_dir / "tilebuild"
    build.mkdir(parents=True, exist_ok=True)
    docs = config.docs_dir

    cpath = build / "counties_props.geojson"
    cpath.write_text(json.dumps(geojson, separators=(",", ":")), encoding="utf-8")
    subprocess.run([tip, "-o", str(docs / "counties.pmtiles"), "-Z0", "-z9",
                    "-l", "counties", "-r1", "--no-tile-size-limit", "--force", str(cpath)],
                   check=True, capture_output=True)
    log.info("[map] baked counties.pmtiles")

    if zip_states:
        allz = {"type": "FeatureCollection", "features": []}
        for st in zip_states:
            zf = docs / st["file"]
            if zf.exists():
                allz["features"] += json.loads(zf.read_text(encoding="utf-8"))["features"]
        zpath = build / "zips_all.geojson"
        zpath.write_text(json.dumps(allz, separators=(",", ":")), encoding="utf-8")
        subprocess.run([tip, "-o", str(docs / "zips.pmtiles"), "-Z5", "-z12",
                        "-l", "zips", "-r1", "--no-tile-size-limit", "--force", str(zpath)],
                       check=True, capture_output=True)
        log.info("[map] baked zips.pmtiles (%d ZIPs)", len(allz["features"]))
    return True


def render_map_page(config: Config) -> Path:
    df = build_county_table(config)
    geojson = _load_geojson(config)

    # Build ZIP drill-down layers for each state the user tracks.
    zip_states = []
    for code in map_zip_state_codes(config):
        try:
            info = build_zip_layer(config, code)
            if info:
                zip_states.append(info)
        except Exception as exc:  # noqa: BLE001
            log.warning("[map] ZIP layer for %s failed: %s", code, exc)
    # Growth score ranks across ALL mapped ZIPs — must land in the state jsons
    # before the tile bake and geo index read them.
    try:
        inject_zip_growth_scores(config, zip_states)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] ZIP growth scores skipped: %s", exc)

    tracked_fips = {mk.county.fips for mk in config.markets.values()}
    name_by_fips = df["name"].to_dict()
    # exp_est powers the cap-rate tooltip; listings the market-heat sample-size
    # line; the SAFMR dollar figures the Section 8 tooltip.
    keys = [m["key"] for m in METRICS] + ["exp_est", "listings", "safmr_2br", "safmr_3br"]
    # Merge per-county values + name into each feature's properties (both the
    # MapLibre tile map and the plotly fallback read straight from these).
    for feat in geojson.get("features", []):
        fips = feat.get("id")
        props = {"cname": name_by_fips.get(fips, ""), "tracked": fips in tracked_fips}
        if fips in df.index:
            row = df.loc[fips]
            for k in keys:
                v = row.get(k)
                if pd.notna(v):
                    props[k] = float(v)
        feat["properties"] = props

    zip_pool = gather_zip_values(config, zip_states)
    metrics_cfg = [_metric_render(df, m, zip_pool) for m in METRICS]
    coverage = {
        "home_value": int(df["home_value"].notna().sum()),
        "rent": int(df["rent"].notna().sum()),
        "income": int(df["median_income"].notna().sum()),
        "total": len(df),
    }

    # Bake the streaming vector tiles (best-effort; keeps committed tiles if no tippecanoe).
    try:
        _generate_tiles(config, geojson, zip_states)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] tile generation failed (%s) — keeping existing PMTiles", exc)

    # Drop zip files for markets no longer tracked, so nothing stale is deployed.
    wanted = {st["file"] for st in zip_states}
    for f in config.docs_dir.glob("zips_*.json"):
        if f.name not in wanted:
            f.unlink()
            log.info("[map] removed stale %s (market no longer tracked)", f.name)

    # Client-side search index (best-effort; the map degrades gracefully without it).
    try:
        build_geo_index(config, df, geojson, zip_states)
    except Exception as exc:  # noqa: BLE001
        log.warning("[map] geo index failed: %s", exc)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html"]))
    html = env.get_template("map.html.j2").render(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        coverage=coverage, tracked=sorted(df[df["is_tracked"]]["name"].tolist()),
        metrics=[{"key": m["key"], "label": m["label"]} for m in METRICS],
        metrics_json=json.dumps(metrics_cfg, separators=(",", ":")),
        landlord_json=json.dumps(load_landlord_state_scores(), separators=(",", ":")),
        zip_states=zip_states,
    )
    dest = config.docs_dir / "map.html"
    dest.write_text(html, encoding="utf-8")
    log.info("[map] wrote %s (PMTiles vector map)", dest)
    return dest
