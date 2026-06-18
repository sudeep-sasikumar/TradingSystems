"""
regime_data.py — Fetch and compute market/sector regime signals.

Market index:  ^CRSLDX (Nifty 500 on Yahoo Finance); falls back to
               synthetic equal-weighted basket from historic price cache.
Sectoral:      9 official NSE indices via Yahoo Finance (Auto, Bank, IT,
               Pharma, FMCG, Metal, Realty, Media, Energy, PSU Bank).
Synthetic:     equal-weighted industry baskets from baseline CSV (≥10 stocks).

Regime signals are computed strictly point-in-time (no time-series lookahead).
Quintile thresholds use the full history distribution of the series — a mild
cross-sectional lookahead that the user explicitly requested for comparative
analysis purposes.
"""

from __future__ import annotations

import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent

REGIME_CACHE_DIR = _ROOT / "data" / "cache" / "regime"
PRICE_CACHE_DIR  = _ROOT / "data" / "cache" / "prices_historic"
BASELINE_CSV     = _ROOT / "data" / "reconstitution_pdfs" / "nifty500_baseline_20200725.csv"

LOOKBACK_START   = "2018-01-01"
CACHE_MAX_AGE_H  = 23   # hours before regime cache is considered stale
MIN_BASKET_SIZE  = 10   # industries with fewer stocks are skipped for synthetic baskets

logger = logging.getLogger(__name__)

# ── Index ticker configuration ─────────────────────────────────────────────────

# Nifty 500 market index — tried in order; first with valid data wins
NIFTY500_CANDIDATES = ["^CRSLDX", "^NSEI"]

# Official NSE sectoral indices available on Yahoo Finance (verified 2026-06-18)
# Unavailable: ^CNXHEALTH, ^CNXFINANCE, ^CNXCONSDUR
SECTORAL_TICKERS: dict[str, tuple[str, str]] = {
    "NIFTY_AUTO":    ("^CNXAUTO",   "Nifty Auto"),
    "NIFTY_BANK":    ("^NSEBANK",   "Nifty Bank"),
    "NIFTY_IT":      ("^CNXIT",     "Nifty IT"),
    "NIFTY_PHARMA":  ("^CNXPHARMA", "Nifty Pharma"),
    "NIFTY_FMCG":    ("^CNXFMCG",  "Nifty FMCG"),
    "NIFTY_METAL":   ("^CNXMETAL",  "Nifty Metal"),
    "NIFTY_REALTY":  ("^CNXREALTY", "Nifty Realty"),
    "NIFTY_MEDIA":   ("^CNXMEDIA",  "Nifty Media"),
    "NIFTY_ENERGY":  ("^CNXENERGY", "Nifty Energy"),
    "NIFTY_PSUBANK": ("^CNXPSUBANK","Nifty PSU Bank"),
}

# Exact mapping from baseline CSV Industry names → available sectoral index
# Only industries with a clean 1:1 relationship are mapped; others left null.
INDUSTRY_TO_SECTOR: dict[str, str] = {
    "PHARMA":               "NIFTY_PHARMA",
    "AUTOMOBILE":           "NIFTY_AUTO",
    "IT":                   "NIFTY_IT",
    "METALS":               "NIFTY_METAL",
    "OIL & GAS":            "NIFTY_ENERGY",
    "POWER":                "NIFTY_ENERGY",
    "MEDIA & ENTERTAINMENT":"NIFTY_MEDIA",
}

QUINTILE_LABELS = (
    "strong_downtrend",
    "moderate_downtrend",
    "flat",
    "moderate_uptrend",
    "strong_uptrend",
)


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_path(name: str) -> Path:
    return REGIME_CACHE_DIR / f"{name}.pkl"


def _load_cache(name: str):
    p = _cache_path(name)
    if not p.exists():
        return None
    age_h = (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 3600
    if age_h > CACHE_MAX_AGE_H:
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_cache(name: str, obj) -> None:
    try:
        REGIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_cache_path(name), "wb") as f:
            pickle.dump(obj, f)
    except Exception as e:
        logger.warning(f"Cache write failed for {name}: {e}")


# ── Core regime computation ────────────────────────────────────────────────────

