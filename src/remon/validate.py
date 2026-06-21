"""Name-based column matching and dataframe validation.

Hard constraint #3: match columns by NAME, never by position, and fail loudly
if an expected column name is missing — Zillow/Redfin rename and reorder
columns periodically and a position-based read silently corrupts output.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

import pandas as pd

from .logging_setup import get_logger

log = get_logger("remon.validate")


class DataValidationError(ValueError):
    """Raised when loaded data fails a validation check. Names the source."""


def require_columns(df: pd.DataFrame, columns: Iterable[str], source: str) -> None:
    """Fail loudly if any expected column NAME is missing from `df`."""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise DataValidationError(
            f"[{source}] expected column(s) missing by name: {missing}. "
            f"Found columns: {list(df.columns)[:25]}"
            f"{'...' if len(df.columns) > 25 else ''}"
        )


def find_column(df: pd.DataFrame, candidates: Iterable[str], source: str) -> str:
    """Return the first matching column name (case-insensitive), or fail loudly.

    Use when a source publishes one of several known names for the same field.
    """
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand in df.columns:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    raise DataValidationError(
        f"[{source}] none of the expected column names {list(candidates)} "
        f"were found. Found: {list(df.columns)[:25]}"
    )


def validate_frame(
    df: pd.DataFrame,
    source: str,
    *,
    required_columns: Optional[Iterable[str]] = None,
    metric_columns: Optional[Iterable[str]] = None,
    min_rows: int = 1,
) -> pd.DataFrame:
    """Validate a freshly loaded dataframe. Raises DataValidationError on failure.

    Checks: row count >= min_rows; required columns present (by name); no metric
    column is entirely null.
    """
    if df is None:
        raise DataValidationError(f"[{source}] dataframe is None")
    if len(df) < min_rows:
        raise DataValidationError(
            f"[{source}] row count {len(df)} below minimum {min_rows}"
        )
    if required_columns:
        require_columns(df, required_columns, source)
    if metric_columns:
        all_null = [c for c in metric_columns if c in df.columns and df[c].isna().all()]
        if all_null:
            raise DataValidationError(
                f"[{source}] metric column(s) are entirely null: {all_null}"
            )
    log.info("[%s] validated: %d rows, %d columns", source, len(df), df.shape[1])
    return df


def parse_dates(df: pd.DataFrame, column: str, source: str) -> pd.Series:
    """Parse a date column, failing loudly if too many values won't parse."""
    if column not in df.columns:
        raise DataValidationError(f"[{source}] date column '{column}' not found")
    parsed = pd.to_datetime(df[column], errors="coerce")
    bad = parsed.isna().sum()
    if bad and bad == len(parsed):
        raise DataValidationError(f"[{source}] date column '{column}' did not parse")
    if bad:
        log.warning("[%s] %d/%d values in '%s' did not parse as dates",
                    source, bad, len(parsed), column)
    return parsed
