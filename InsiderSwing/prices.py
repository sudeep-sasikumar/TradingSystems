"""
InsiderSwing — price data loading and indicator computation.

REUSE, NOT REBUILD
------------------
The batch-download plumbing and the core indicator set already exist in
``52WeekHighUS/data_loader.py`` (Wilder ATR-14, SMA50/200, EMA14, AvgVol20,
SwingLow5, RS3M, and the shift(1) convention that keeps rolling windows
strictly backward-looking).  This module imports ``compute_indicators`` from
there and layers on only what the insider-swing triggers additionally need:
SMA20, Wilder RSI-14, a Donchian breakout level, a configurable swing low, and
a dollar-volume series for the liquidity screen and slippage model.

Two things are deliberately NOT shared with that module:

* Cache directory — ``data/cache/insider/prices``.  A different lookback and
  TTL live here; sharing the directory would mean one system's 6-hour intraday
  refresh invalidating the other's decade-long history.
* Start date — this system backtests from 2016 by default and needs a 252-day
  warm-up before that, so it fetches from an earlier date than the US breakout
  system's 2020 window.

DELISTED NAMES
--------------
The point-in-time universe deliberately contains companies that no longer
trade.  yfinance usually returns nothing for those.  Every such miss is
recorded in ``PriceCoverage.missing`` and reported as a coverage percentage in
the backtest — a survivorship-corrected universe with a silently
survivorship-biased price layer is no better than the bug it was meant to fix.
"""
from __future__ import annotations

import logging
import sys
import time as _time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402

logger = logging.getLogger(__name__)

# 52WeekHighUS/ cannot be imported as a package (its name starts with a digit),
# and putting it on sys.path is not safe here: it contains universe.py, models.py
# and db.py, which would shadow this package's modules of the same name.  Load
# the one function we reuse by explicit file path and restore sys.path
# afterwards — data_loader.py inserts its own directory at position 0 on import,
# and that insert is exactly what would cause the shadowing.
_COMPUTE_INDICATORS = None