def compute_regime(close: pd.Series) -> pd.DataFrame:
    """
    Compute regime signals from a daily close series.

    Returns DataFrame indexed by date with columns:
      vs_200dma, dist_200dma_pct, ret_6m_pct, quintile_6m

    200-DMA and 6-month return are strictly point-in-time.
    Quintile thresholds are computed from the full history of this series.
    """
    close = close.sort_index()

    sma200         = close.rolling(200, min_periods=200).mean()
    dist_200dma    = (close - sma200) / sma200 * 100
    ret_6m_pct     = (close / close.shift(126) - 1) * 100

    # Full-history quintile thresholds (cross-sectional, as requested)
    valid_returns = ret_6m_pct.dropna()
    if len(valid_returns) > 0:
        q20, q40, q60, q80 = np.nanpercentile(valid_returns, [20, 40, 60, 80])
    else:
        q20 = q40 = q60 = q80 = float("nan")

    def _label(r) -> Optional[str]:
        if pd.isna(r) or pd.isna(q20):
            return None
        if r <= q20: return "strong_downtrend"
        if r <= q40: return "moderate_downtrend"
        if r <= q60: return "flat"
        if r <= q80: return "moderate_uptrend"
        return "strong_uptrend"

    vs_200dma = pd.Series(index=close.index, dtype="object")
    valid_sma = sma200.notna()
    vs_200dma[valid_sma & (close > sma200)] = "above_200dma"
    vs_200dma[valid_sma & (close <= sma200)] = "below_200dma"

    return pd.DataFrame({
        "close":           close,
        "vs_200dma":       vs_200dma,
        "dist_200dma_pct": dist_200dma.round(3),
        "ret_6m_pct":      ret_6m_pct.round(3),
        "quintile_6m":     ret_6m_pct.apply(_label),
    })


# ── Yahoo Finance download ─────────────────────────────────────────────────────

def _download_close(ticker: str, min_start: str = LOOKBACK_START) -> Optional[pd.Series]:
    """Download daily close from Yahoo Finance. Returns None on failure or insufficient history."""
    try:
        raw = yf.download(ticker, start=LOOKBACK_START, auto_adjust=True, progress=False)
        if raw.empty:
            return None
        close = raw["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        close.index = pd.to_datetime(close.index).tz_localize(None)
        if len(close) < 200 or close.index[0] > pd.Timestamp(min_start):
            return None
        return close
    except Exception as e:
        logger.debug(f"  {ticker}: {e}")
        return None


# ── Market regime (Nifty 500) ─────────────────────────────────────────────────

def load_market_regime(force_refresh: bool = False) -> tuple[pd.DataFrame, str]:
    """
    Load Nifty 500 market regime. Returns (regime_df, ticker_used_description).

    Tries ^CRSLDX first (Nifty 500), then ^NSEI (Nifty 50 proxy), then
    builds a synthetic equal-weighted basket from the historic price cache.
    """
    if not force_refresh:
        cached = _load_cache("market_regime")
        if cached is not None:
            ticker_used = getattr(cached, "attrs", {}).get("ticker_used", "cached")
            if isinstance(cached, pd.DataFrame) and hasattr(cached, "attrs"):
                ticker_used = cached.attrs.get("ticker_used", "cached")
            return cached, ticker_used

    logger.info("Loading market regime (Nifty 500 index)...")
    close = None
    ticker_used = None

    for candidate in NIFTY500_CANDIDATES:
        logger.info(f"  Trying {candidate} ...")
        close = _download_close(candidate)
        if close is not None:
            ticker_used = candidate
            logger.info(f"  OK: {len(close)} rows, {close.index[0].date()} → {close.index[-1].date()}")
            break
        logger.info(f"  {candidate}: no data or insufficient history")

    if close is None:
        logger.warning("  Yahoo Finance tickers failed. Building synthetic equal-weighted proxy.")
        close, n_stocks = _build_universe_index()
        ticker_used = f"synthetic_equal_weight_{n_stocks}_stocks"
        logger.info(f"  Synthetic index built from {n_stocks} stocks.")

    regime_df = compute_regime(close)
    regime_df.attrs["ticker_used"] = ticker_used
    _save_cache("market_regime", regime_df)
    logger.info(f"  Market regime computed: {regime_df['vs_200dma'].notna().sum()} tagged dates")
    return regime_df, ticker_used


def _build_universe_index() -> tuple[pd.Series, int]:
    """Build equal-weighted index from all stocks in the historic price cache."""
    series_list: list[pd.Series] = []
    for f in PRICE_CACHE_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(f)
            s = df["Close"].dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None)
            if len(s) >= 200:
                series_list.append(s)
        except Exception:
            pass

    if not series_list:
        raise RuntimeError("No price cache files found for synthetic index.")

    combined   = pd.concat(series_list, axis=1)
    basket_ret = combined.pct_change(fill_method=None).mean(axis=1)
    level      = (1 + basket_ret).cumprod() * 100
    return level.dropna(), len(series_list)


# ── Sectoral regimes ───────────────────────────────────────────────────────────

