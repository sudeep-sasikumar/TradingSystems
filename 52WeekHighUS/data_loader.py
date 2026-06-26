"""
US S&P 500 Breakout System — Data Loader

Downloads adjusted daily OHLCV for ~500 tickers and computes all required
indicators. Also downloads SPY, QQQ, and the 11 sector ETFs.

Key design choices:
- auto_adjust=True: all OHLCV columns are split- and dividend-adjusted.
  Volume is NOT adjusted (reported shares traded), which is correct per spec.
- ATR-14: Wilder's RMA (ewm alpha=1/14, adjust=False) — standard convention.
  The existing 52WeekHigh/Nifty system does not use ATR, so there is no
  existing convention to match; Wilder's standard is used.
- Partial bar detection: if the last bar's date is today (US/Eastern) and
  the current time is before 16:15 ET, that bar is excluded (incomplete).
- 5DaySwingLow: MIN(adjusted Low) of the previous 5 COMPLETED trading days,
  EXCLUDING today, so it does not double-count today's candle low in the
  StructuralSL formula.
- Batched downloads: 50 tickers per yfinance call; tenacity retry with
  exponential backoff; a single failed ticker never crashes the scan.
"""
import logging
import sys
import time as _time
from datetime import date, datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent   # 52WeekHighUS/
_ROOT = _HERE.parent                      # project root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

