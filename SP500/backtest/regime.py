"""
CP-S4 — S&P 500 Regime Analysis

Downloads ^GSPC (S&P 500 index) and ^VIX daily closes, computes:
  - 200-DMA regime: 'bull' (close > 200-DMA) or 'bear' (close ≤ 200-DMA)
  - VIX tier: 'calm' (<20) | 'elevated' (20-25) | 'stressed' (≥25)
  - 6M trailing return on ^GSPC (126 trading days)

Saves to sp500_market_regime table (daily time series, DELETE + repopulate).
Dashboard joins trades.entry_date with this table at query time — no trade records
are modified.
"""

import logging
from datetime import date
from pathlib import Path
import sys

import pandas as pd
import yfinance as yf
from sqlalchemy.orm import Session

_HERE = Path(__file__).resolve().parent   # SP500/backtest/
_ROOT = _HERE.parent.parent               # project root
sys.path.insert(0, str(_ROOT))

from shared.db import get_engine
from shared.models import Base, Sp500MarketRegime

logger = logging.getLogger(__name__)

GSPC_TICKER  = "^GSPC"
VIX_TICKER   = "^VIX"
DATA_START   = "2004-06-01"   # extra warm-up for 200-DMA (needs ~150 sessions before 2006-01-01)
REGIME_START = date(2006, 1, 1)

_VIX_CALM      = 20.0
_VIX_ELEVATED  = 25.0


# ── Download helpers ───────────────────────────────────────────────────────────

def _download_close(ticker: str, start: str) -> pd.Series:
    """Download daily adjusted close for ticker. Returns Series indexed by date."""
    logger.info("Downloading %-6s from %s ...", ticker, start)
    raw = yf.download(ticker, start=start, auto_adjust=True, progress=False)
    if raw.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    close = raw["Close"]
    if hasattr(close, "columns"):       # multi-ticker call returns DataFrame
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).normalize()
    close.name  = ticker
    return close.dropna()


# ── Regime classifiers ─────────────────────────────────────────────────────────

def _gspc_regime(close: float, ma200: float) -> str:
    if pd.isna(ma200):
        return "unknown"
    return "bull" if close > ma200 else "bear"


def _vix_tier(vix: float) -> str:
    if pd.isna(vix):
        return "unknown"
    if vix < _VIX_CALM:
        return "calm"
    elif vix < _VIX_ELEVATED:
        return "elevated"
    else:
        return "stressed"


# ── Main build function ────────────────────────────────────────────────────────

def build_regime_table() -> pd.DataFrame:
    """
    Download ^GSPC + ^VIX, compute regime signals, persist to sp500_market_regime.
    Returns the full regime DataFrame (index = trading date).
    """
    engine = get_engine()
    Base.metadata.create_all(engine)

    gspc = _download_close(GSPC_TICKER, DATA_START)
    vix  = _download_close(VIX_TICKER,  DATA_START)

    # Align on ^GSPC trading days; VIX may have slightly different calendar
    df = pd.DataFrame({"gspc": gspc, "vix": vix})
    df["vix"] = df["vix"].ffill(limit=3)          # fill up to 3 missing VIX days
    df = df.dropna(subset=["gspc"])

    # Rolling signals
    df["gspc_ma200"]           = df["gspc"].rolling(200, min_periods=150).mean()
    df["gspc_6m_return_pct"]   = df["gspc"].pct_change(126) * 100
    df["gspc_dist_200dma_pct"] = (df["gspc"] / df["gspc_ma200"] - 1) * 100

    df["gspc_regime"] = df.apply(
        lambda r: _gspc_regime(r["gspc"], r["gspc_ma200"]), axis=1
    )
    df["vix_tier"] = df["vix"].apply(_vix_tier)
    df["date_str"] = df.index.strftime("%Y-%m-%d")

    # Only persist from REGIME_START onward
    df_save = df[df.index >= pd.Timestamp(REGIME_START)].copy()
    logger.info("Building regime table: %d trading days from %s to %s",
                len(df_save), df_save.index[0].date(), df_save.index[-1].date())

    with Session(engine) as session:
        deleted = session.query(Sp500MarketRegime).delete()
        logger.info("Cleared %d old regime rows", deleted)

        objs = []
        for _, row in df_save.iterrows():
            def _f(v):
                return float(v) if not pd.isna(v) else None

            objs.append(Sp500MarketRegime(
                date                 = row["date_str"],
                gspc_close           = _f(row["gspc"]),
                gspc_ma200           = _f(row["gspc_ma200"]),
                gspc_regime          = row["gspc_regime"],
                gspc_dist_200dma_pct = _f(row["gspc_dist_200dma_pct"]),
                gspc_6m_return_pct   = _f(row["gspc_6m_return_pct"]),
                vix_close            = _f(row["vix"]),
                vix_tier             = row["vix_tier"],
            ))

        session.bulk_save_objects(objs)
        session.commit()

    logger.info("Saved %d regime rows to sp500_market_regime", len(objs))
    return df_save