def _base_compute_indicators():
    global _COMPUTE_INDICATORS
    if _COMPUTE_INDICATORS is None:
        import importlib.util

        path = _ROOT / "52WeekHighUS" / "data_loader.py"
        if not path.exists():
            raise ImportError(f"Expected shared indicator module at {path}")
        saved_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("us52wh_data_loader", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["us52wh_data_loader"] = mod
            spec.loader.exec_module(mod)
            _COMPUTE_INDICATORS = mod.compute_indicators
        finally:
            sys.path[:] = saved_path
    return _COMPUTE_INDICATORS

CHUNK_SIZE = 50
_CACHE_MAX_AGE_H = 20      # daily bars; one refresh per session is plenty


# ── Extra indicators ──────────────────────────────────────────────────────────

def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI — RMA smoothing (alpha=1/period), matching the ATR convention
    already used elsewhere in this repo rather than a simple-average variant.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # Degenerate cases the ratio can't express:
    #   no losses at all  → RSI 100 by definition
    #   perfectly flat    → RSI 50 (neither overbought nor oversold)
    no_loss = (avg_loss == 0) & (avg_gain > 0)
    flat = (avg_loss == 0) & (avg_gain == 0)
    rsi = rsi.mask(no_loss, 100.0).mask(flat, 50.0)
    # Leading NaNs (before `period` bars exist) are left as NaN on purpose.
    return rsi.where(avg_gain.notna())


def add_insider_indicators(df: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> pd.DataFrame:
    """
    Base indicators from 52WeekHighUS/data_loader plus the insider-swing extras.

    Every rolling window that describes "the past" uses shift(1) so the current
    bar is excluded.  A breakout level that includes today's own high is not a
    level — it is a tautology.
    """
    compute_indicators = _base_compute_indicators()

    conf = config or cfg.DEFAULT_CONFIG
    out = compute_indicators(df)

    out["SMA20"] = df["Close"].rolling(20).mean()
    out["RSI14"] = wilder_rsi(df["Close"], conf.atr_period)

    n = conf.breakout_lookback
    out[f"DonchianHigh{n}"] = df["High"].shift(1).rolling(n).max()

    k = conf.swing_low_lookback
    out[f"SwingLow{k}"] = df["Low"].shift(1).rolling(k).min()

    out["DollarVol"] = df["Close"] * df["Volume"]
    out["AvgDollarVol"] = out["DollarVol"].rolling(conf.adv_lookback_days).mean()

    return out


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_path(ticker: str) -> Path:
    safe = str(ticker).replace(".", "_").replace("/", "_").replace("-", "_")
    return cfg.PRICE_CACHE_DIR / f"{safe}.parquet"


def _cache_fresh(ticker: str) -> bool:
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
        logger.debug("Price cache read failed for %s: %s", ticker, exc)
        return None


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    cfg.PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(_cache_path(ticker))
    except Exception as exc:
        logger.debug("Price cache write failed for %s: %s", ticker, exc)


# ── Download ──────────────────────────────────────────────────────────────────

class PriceCoverage:
    """Result of a universe-wide price fetch, including what could NOT be fetched."""

    def __init__(self):
        self.data: dict[str, pd.DataFrame] = {}
        self.missing: dict[str, str] = {}

    @property
    def coverage_pct(self) -> float:
        total = len(self.data) + len(self.missing)
        return 100.0 * len(self.data) / total if total else 0.0

    def summary(self) -> dict:
        return {
            "with_price": len(self.data),
            "missing": len(self.missing),
            "coverage_pct": round(self.coverage_pct, 1),
            "missing_sample": sorted(self.missing)[:25],
        }


def fetch_prices(
    tickers: Iterable[str],
    start: str,
    end: Optional[str] = None,
    config: Optional[cfg.InsiderConfig] = None,
    use_cache: bool = True,
) -> PriceCoverage:
    """
    Adjusted daily OHLCV + indicators for a universe.

    Batched with a per-ticker fallback, and a failure on one name never
    propagates: it lands in ``coverage.missing`` with a reason.
    """
    import yfinance as yf

    conf = config or cfg.DEFAULT_CONFIG
    cfg.ensure_dirs()

    cov = PriceCoverage()
    want = sorted({str(t).upper().strip() for t in tickers if str(t).strip()})
    to_get: list[str] = []

    for t in want:
        if use_cache and _cache_fresh(t):
            cached = _load_cache(t)
            if cached is not None and not cached.empty:
                cov.data[t] = cached
                continue
        to_get.append(t)

    logger.info("Prices: %d tickers | %d cached | %d to download",
                len(want), len(cov.data), len(to_get))

    def _finish(ticker: str, raw: pd.DataFrame) -> bool:
        raw = raw.dropna(subset=["Close"])
        if raw.empty or len(raw) < 60:
            cov.missing[ticker] = f"insufficient history ({len(raw)} rows)"
            return False
        raw.index = pd.to_datetime(raw.index)
        if getattr(raw.index, "tz", None) is not None:
            raw.index = raw.index.tz_localize(None)
        enriched = add_insider_indicators(raw[["Open", "High", "Low", "Close", "Volume"]], conf)
        _save_cache(ticker, enriched)
        cov.data[ticker] = enriched
        return True

    chunks = [to_get[i:i + CHUNK_SIZE] for i in range(0, len(to_get), CHUNK_SIZE)]
    for idx, chunk in enumerate(chunks, 1):
        logger.info("Downloading price chunk %d/%d (%d tickers)", idx, len(chunks), len(chunk))
        try:
            raw = yf.download(chunk, start=start, end=end, auto_adjust=True,
                              progress=False, threads=True, group_by="ticker")
        except Exception as exc:
            logger.warning("Chunk %d download failed: %s — falling back per ticker", idx, exc)
            raw = None

        for t in chunk:
            sub = None
            if raw is not None and not raw.empty:
                try:
                    sub = raw[t].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
                except KeyError:
                    sub = None
            if sub is None or sub.dropna(subset=["Close"]).empty:
                try:
                    single = yf.download(t, start=start, end=end, auto_adjust=True,
                                         progress=False, threads=False)
                    if isinstance(single.columns, pd.MultiIndex):
                        single.columns = single.columns.get_level_values(0)
                    sub = single
                except Exception as exc:
                    cov.missing[t] = str(exc)
                    continue
                _time.sleep(0.2)
            if sub is None or sub.empty:
                cov.missing[t] = "no data returned (likely delisted)"
                continue
            try:
                _finish(t, sub)
            except Exception as exc:
                cov.missing[t] = f"indicator computation failed: {exc}"

        _time.sleep(0.4)

    logger.info("Price coverage: %s", cov.summary())
    return cov


def load_one(ticker: str, start: str, end: Optional[str] = None,
             config: Optional[cfg.InsiderConfig] = None) -> Optional[pd.DataFrame]:
    """Single-ticker convenience wrapper (live scanner path)."""
    cov = fetch_prices([ticker], start=start, end=end, config=config)
    return cov.data.get(str(ticker).upper())


# ── Calendar helpers ──────────────────────────────────────────────────────────

def trading_days(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(df.index)


def next_session_on_or_after(df: pd.DataFrame, d: date) -> Optional[pd.Timestamp]:
    """First bar on or after ``d``.  None if the series ends before ``d``."""
    idx = pd.DatetimeIndex(df.index)
    pos = idx.searchsorted(pd.Timestamp(d), side="left")
    return idx[pos] if pos < len(idx) else None


def shift_sessions(df: pd.DataFrame, d: date, n: int) -> Optional[pd.Timestamp]:
    """
    The bar ``n`` sessions after the first bar on/after ``d``.

    Used for the availability lag and the confirmation window, so both are
    measured in trading days on the instrument's own calendar rather than in
    calendar days.
    """
    idx = pd.DatetimeIndex(df.index)
    pos = idx.searchsorted(pd.Timestamp(d), side="left")
    target = pos + n
    return idx[target] if 0 <= target < len(idx) else None