import numpy as np
import pandas as pd
import yfinance as yf
from tenacity import (
    retry, stop_after_attempt, wait_exponential, before_sleep_log, RetryError,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT  = Path(__file__).resolve().parent.parent
_CACHE_DIR_US  = _PROJECT_ROOT / "data" / "cache" / "prices_us52wh"
_CACHE_MAX_AGE_H = 6   # hours: intraday cache TTL

LOOKBACK_START = "2020-01-01"   # ~5 years back; 252-day warm-up needs ~1 year before backtest start
CHUNK_SIZE     = 50             # tickers per yfinance batch

_ET = ZoneInfo("America/New_York")


# ── Partial-bar detection ──────────────────────────────────────────────────────

def _market_buffer_passed() -> bool:
    """True if it's past 16:15 ET today (15-min buffer after US market close)."""
    now_et = datetime.now(_ET)
    return now_et.time() >= time(16, 15)


def _today_et() -> date:
    return datetime.now(_ET).date()


def _last_complete_bar_date(df: pd.DataFrame) -> Optional[date]:
    """
    Return the date of the most recently COMPLETED daily bar.

    If the last bar's date is today (ET) and we're before the 16:15 ET buffer,
    we exclude it (market still open or data not yet finalized).
    """
    if df.empty:
        return None
    last_date = df.index[-1]
    if hasattr(last_date, "date"):
        last_date = last_date.date()
    today = _today_et()
    if last_date >= today and not _market_buffer_passed():
        # Today's bar is incomplete — return second-to-last
        if len(df) < 2:
            return None
        prev = df.index[-2]
        return prev.date() if hasattr(prev, "date") else prev
    return last_date


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_path(ticker: str) -> Path:
    safe = ticker.replace(".", "_").replace("/", "_").replace("-", "_")
    return _CACHE_DIR_US / f"{safe}.parquet"


def _cache_is_fresh(ticker: str) -> bool:
    p = _cache_path(ticker)
    if not p.exists():
        return False
    age_h = (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 3600
    return age_h < _CACHE_MAX_AGE_H


def _load_cache(ticker: str) -> Optional[pd.DataFrame]:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception as exc:
        logger.warning("Cache read failed for %s: %s", ticker, exc)
        return None


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    _CACHE_DIR_US.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(_cache_path(ticker))
    except Exception as exc:
        logger.warning("Cache write failed for %s: %s", ticker, exc)


# ── Indicator computation ──────────────────────────────────────────────────────

def _wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Wilder's ATR using RMA (exponential smoothing with alpha=1/period).
    TR = MAX(High-Low, |High-prevClose|, |Low-prevClose|).
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing: alpha = 1/period, adjust=False
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all required indicator columns to a raw OHLCV DataFrame.

    Input columns expected: Open, High, Low, Close, Volume
    All OHLCV columns must be adjusted prices (auto_adjust=True from yfinance).

    Added columns:
      ATR14         — Wilder 14-period ATR
      SMA50         — 50-day simple moving average of Close
      SMA200        — 200-day simple moving average of Close
      EMA14         — 14-day exponential moving average of Close (for exit rules)
      Prior252High  — MAX(High) over previous 252 trading days (today excluded via shift)
      AvgVol20      — 20-day simple moving average of Volume
      SwingLow5     — MIN(Low) over previous 5 trading days (today excluded via shift)
      RS3M          — 63-day return = Close/Close.shift(63) - 1
    """
    out = df.copy()

    out["ATR14"]        = _wilder_atr(df)
    out["SMA50"]        = df["Close"].rolling(50).mean()
    out["SMA200"]       = df["Close"].rolling(200).mean()
    out["EMA14"]        = df["Close"].ewm(span=14, adjust=False).mean()

    # Prior252High: max of daily High over the PREVIOUS 252 days (shift(1) excludes today)
    out["Prior252High"] = df["High"].shift(1).rolling(252).max()

    out["AvgVol20"]     = df["Volume"].rolling(20).mean()

    # SwingLow5: min of adjusted Low over PREVIOUS 5 days (shift(1) excludes today)
    out["SwingLow5"]    = df["Low"].shift(1).rolling(5).min()

    # 3-month (63 trading day) return — requires 63 prior rows
    out["RS3M"]         = df["Close"] / df["Close"].shift(63) - 1

    return out


# ── Single-ticker download ─────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _download_single(ticker: str, start: str) -> pd.DataFrame:
    """Download one ticker from yfinance with retry."""
    raw = yf.download(
        ticker,
        start=start,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if raw.empty:
        raise ValueError(f"yfinance returned empty DataFrame for {ticker}")

    # yfinance may return a MultiIndex when a single ticker is fetched via download()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(f"Missing columns for {ticker}: {missing}")

    raw.index = pd.to_datetime(raw.index)
    return raw[list(required)]


# ── Batch download ─────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=3, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _download_chunk(tickers: list[str], start: str) -> pd.DataFrame:
    """
    Download a chunk of tickers in a single yfinance call.
    Returns a MultiIndex DataFrame (ticker level on columns).
    """
    raw = yf.download(
        tickers,
        start=start,
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    if raw.empty:
        raise ValueError("yfinance returned empty DataFrame for chunk")
    raw.index = pd.to_datetime(raw.index)
    return raw


def _extract_ticker_from_chunk(chunk_df: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """Extract a single ticker's OHLCV from a chunk MultiIndex DataFrame."""
    try:
        if isinstance(chunk_df.columns, pd.MultiIndex):
            df = chunk_df[ticker].copy()
        else:
            # Single-ticker chunk (happens when chunk has 1 ticker)
            df = chunk_df.copy()

        required = {"Open", "High", "Low", "Close", "Volume"}
        df = df.dropna(subset=["Close"])
        missing = required - set(df.columns)
        if missing:
            logger.warning("Missing columns %s for ticker %s", missing, ticker)
            return None
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except KeyError:
        return None


# ── Main fetch entry points ────────────────────────────────────────────────────

class FetchResult:
    """Result of fetch_all_tickers."""
    def __init__(self):
        self.data: dict[str, pd.DataFrame] = {}      # ticker → raw OHLCV (with indicators)
        self.failed: dict[str, str] = {}             # ticker → error reason
        self.data_end_date: Optional[date] = None    # most recent completed bar date seen


def fetch_all_tickers(
    tickers: list[str],
    start: str = LOOKBACK_START,
    use_cache: bool = True,
) -> FetchResult:
    """
    Download adjusted daily OHLCV for all tickers, compute indicators, return results.

    Strategy:
    1. Load fresh-cached tickers from disk (skip download).
    2. Download remaining tickers in chunks of CHUNK_SIZE.
    3. For any chunk that fails as a batch, retry each ticker individually.
    4. Compute indicators on all successfully downloaded DataFrames.
    5. Log per-ticker status; never let one failure propagate to others.

    Returns FetchResult with .data (ticker → DataFrame with indicators),
    .failed (ticker → reason), and .data_end_date.
    """
    result = FetchResult()
    to_download: list[str] = []

    # Step 1: check cache
    for ticker in tickers:
        if use_cache and _cache_is_fresh(ticker):
            cached = _load_cache(ticker)
            if cached is not None and not cached.empty:
                result.data[ticker] = cached
                continue
        to_download.append(ticker)

    logger.info(
        "Universe: %d tickers | %d from cache | %d to download",
        len(tickers), len(result.data), len(to_download),
    )

    # Step 2: batch download
    chunks = [to_download[i:i + CHUNK_SIZE] for i in range(0, len(to_download), CHUNK_SIZE)]
    for chunk_idx, chunk in enumerate(chunks):
        logger.info("Downloading chunk %d/%d (%d tickers) ...", chunk_idx + 1, len(chunks), len(chunk))
        try:
            chunk_df = _download_chunk(chunk, start=start)
            for ticker in chunk:
                df = _extract_ticker_from_chunk(chunk_df, ticker)
                if df is None or df.empty:
                    logger.warning("No data extracted for %s from chunk", ticker)
                    result.failed[ticker] = "no data in chunk response"
                    continue
                df = compute_indicators(df)
                _save_cache(ticker, df)
                result.data[ticker] = df
        except RetryError as exc:
            # Chunk failed after retries — fall back to individual downloads
            logger.warning(
                "Chunk %d failed after retries (%s), retrying each ticker individually ...",
                chunk_idx + 1, exc,
            )
            for ticker in chunk:
                try:
                    df = _download_single(ticker, start=start)
                    df = compute_indicators(df)
                    _save_cache(ticker, df)
                    result.data[ticker] = df
                except Exception as ind_exc:
                    logger.error("Individual download failed for %s: %s", ticker, ind_exc)
                    result.failed[ticker] = str(ind_exc)
                _time.sleep(0.3)   # polite rate limiting between individual calls

        _time.sleep(0.5)   # small pause between chunks

    # Step 3: compute data_end_date (most recent completed bar seen across all tickers)
    max_date: Optional[date] = None
    for df in result.data.values():
        d = _last_complete_bar_date(df)
        if d is not None and (max_date is None or d > max_date):
            max_date = d
    result.data_end_date = max_date

    logger.info(
        "Fetch complete: %d ok, %d failed, data_end_date=%s",
        len(result.data), len(result.failed), result.data_end_date,
    )
    return result


def fetch_index_data(
    symbols: list[str] | None = None,
    start: str = LOOKBACK_START,
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Download SPY, QQQ, and all sector ETFs.

    Returns dict: symbol → DataFrame with indicators computed.
    """
    from universe import INDEX_TICKERS
    if symbols is None:
        symbols = INDEX_TICKERS

    result: dict[str, pd.DataFrame] = {}
    to_download = []

    for sym in symbols:
        if use_cache and _cache_is_fresh(sym):
            cached = _load_cache(sym)
            if cached is not None and not cached.empty:
                result[sym] = cached
                continue
        to_download.append(sym)

    if to_download:
        try:
            chunk_df = _download_chunk(to_download, start=start)
            for sym in to_download:
                df = _extract_ticker_from_chunk(chunk_df, sym)
                if df is None or df.empty:
                    logger.warning("No data for index/ETF: %s", sym)
                    continue
                df = compute_indicators(df)
                _save_cache(sym, df)
                result[sym] = df
        except Exception as exc:
            logger.error("Index data download failed: %s", exc)
            # Try individually
            for sym in to_download:
                try:
                    df = _download_single(sym, start=start)
                    df = compute_indicators(df)
                    _save_cache(sym, df)
                    result[sym] = df
                except Exception as ind_exc:
                    logger.error("Individual download failed for %s: %s", sym, ind_exc)

    logger.info("Index/ETF data ready: %s", sorted(result.keys()))
    return result


def get_last_row(df: pd.DataFrame, as_of_date: Optional[date] = None) -> Optional[pd.Series]:
    """
    Return the last row of df that is on or before as_of_date.

    If as_of_date is None, uses _last_complete_bar_date(df) — i.e. the most
    recent COMPLETED trading day, excluding any in-progress intraday bar.

    Returns None if no valid row exists.
    """
    if df.empty:
        return None

    if as_of_date is None:
        as_of_date = _last_complete_bar_date(df)
    if as_of_date is None:
        return None

    cutoff = pd.Timestamp(as_of_date)
    valid = df[df.index <= cutoff]
    if valid.empty:
        return None
    return valid.iloc[-1]


def get_prior_row(df: pd.DataFrame, as_of_date: Optional[date] = None, n: int = 1) -> Optional[pd.Series]:
    """Return the row n periods before the last complete bar."""
    if df.empty:
        return None
    if as_of_date is None:
        as_of_date = _last_complete_bar_date(df)
    if as_of_date is None:
        return None
    cutoff = pd.Timestamp(as_of_date)
    valid = df[df.index <= cutoff]
    if len(valid) <= n:
        return None
    return valid.iloc[-(n + 1)]
