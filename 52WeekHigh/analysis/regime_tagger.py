"""
regime_tagger.py — Tag historic backtest trades with regime signals, save to DB.

Writes to trade_regime_tags (never modifies the trades table).
Clears and re-writes tags for the target strategy_version on each run.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import text

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine, session_scope
from shared.models import Base, TradeRegimeTag
from analysis.regime_data import (
    INDUSTRY_TO_SECTOR,
    SECTORAL_TICKERS,
    build_synthetic_baskets,
    load_baseline_industries,
    load_market_regime,
    load_sectoral_regimes,
    regime_at_date,
)

logger = logging.getLogger(__name__)


def _load_trades(engine, strategy_version: str) -> pd.DataFrame:
    return pd.read_sql(
        """SELECT id, ticker, entry_date FROM trades
           WHERE strategy_version = :sv AND source = 'backtest'
           ORDER BY entry_date""",
        engine,
        params={"sv": strategy_version},
    )


def _reverse_map(industry_stocks: dict[str, list[str]]) -> dict[str, str]:
    """Build ticker → industry reverse index from load_baseline_industries() output."""
    mapping: dict[str, str] = {}
    for industry, tickers in industry_stocks.items():
        for t in tickers:
            mapping[t] = industry
    return mapping


def tag_all_trades(
    force_refresh: bool = False,
    strategy_version: str = "52wh_v1_survivorship_10y",
) -> int:
    """
    Tag every backtest trade with market + sector regime signals.
    strategy_version selects which set of trades to tag.
    Returns number of records written.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)

    logger.info("=" * 60)
    logger.info(f"Loading regime data for strategy_version={strategy_version!r} ...")
    logger.info("=" * 60)

    market_regime, market_ticker = load_market_regime(force_refresh)
    sectoral_regimes             = load_sectoral_regimes(force_refresh)
    synthetic_baskets            = build_synthetic_baskets(force_refresh)
    industry_stocks              = load_baseline_industries()
    ticker_to_industry           = _reverse_map(industry_stocks)

    logger.info(f"Market index used: {market_ticker}")
    logger.info(f"Sectoral indices available: {sorted(sectoral_regimes.keys())}")
    logger.info(f"Synthetic baskets built: {len(synthetic_baskets)}")

    trades_df = _load_trades(engine, strategy_version)
    logger.info(f"Tagging {len(trades_df):,} trades...")

    # Clear existing tags for this strategy version
    with session_scope() as s:
        deleted = s.execute(
            text("DELETE FROM trade_regime_tags WHERE strategy_version = :sv"),
            {"sv": strategy_version},
        ).rowcount
        if deleted:
            logger.info(f"Cleared {deleted} existing regime tag rows.")

    now_str = datetime.utcnow().isoformat()
    records = []
    no_market = 0

    for _, row in trades_df.iterrows():
        trade_id   = int(row["id"])
        ticker     = str(row["ticker"])
        entry_date = str(row["entry_date"])

        # Market regime
        mkt = regime_at_date(market_regime, entry_date)
        if mkt["vs_200dma"] is None:
            no_market += 1

        # Industry / sector
        industry   = ticker_to_industry.get(ticker)
        sector_key = INDUSTRY_TO_SECTOR.get(industry) if industry else None
        sec_regime = sectoral_regimes.get(sector_key) if sector_key else None
        sec        = regime_at_date(sec_regime, entry_date)
        sec_ticker = SECTORAL_TICKERS[sector_key][0] if sector_key and sector_key in SECTORAL_TICKERS else None

        # Synthetic basket regime
        basket_entry = synthetic_baskets.get(industry) if industry else None
        if basket_entry is not None:
            syn_df, basket_size = basket_entry
            syn = regime_at_date(syn_df, entry_date)
        else:
            syn, basket_size = {
                "vs_200dma": None, "dist_200dma_pct": None,
                "ret_6m_pct": None, "quintile_6m": None,
            }, 0

        records.append(TradeRegimeTag(
            trade_id                  = trade_id,
            ticker                    = ticker,
            entry_date                = entry_date,
            strategy_version          = strategy_version,
            market_index_used         = market_ticker,
            market_vs_200dma          = mkt["vs_200dma"],
            market_dist_200dma_pct    = mkt["dist_200dma_pct"],
            market_6m_return_pct      = mkt["ret_6m_pct"],
            market_6m_quintile        = mkt["quintile_6m"],
            official_sector           = sector_key,
            official_sector_ticker    = sec_ticker,
            official_vs_200dma        = sec["vs_200dma"],
            official_dist_200dma_pct  = sec["dist_200dma_pct"],
            official_6m_return_pct    = sec["ret_6m_pct"],
            official_6m_quintile      = sec["quintile_6m"],
            industry_group            = industry,
            synthetic_basket_size     = basket_size,
            synthetic_vs_200dma       = syn["vs_200dma"],
            synthetic_dist_200dma_pct = syn["dist_200dma_pct"],
            synthetic_6m_return_pct   = syn["ret_6m_pct"],
            synthetic_6m_quintile     = syn["quintile_6m"],
            created_at                = now_str,
        ))

    with session_scope() as s:
        for rec in records:
            s.add(rec)

    logger.info(f"Wrote {len(records):,} regime tag records.")
    if no_market:
        logger.warning(f"  {no_market} trades had no market regime (entry too early for 200-DMA)")
    return len(records)
