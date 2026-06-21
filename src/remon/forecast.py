"""Transparent public-data home-price forecasts. Two methods, both backtested.

This is NOT any paid service's score and not a guarantee. Every forecast carries
its own backtest error, and ZIPs with too little history are skipped, never
extrapolated.

Method 1 — per-ZIP time series: ETS (Holt-Winters family) on each ZIP's full
  monthly ZHVI history; 12- and 24-month point forecasts with 80% prediction
  intervals; backtested by holding out the last 12 months (MAPE).
  (ZHVI here is seasonally adjusted, so an additive damped-trend ETS without a
  seasonal term is the right specification.)

Method 2 — national driver model: a panel of several hundred ZIPs (ZHVI + ZORI +
  the national mortgage rate) is built and a gradient-boosted model is trained to
  predict the forward 12-month ZHVI change, backtested OUT OF TIME (train on older
  months, test on newer). Applied to the tracked ZIPs with a band from the
  backtest residuals, plus feature importances so the drivers are visible.
  Honest scope note: features are momentum (3/6/12-mo), price-to-rent (where ZORI
  exists), the 30-yr mortgage rate and its 12-mo change, and price level relative
  to the national median. Income / migration / inventory are county-or-metro and
  annual, so they are not used at national ZIP-month resolution here.
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import Config
from .http import last_cached
from .logging_setup import get_logger
from .sources import fred, zillow

warnings.simplefilter("ignore")  # statsmodels convergence chatter
log = get_logger("remon.forecast")

MIN_MONTHS_TS = 48          # need >=4 yrs to fit a per-ZIP model
BACKTEST_MONTHS = 12
HORIZONS = (12, 24)
PANEL_MAX_ZIPS = 400        # national training sample size
PANEL_MIN_MONTHS = 120


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _series(row: pd.Series, date_cols: List[str]) -> pd.Series:
    s = pd.Series({pd.to_datetime(d): row[d] for d in date_cols}, dtype="float64").dropna()
    s = s.sort_index()
    s.index = s.index.to_period("M").to_timestamp("M")
    return s


# --------------------------------------------------------------------------- #
# Method 1 — per-ZIP time series
# --------------------------------------------------------------------------- #
def forecast_ts(s: pd.Series) -> Optional[dict]:
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    s = s.dropna().astype(float)
    if len(s) < MIN_MONTHS_TS:
        return None
    s = s.asfreq("M")
    s = s.interpolate(limit_direction="both")

    def fit(series):
        return ETSModel(series, error="add", trend="add", damped_trend=True,
                        seasonal=None).fit(disp=False)

    # Backtest: hold out last 12 months
    mape = None
    if len(s) >= MIN_MONTHS_TS + BACKTEST_MONTHS:
        try:
            m_bt = fit(s.iloc[:-BACKTEST_MONTHS])
            fc = np.asarray(m_bt.forecast(BACKTEST_MONTHS))
            actual = s.iloc[-BACKTEST_MONTHS:].values
            mape = float(np.mean(np.abs(actual - fc) / np.abs(actual)) * 100)
        except Exception as exc:  # noqa: BLE001
            log.debug("backtest failed: %s", exc)

    try:
        model = fit(s)
        pred = model.get_prediction(start=len(s), end=len(s) + max(HORIZONS) - 1)
        sf = pred.summary_frame(alpha=0.2)  # 80% interval
    except Exception as exc:  # noqa: BLE001
        log.debug("fit/forecast failed: %s", exc)
        return None

    last = float(s.iloc[-1])
    out = {"n_months": int(len(s)), "mape": mape, "last": last, "horizons": {}}
    for h in HORIZONS:
        mean = float(sf["mean"].iloc[h - 1])
        out["horizons"][str(h)] = {
            "point": mean,
            "lo": float(sf["pi_lower"].iloc[h - 1]),
            "hi": float(sf["pi_upper"].iloc[h - 1]),
            "pct": (mean / last - 1) * 100 if last else None,
        }
    return out


# --------------------------------------------------------------------------- #
# Method 2 — national driver model
# --------------------------------------------------------------------------- #
FEATURES = ["mom3", "mom6", "mom12", "ptr", "has_ptr", "mort", "mort_chg12", "rel_level"]


def _zip_panel(z, hv: pd.Series, zo: Optional[pd.Series], mort: pd.Series,
               nat_med: pd.Series, every: int = 3) -> Optional[pd.DataFrame]:
    if len(hv) < PANEL_MIN_MONTHS:
        return None
    df = pd.DataFrame({"hv": hv})
    df["mom3"] = hv / hv.shift(3) - 1
    df["mom6"] = hv / hv.shift(6) - 1
    df["mom12"] = hv / hv.shift(12) - 1
    if zo is not None and not zo.empty:
        zoa = zo.reindex(df.index)
        df["ptr"] = df["hv"] / (zoa * 12)
    else:
        df["ptr"] = np.nan
    df["has_ptr"] = df["ptr"].notna().astype(float)
    df["mort"] = mort.reindex(df.index)
    df["mort_chg12"] = df["mort"] - df["mort"].shift(12)
    df["rel_level"] = df["hv"] / nat_med.reindex(df.index)
    df["target"] = hv.shift(-12) / hv - 1
    df["zip"] = z
    df = df.iloc[::1]  # keep monthly; sample below at panel level
    return df.iloc[12:]  # drop the first year (lags undefined)


def train_driver(config: Config, zhvi, zhvi_dates, zori, zori_dates, mort) -> Optional[dict]:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error

    nat_med = _series(pd.Series(zhvi[zhvi_dates].median(axis=0), index=zhvi_dates), zhvi_dates)
    # Pick the largest markets (lowest SizeRank) with enough history.
    cand = zhvi.sort_values("SizeRank") if "SizeRank" in zhvi.columns else zhvi
    panels = []
    used = 0
    zori_idx = set(zori.index) if zori is not None else set()
    for z, row in cand.iterrows():
        if used >= PANEL_MAX_ZIPS:
            break
        hv = _series(row, zhvi_dates)
        if len(hv) < PANEL_MIN_MONTHS:
            continue
        zo = _series(zori.loc[z], zori_dates) if z in zori_idx else None
        p = _zip_panel(z, hv, zo, mort, nat_med)
        if p is not None:
            panels.append(p)
            used += 1
    if used < 50:
        log.warning("driver model: only %d ZIPs in panel — skipping", used)
        return None

    panel = pd.concat(panels)
    panel = panel.dropna(subset=["mom12", "mort", "rel_level", "target"])
    panel["ptr"] = panel["ptr"].fillna(panel["ptr"].median())
    panel = panel.reset_index().rename(columns={"index": "date"})
    panel = panel[panel["date"].dt.month % 3 == 0]  # quarterly snapshots — speed

    # Out-of-time split: newest 24 months of (realized-target) obs are the test set.
    cutoff = panel["date"].max() - pd.DateOffset(months=24)
    train = panel[panel["date"] <= cutoff]
    test = panel[panel["date"] > cutoff]
    if len(train) < 500 or len(test) < 100:
        log.warning("driver model: insufficient panel (train=%d test=%d)", len(train), len(test))
        return None

    model = HistGradientBoostingRegressor(max_depth=3, max_iter=300, learning_rate=0.05,
                                          l2_regularization=1.0, random_state=0)
    model.fit(train[FEATURES], train["target"])
    pred = model.predict(test[FEATURES])
    mae_pp = float(mean_absolute_error(test["target"], pred) * 100)
    resid_std = float(np.std(test["target"].values - pred))
    # directional accuracy
    dir_acc = float(np.mean(np.sign(pred) == np.sign(test["target"].values)) * 100)

    # permutation-style importance via sklearn
    from sklearn.inspection import permutation_importance
    imp = permutation_importance(model, test[FEATURES], test["target"], n_repeats=5, random_state=0)
    importances = sorted(zip(FEATURES, imp.importances_mean), key=lambda x: -x[1])

    log.info("driver model: panel=%d ZIPs, train=%d test=%d rows, test MAE=%.2fpp, dir-acc=%.0f%%",
             used, len(train), len(test), mae_pp, dir_acc)
    return {
        "model": model, "mae_pp": mae_pp, "resid_std": resid_std, "dir_acc": dir_acc,
        "n_zips": used, "n_train": int(len(train)), "n_test": int(len(test)),
        "importances": [(f, float(v)) for f, v in importances],
        "nat_med": nat_med, "mort": mort,
    }


def driver_predict(driver: dict, z, hv: pd.Series, zo: Optional[pd.Series]) -> Optional[dict]:
    if driver is None or len(hv) < 13:
        return None
    last = hv.index[-1]
    feat = {
        "mom3": hv.iloc[-1] / hv.iloc[-4] - 1 if len(hv) > 3 else np.nan,
        "mom6": hv.iloc[-1] / hv.iloc[-7] - 1 if len(hv) > 6 else np.nan,
        "mom12": hv.iloc[-1] / hv.iloc[-13] - 1,
        "ptr": (hv.iloc[-1] / (zo.reindex([last]).iloc[0] * 12)) if (zo is not None and last in zo.index) else np.nan,
        "mort": driver["mort"].reindex([last]).iloc[0],
        "mort_chg12": np.nan,
        "rel_level": hv.iloc[-1] / driver["nat_med"].reindex([last]).iloc[0],
    }
    m = driver["mort"]
    if last in m.index and (last - pd.DateOffset(months=12)) in m.index:
        feat["mort_chg12"] = m.loc[last] - m.loc[last - pd.DateOffset(months=12)]
    feat["has_ptr"] = 1.0 if not np.isnan(feat["ptr"]) else 0.0
    X = pd.DataFrame([{k: feat.get(k, np.nan) for k in FEATURES}])
    if X[["mom12", "mort", "rel_level"]].isna().any(axis=1).iloc[0]:
        return None
    X["ptr"] = X["ptr"].fillna(0.0)
    X["mort_chg12"] = X["mort_chg12"].fillna(0.0)
    pct = float(driver["model"].predict(X[FEATURES])[0] * 100)
    band = driver["resid_std"] * 100
    return {"pct": pct, "lo_pct": pct - band, "hi_pct": pct + band, "mae_pp": driver["mae_pp"]}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build_forecasts(config: Config) -> dict:
    raw = config.raw_dir

    # Load full national ZHVI (no ZIP filter) for the training panel.
    zdf = pd.read_csv(last_cached(raw, "zillow_zhvi_zip", "csv"),
                      dtype={"RegionName": str}, low_memory=False)
    zdf["RegionName"] = zdf["RegionName"].astype(str).str.zfill(5)
    zdf = zdf.set_index("RegionName")
    zhvi_dates = [c for c in zdf.columns if zillow.DATE_COL_RE.match(str(c))]

    zodf = pd.read_csv(last_cached(raw, "zillow_zori_zip", "csv"),
                       dtype={"RegionName": str}, low_memory=False)
    zodf["RegionName"] = zodf["RegionName"].astype(str).str.zfill(5)
    zodf = zodf.set_index("RegionName")
    zori_dates = [c for c in zodf.columns if zillow.DATE_COL_RE.match(str(c))]

    # National monthly mortgage rate.
    mort_path = last_cached(raw, "fred_MORTGAGE30US", "csv")
    mser = fred.load_series(mort_path, "FRED:MORTGAGE30US")
    mort = mser.set_index("date")["value"].astype(float)
    mort.index = mort.index.to_period("M").to_timestamp("M")
    mort = mort.groupby(level=0).mean()

    log.info("Training national driver model...")
    driver = train_driver(config, zdf, zhvi_dates, zodf, zori_dates, mort)

    results: dict = {"zips": {}, "driver_meta": None}
    if driver:
        results["driver_meta"] = {
            "mae_pp": driver["mae_pp"], "dir_acc": driver["dir_acc"],
            "n_zips": driver["n_zips"], "n_train": driver["n_train"], "n_test": driver["n_test"],
            "importances": driver["importances"],
        }

    skipped = []
    for m in config.markets.values():
        for z in m.zips:
            if z not in zdf.index:
                skipped.append(z)
                continue
            hv = _series(zdf.loc[z], zhvi_dates)
            zo = _series(zodf.loc[z], zori_dates) if z in zodf.index else None
            ts = forecast_ts(hv)
            drv = driver_predict(driver, z, hv, zo) if driver else None
            if ts is None and drv is None:
                skipped.append(z)
                continue
            blend = None
            if ts and drv and ts["horizons"].get("12"):
                blend = round((ts["horizons"]["12"]["pct"] + drv["pct"]) / 2, 1)
            results["zips"][z] = {"ts": ts, "driver": drv, "blend": blend}

    log.info("Forecasts: %d ZIPs modeled, %d skipped (too little history)",
             len(results["zips"]), len(skipped))
    results["skipped"] = skipped
    return results
