"""
Nifty 500 universe fetcher with local cache.

SURVIVORSHIP BIAS WARNING:
    This module fetches the CURRENT Nifty 500 constituent list. Stocks that
    were added to or removed from the index between 2022 and today will not be
    accurately reflected in historical analysis. This is a known, accepted
    limitation documented in README.md and shown in the backtest UI.
    Do not treat backtest results as survivorship-bias-free.

NSE fetch notes:
    NSE blocks requests without browser-like headers and an active session
    cookie (acquired by hitting the landing page first). This module handles
    that automatically. If the live fetch fails (e.g., 403, timeout), it
    falls back to the local cache. If no cache exists, it raises with clear
    instructions for the manual fallback.
"""
import io
import logging
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd
from tenacity import (
    retry, stop_after_attempt, wait_exponential, before_sleep_log
)

logger = logging.getLogger(__name__)

NSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "DNT": "1",
    "Connection": "keep-alive",
}

# universe.py is at: <root>/52WeekHigh/backtest/universe.py
# So parent×3 = project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = _PROJECT_ROOT / "data" / "cache"
_CACHE_FILE = _CACHE_DIR / "nifty500.csv"
_CACHE_MAX_AGE_HOURS = 24


def _cache_age_hours() -> float:
    if not _CACHE_FILE.exists():
        return float("inf")
    age_secs = (datetime.now() - datetime.fromtimestamp(_CACHE_FILE.stat().st_mtime)).total_seconds()
    return age_secs / 3600


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _fetch_raw_from_nse() -> bytes:
    """
    Fetch the Nifty 500 CSV from NSE archives.
    Opens a session and hits the landing page first to acquire cookies —
    NSE rejects direct API calls without a valid session.
    """
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=15)
    except Exception:
        pass  # cookie grab failure is non-fatal; proceed with the actual request

    resp = session.get(NSE_URL, headers=NSE_HEADERS, timeout=20)

    if resp.status_code == 403:
        raise RuntimeError(
            f"NSE returned 403 Forbidden. The automated fetch is blocked.\n"
            f"Manual fallback: open {NSE_URL} in a browser and save the file to:\n"
            f"  {_CACHE_FILE}"
        )
    resp.raise_for_status()
    return resp.content


def _parse_csv(content: bytes) -> pd.DataFrame:
    """Parse NSE CSV bytes into a clean DataFrame with columns: symbol, company_name, ticker."""
    df = pd.read_csv(io.BytesIO(content))
    df.columns = df.columns.str.strip()

    # Flexible column detection — NSE has changed column names in the past
    symbol_col = next(
        (c for c in df.columns if c.strip().lower() == "symbol"), None
    ) or next(
        (c for c in df.columns if "symbol" in c.lower()), None
    )
    name_col = next(
        (c for c in df.columns if "company" in c.lower()), None
    ) or next(
        (c for c in df.columns if "name" in c.lower()), None
    )

    if not symbol_col:
        raise ValueError(
            f"Cannot find Symbol column in NSE CSV.\n"
            f"Columns found: {list(df.columns)}\n"
            f"The CSV format may have changed — inspect {_CACHE_FILE} manually."
        )

    df = df.rename(columns={symbol_col: "symbol"})
    if name_col:
        df = df.rename(columns={name_col: "company_name"})
    else:
        logger.warning("No company name column found in NSE CSV; using symbol as name.")
        df["company_name"] = df["symbol"]

    df["symbol"] = df["symbol"].str.strip()
    df["company_name"] = df["company_name"].str.strip()
    df["ticker"] = df["symbol"] + ".NS"

    result = df[["symbol", "company_name", "ticker"]].drop_duplicates().reset_index(drop=True)
    logger.info(f"Parsed {len(result)} unique Nifty 500 constituents.")
    return result


def fetch_nifty500(force_refresh: bool = False) -> pd.DataFrame:
    """
    Returns a DataFrame of Nifty 500 constituents:
        symbol       — NSE ticker without suffix (e.g., "RELIANCE")
        company_name — Full company name
        ticker       — yfinance-compatible ticker (e.g., "RELIANCE.NS")

    Cache policy:
        - Returns cached file if it's fresh (< 24h) and force_refresh is False.
        - Otherwise fetches from NSE (with retry) and updates the cache.
        - Falls back to stale cache if the live fetch fails for any reason.
        - Raises RuntimeError only if no data source is available at all.
    """
    if not force_refresh and _cache_age_hours() < _CACHE_MAX_AGE_HOURS:
        logger.info(f"Loading Nifty 500 from cache (age: {_cache_age_hours():.1f}h).")
        return _parse_csv(_CACHE_FILE.read_bytes())

    try:
        logger.info("Fetching Nifty 500 list from NSE archives...")
        content = _fetch_raw_from_nse()
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_bytes(content)
        logger.info(f"Fetched {len(content):,} bytes — cached to {_CACHE_FILE}.")
        return _parse_csv(content)

    except Exception as exc:
        if _CACHE_FILE.exists():
            logger.warning(
                f"NSE live fetch failed ({exc}). "
                f"Falling back to stale cache (age: {_cache_age_hours():.1f}h)."
            )
            return _parse_csv(_CACHE_FILE.read_bytes())

        raise RuntimeError(
            f"No Nifty 500 data available: live fetch failed and no local cache exists.\n"
            f"Error: {exc}\n\n"
            f"Manual fix: download {NSE_URL} in a browser and save to:\n"
            f"  {_CACHE_FILE}"
        ) from exc
