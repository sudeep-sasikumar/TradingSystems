"""
S&P 500 universe fetcher with local cache and GICS sector mapping.

Source: Wikipedia "List of S&P 500 companies" page — community-maintained,
        commonly used for this purpose, free and programmatic.

SURVIVORSHIP BIAS WARNING:
    This module fetches the CURRENT S&P 500 constituent list. Stocks removed
    from the index since the start of any analysis period are excluded even
    for periods when they were actual constituents. The backtest and live
    scanner both use this current list. This is a known, accepted limitation
    documented in the UI as:
    "Backtested on CURRENT S&P 500 constituents only — survivorship bias
     present, historical performance likely overstated versus a true
     point-in-time universe."

Cache: data/cache/sp500_us_universe.csv — refreshed weekly.
Fallback: if fetch fails AND cache exists, use cache.
"""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from tenacity import (
    retry, stop_after_attempt, wait_exponential, before_sleep_log,
)

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE_U = Path(__file__).resolve().parent   # 52WeekHighUS/
_ROOT_U = _HERE_U.parent                    # project root
for _pu in (str(_ROOT_U), str(_HERE_U)):
    if _pu not in sys.path:
        sys.path.insert(0, _pu)
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# GICS Sector → sector ETF ticker mapping (from Part A spec)
SECTOR_ETF_MAP: dict[str, str] = {
    "Information Technology":    "XLK",
    "Communication Services":    "XLC",
    "Consumer Discretionary":    "XLY",
    "Financials":                "XLF",
    "Industrials":               "XLI",
    "Health Care":               "XLV",
    "Energy":                    "XLE",
    "Consumer Staples":          "XLP",
    "Utilities":                 "XLU",
    "Materials":                 "XLB",
    "Real Estate":               "XLRE",
}

ALL_SECTOR_ETFS = list(SECTOR_ETF_MAP.values())
INDEX_TICKERS = ["SPY", "QQQ"] + ALL_SECTOR_ETFS

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR    = _PROJECT_ROOT / "data" / "cache"
_CACHE_FILE   = _CACHE_DIR / "sp500_us_universe.csv"
_CACHE_MAX_AGE_DAYS = 7


def _cache_age_days() -> float:
    if not _CACHE_FILE.exists():
        return float("inf")
    age_secs = (datetime.now() - datetime.fromtimestamp(_CACHE_FILE.stat().st_mtime)).total_seconds()
    return age_secs / 86400


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=3, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _fetch_from_wikipedia() -> pd.DataFrame:
    """
    Fetch the S&P 500 constituent table from Wikipedia.

    Returns DataFrame with columns: ticker, company_name, gics_sector, sector_etf.
    """
    logger.info("Fetching S&P 500 universe from Wikipedia ...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(WIKI_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    # Wikipedia page has multiple tables; the first one is the constituent list
    from io import StringIO
    tables = pd.read_html(StringIO(resp.text))
    df = tables[0]

    # Expected columns vary by Wikipedia revision, normalize them
    df.columns = [c.strip() for c in df.columns]

    # Locate the ticker and sector columns (Wikipedia occasionally renames them)
    col_map: dict[str, str] = {}
    for col in df.columns:
        lc = col.lower()
        if "symbol" in lc or "ticker" in lc:
            col_map["ticker"] = col
        elif "security" in lc or "company" in lc or "name" in lc:
            col_map["company_name"] = col
        elif "gics sector" in lc or "gics sub" not in lc and "sector" in lc:
            col_map["gics_sector"] = col

    missing = [k for k in ("ticker", "company_name", "gics_sector") if k not in col_map]
    if missing:
        raise ValueError(
            f"Wikipedia table column detection failed — could not find: {missing}. "
            f"Actual columns: {list(df.columns)}"
        )

    result = pd.DataFrame({
        "ticker":       df[col_map["ticker"]].str.strip().str.replace(r"\.", "-", regex=True),
        "company_name": df[col_map["company_name"]].str.strip(),
        "gics_sector":  df[col_map["gics_sector"]].str.strip(),
    })

    # BRK.B → BRK-B, BF.B → BF-B (yfinance uses hyphen)
    result["ticker"] = result["ticker"].str.replace(r"\.", "-", regex=True)

    # Map sector → ETF (unknown sectors get None — log them)
    result["sector_etf"] = result["gics_sector"].map(SECTOR_ETF_MAP)
    unmapped = result[result["sector_etf"].isna()]["gics_sector"].unique()
    if len(unmapped) > 0:
        logger.warning("Unmapped GICS sectors (no ETF assigned): %s", unmapped.tolist())

    result = result.dropna(subset=["ticker"])
    result = result[result["ticker"].str.len() > 0]

    logger.info("Fetched %d S&P 500 constituents from Wikipedia", len(result))
    return result.reset_index(drop=True)


def fetch_universe(force_refresh: bool = False) -> pd.DataFrame:
    """
    Return the current S&P 500 constituent list as a DataFrame.

    Columns: ticker, company_name, gics_sector, sector_etf

    Caches to data/cache/sp500_us_universe.csv; refreshes if cache is older
    than 7 days or if force_refresh=True.

    Falls back to cache on fetch failure. Raises if cache also unavailable.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_stale = _cache_age_days() > _CACHE_MAX_AGE_DAYS
    if not force_refresh and not cache_stale and _CACHE_FILE.exists():
        logger.info("Loading S&P 500 universe from cache (age %.1f days)", _cache_age_days())
        return pd.read_csv(_CACHE_FILE)

    try:
        df = _fetch_from_wikipedia()
        df.to_csv(_CACHE_FILE, index=False)
        logger.info("S&P 500 universe cached at %s", _CACHE_FILE)
        return df
    except Exception as exc:
        if _CACHE_FILE.exists():
            logger.warning(
                "Wikipedia fetch failed (%s) — falling back to local cache (age %.1f days)",
                exc, _cache_age_days(),
            )
            return pd.read_csv(_CACHE_FILE)
        raise RuntimeError(
            "S&P 500 universe fetch failed and no local cache exists. "
            f"Error: {exc}\n"
            "Manual fallback: open https://en.wikipedia.org/wiki/List_of_S%26P_500_companies "
            "in a browser, copy the constituent table, and save as data/cache/sp500_us_universe.csv "
            "with columns: ticker, company_name, gics_sector, sector_etf"
        ) from exc


def get_tickers(force_refresh: bool = False) -> list[str]:
    """Return a plain list of S&P 500 tickers."""
    return fetch_universe(force_refresh=force_refresh)["ticker"].tolist()


def get_sector_map(force_refresh: bool = False) -> dict[str, str]:
    """Return {ticker: gics_sector} mapping."""
    df = fetch_universe(force_refresh=force_refresh)
    return dict(zip(df["ticker"], df["gics_sector"]))


def get_sector_etf_map(force_refresh: bool = False) -> dict[str, str]:
    """Return {ticker: sector_etf_ticker} mapping (e.g. {'AAPL': 'XLK', ...})."""
    df = fetch_universe(force_refresh=force_refresh)
    return dict(zip(df["ticker"], df["sector_etf"].fillna("")))
