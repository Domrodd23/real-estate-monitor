"""Cached, retry-with-backoff HTTP for downloads and page fetches.

Implements two hard requirements:
  * #5 cache raw downloads with a date stamp so a failed compute step does not
    force a re-download (and a fresh cache is reused).
  * robustness: every download is wrapped in retry-with-backoff; callers can
    fall back to the last good cached copy if a source is unreachable.

Dependency-light: retry/backoff is hand-rolled (no extra package).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests

from .logging_setup import get_logger

log = get_logger("remon.http")

DEFAULT_HEADERS = {
    # A real browser UA: some public sites (e.g. zillow.com) 403 non-browser
    # agents and datacenter IPs. We only read public data pages / files.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_TIMEOUT = 60  # seconds


class DownloadError(RuntimeError):
    """Raised when a download fails after all retries."""


def _stamp(when: Optional[datetime] = None) -> str:
    return (when or datetime.now()).strftime("%Y%m%d")


def cache_path(raw_dir: Path, name: str, ext: str, when: Optional[datetime] = None) -> Path:
    """Date-stamped path inside the raw cache, e.g. data/raw/zhvi_zip_20260620.csv."""
    ext = ext.lstrip(".")
    return Path(raw_dir) / f"{name}_{_stamp(when)}.{ext}"


def find_cached(raw_dir: Path, name: str, ext: str, max_age_days: int) -> Optional[Path]:
    """Return the newest cached file for `name` within max_age_days, else None.

    Used so a failed run, or a same-month re-run, reuses raw data instead of
    re-downloading tens of MB. Set env REMON_NO_CACHE=1 to force a fresh fetch.
    """
    if os.environ.get("REMON_NO_CACHE", "").strip() not in ("", "0", "false", "False"):
        return None
    ext = ext.lstrip(".")
    candidates = sorted(
        Path(raw_dir).glob(f"{name}_*.{ext}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    newest = candidates[0]
    age = datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)
    if age <= timedelta(days=max_age_days):
        return newest
    return None


def last_cached(raw_dir: Path, name: str, ext: str) -> Optional[Path]:
    """Return the newest cached file for `name` regardless of age, else None.

    Used for graceful degradation: if a source is unreachable, fall back to the
    last good copy and mark it stale in the dashboard footer.
    """
    ext = ext.lstrip(".")
    candidates = sorted(
        Path(raw_dir).glob(f"{name}_*.{ext}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _request(
    method: str,
    url: str,
    *,
    retries: int = 4,
    backoff: float = 2.0,
    timeout: int = DEFAULT_TIMEOUT,
    **kwargs,
) -> requests.Response:
    """HTTP request with retry-with-backoff. Raises DownloadError on final fail."""
    headers = {**DEFAULT_HEADERS, **kwargs.pop("headers", {})}
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, timeout=timeout, **kwargs
            )
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 — we retry on any transport error
            last_exc = exc
            wait = backoff ** (attempt - 1)
            log.warning(
                "%s %s failed (attempt %d/%d): %s — retrying in %.1fs",
                method, url, attempt, retries, exc, wait,
            )
            if attempt < retries:
                time.sleep(wait)
    raise DownloadError(f"{method} {url} failed after {retries} attempts: {last_exc}")


def get_text(url: str, **kwargs) -> str:
    """Fetch a page as text (used to discover current Zillow/Redfin file links)."""
    log.info("GET (text) %s", url)
    return _request("GET", url, **kwargs).text


def get_json(url: str, **kwargs):
    """Fetch and parse JSON (used for FRED / Census APIs)."""
    log.info("GET (json) %s", url)
    return _request("GET", url, **kwargs).json()


def url_resolves(url: str, timeout: int = 20) -> bool:
    """Confirm a URL resolves before we try to parse it (spec requirement)."""
    try:
        resp = _request("HEAD", url, retries=2, timeout=timeout)
        return resp.status_code < 400
    except DownloadError:
        # Some hosts reject HEAD; try a ranged GET of the first byte.
        try:
            resp = _request(
                "GET", url, retries=2, timeout=timeout, headers={"Range": "bytes=0-0"},
                stream=True,
            )
            resp.close()
            return resp.status_code < 400
        except DownloadError:
            return False


def download(url: str, dest: Path, *, chunk: int = 1 << 16, **kwargs) -> Path:
    """Stream a URL to `dest` (date-stamped path), with retry-with-backoff.

    Writes to a temp file first, then renames, so an interrupted download never
    leaves a half-written cache file that looks valid.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("Downloading %s -> %s", url, dest.name)
    resp = _request("GET", url, stream=True, **kwargs)
    total = 0
    with open(tmp, "wb") as fh:
        for block in resp.iter_content(chunk_size=chunk):
            if block:
                fh.write(block)
                total += len(block)
    tmp.replace(dest)
    log.info("Saved %s (%.1f MB)", dest.name, total / 1e6)
    return dest