def load_sectoral_regimes(force_refresh: bool = False) -> dict[str, pd.DataFrame]:
    """
    Download each available NSE sectoral index and compute regime signals.
    Only indices with data back to LOOKBACK_START are included.
    """
    if not force_refresh:
        cached = _load_cache("sectoral_regimes")
        if cached is not None:
            return cached

    logger.info("Loading sectoral regime data...")
    result: dict[str, pd.DataFrame] = {}

    for key, (ticker, name) in SECTORAL_TICKERS.items():
        close = _download_close(ticker)
        if close is not None:
            result[key] = compute_regime(close)
            logger.info(f"  {name} ({ticker}): {len(close)} rows")
        else:
            logger.info(f"  {name} ({ticker}): skipped (no data or insufficient history)")

    logger.info(f"  Sectoral indices loaded: {len(result)}/{len(SECTORAL_TICKERS)}")
    _save_cache("sectoral_regimes", result)
    return result


# ── Synthetic industry baskets ────────────────────────────────────────────────

def load_baseline_industries() -> dict[str, list[str]]:
    """
    Read baseline CSV and return {industry → [ticker_symbols]} dict.
    Tickers are in Yahoo Finance format (e.g. "RELIANCE.NS").
    """
    df = pd.read_csv(BASELINE_CSV, dtype=str).fillna("")
    df.columns = df.columns.str.strip()

    sym_col = next((c for c in df.columns if c.strip().lower() == "symbol"), None)
    ind_col = next((c for c in df.columns if "industry" in c.lower()), None)
    if not sym_col or not ind_col:
        raise ValueError(f"Cannot find Symbol/Industry cols. Found: {df.columns.tolist()}")

    groups: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        sym = str(row[sym_col]).strip().upper()
        ind = str(row[ind_col]).strip()
        if not sym or sym == "NAN" or not ind or ind == "NAN":
            continue
        ticker = f"{sym}.NS"
        groups.setdefault(ind, []).append(ticker)

    return groups


def build_synthetic_baskets(force_refresh: bool = False) -> dict[str, tuple[pd.DataFrame, int]]:
    """
    Build equal-weighted industry baskets for all industries with ≥ MIN_BASKET_SIZE stocks.
    Returns {industry_name → (regime_df, actual_basket_size)}.
    """
    if not force_refresh:
        cached = _load_cache("synthetic_baskets")
        if cached is not None:
            return cached

    logger.info("Building synthetic industry baskets...")
    industry_stocks = load_baseline_industries()

    result: dict[str, tuple[pd.DataFrame, int]] = {}
    for industry, tickers in sorted(industry_stocks.items()):
        n_members = len(tickers)
        if n_members < MIN_BASKET_SIZE:
            logger.info(f"  {industry}: {n_members} stocks — skipped (<{MIN_BASKET_SIZE})")
            continue

        series_list: list[pd.Series] = []
        for t in tickers:
            # File naming: RELIANCE.NS → RELIANCE_NS.parquet
            fname = t.replace(".", "_") + ".parquet"
            p = PRICE_CACHE_DIR / fname
            if p.exists():
                try:
                    df = pd.read_parquet(p)
                    s = df["Close"].dropna()
                    s.index = pd.to_datetime(s.index).tz_localize(None)
                    if len(s) >= 200:
                        series_list.append(s)
                except Exception:
                    pass

        if len(series_list) < MIN_BASKET_SIZE:
            logger.info(
                f"  {industry}: only {len(series_list)} with price data (need {MIN_BASKET_SIZE}), skipped"
            )
            continue

        combined   = pd.concat(series_list, axis=1)
        basket_ret = combined.pct_change(fill_method=None).mean(axis=1)
        level      = (1 + basket_ret).cumprod() * 100
        level      = level.dropna()

        regime_df  = compute_regime(level)
        result[industry] = (regime_df, len(series_list))
        logger.info(f"  {industry}: basket from {len(series_list)} stocks, {len(level)} dates")

    logger.info(f"Built {len(result)} synthetic baskets.")
    _save_cache("synthetic_baskets", result)
    return result


# ── Point-in-time regime lookup ───────────────────────────────────────────────

_NULL_REGIME = {
    "vs_200dma":       None,
    "dist_200dma_pct": None,
    "ret_6m_pct":      None,
    "quintile_6m":     None,
}


def regime_at_date(regime_df: Optional[pd.DataFrame], trade_date: str) -> dict:
    """
    Return regime values for the last available date on or before trade_date.
    All values are None if no valid data exists at that date.
    """
    if regime_df is None or regime_df.empty:
        return _NULL_REGIME.copy()

    ts = pd.Timestamp(trade_date)
    valid = regime_df[
        (regime_df.index <= ts)
        & regime_df["vs_200dma"].notna()
        & regime_df["quintile_6m"].notna()
    ]
    if valid.empty:
        return _NULL_REGIME.copy()

    row = valid.iloc[-1]
    return {
        "vs_200dma":       str(row["vs_200dma"]),
        "dist_200dma_pct": round(float(row["dist_200dma_pct"]), 3),
        "ret_6m_pct":      round(float(row["ret_6m_pct"]), 3),
        "quintile_6m":     str(row["quintile_6m"]),
    }
