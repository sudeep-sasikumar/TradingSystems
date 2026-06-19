"""
SQLAlchemy table definitions — shared across all strategy phases and all services.

All phases write to the same trading.db. Use the strategy_version column to
filter by phase (e.g., "52wh_v1" for Phase 1).
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _now() -> str:
    return datetime.utcnow().isoformat()


class Signal(Base):
    """
    Every scanner-detected entry signal, regardless of outcome.
    Created by the live scanner; not used by the backtest.

    status flow:  pending → accepted | rejected | expired
    """
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200))

    signal_price = Column(Float, nullable=False)    # price when scanner ran
    signal_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    scan_timestamp = Column(String(30), nullable=False)  # ISO UTC datetime

    # "intraday_provisional" = intraday high crossed 252d-high benchmark
    # "eod_confirmed"        = also closed above close-based 252d benchmark
    signal_type = Column(String(30), nullable=False, default="intraday_provisional")
    benchmark_252d = Column(Float)  # the 252-day level that was crossed

    # pending | accepted | rejected | expired
    status = Column(String(20), nullable=False, default="pending", index=True)

    telegram_message_id = Column(String(50))
    positions_open_at_signal = Column(Integer)   # open count when signal fired
    cap_at_signal = Column(Integer)              # MAX_CONCURRENT_POSITIONS at that time

    # Conviction tier — set by scanner at signal creation time (Checkpoint 8b+).
    # NULL for signals created before Checkpoint 8b.
    #
    # Tier rules (see analysis/conviction.py):
    #   'HIGH'     = market 6M in bottom-2 quintiles AND synthetic basket above 200-DMA
    #   'AVOID'    = market 6M in strong_uptrend quintile
    #   'STANDARD' = everything else (incl. when basket data unavailable)
    #
    # Additive score: +1 below_200dma, +1 bottom-2 quintiles, +1 basket above 200-DMA,
    # -1 above_200dma + strong_uptrend.  Range: -1 to +3.
    # Revisit finer scoring (>3 tiers) once 12-18 months of live tier-tagged signals
    # exist with 50+ trades per tier to revalidate.
    conviction_tier = Column(String(20))   # 'HIGH' | 'STANDARD' | 'AVOID' | NULL
    regime_score    = Column(Integer)      # -1 to +3 | NULL

    created_at = Column(String(30), nullable=False, default=_now)
    updated_at = Column(String(30), nullable=False, default=_now)
    strategy_version = Column(String(20), nullable=False, default="52wh_v1")

    trade = relationship("Trade", back_populates="signal", uselist=False)


class Trade(Base):
    """
    All trades — both backtest (source='backtest') and live (source='live').

    Backtest trades: signal_id is NULL; written in bulk by run_backtest.py.
    Live trades: signal_id links back to the Signal that was accepted.

    status:  open | closed
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)

    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200))

    # Entry
    entry_date = Column(String(10), nullable=False)   # YYYY-MM-DD
    entry_price = Column(Float, nullable=False)
    source = Column(String(20), nullable=False)        # 'backtest' | 'live'

    # Running state (updated as price moves)
    highest_price_reached = Column(Float)
    trailing_stop = Column(Float)  # = highest_price_reached * 0.80

    # Exit — NULL while trade is open
    exit_date = Column(String(10))
    exit_price = Column(Float)
    exit_reason = Column(String(50))  # 'trailing_stop' | 'manual'

    # Computed on close
    return_pct = Column(Float)
    holding_days = Column(Integer)
    trade_year = Column(Integer, index=True)  # calendar year trade was OPENED

    status = Column(String(20), nullable=False, default="open", index=True)
    strategy_version = Column(String(20), nullable=False, default="52wh_v1")

    created_at = Column(String(30), nullable=False, default=_now)
    updated_at = Column(String(30), nullable=False, default=_now)

    signal = relationship("Signal", back_populates="trade")


class IndexMembership(Base):
    """
    Historical Nifty 500 membership — which stocks were constituents on which dates.

    Reconstructed from:
      - Baseline: data/reconstitution_pdfs/nifty500_baseline_20200725.csv
        (Wayback Machine snapshot of NSE's ind_nifty500list.csv, captured 2020-07-25)
      - Semi-annual reconstitution PDFs from niftyindices.com (~Sep 2019 → present)

    Effective coverage: ~Oct 2019 to present (~7 years as of 2026).
    Backtest tags this period as strategy_version='52wh_v1_survivorship_10y' per user spec.

    Membership query:
        SELECT * FROM index_membership
        WHERE symbol = :symbol
          AND added_date <= :date
          AND (removed_date IS NULL OR removed_date >= :date)
    """
    __tablename__ = "index_membership"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(20), nullable=False)
    company_name = Column(String(200))
    isin         = Column(String(20))

    # Dates stored as YYYY-MM-DD strings for consistency with the rest of the DB.
    added_date   = Column(String(10), nullable=False)   # when stock joined the index
    removed_date = Column(String(10))                   # when it left; NULL = still in

    # 'exact'    — date taken directly from a reconstitution PDF
    # 'inferred' — stock was in the Jul-2020 baseline but no add-event found in PDFs;
    #              added_date is set to the first day of our coverage window
    date_quality = Column(String(20), nullable=False, default="inferred")

    source = Column(String(100))    # e.g. 'baseline_20200725', 'recon_202404'
    notes  = Column(String(500))

    __table_args__ = (
        UniqueConstraint("symbol", "added_date", name="uq_membership_symbol_added"),
        Index("ix_membership_symbol", "symbol"),
    )


class Sp500Membership(Base):
    """
    Historical S&P 500 membership — time-varying constituent list.

    Source: fja05680/sp500 (GitHub), derived from S&P announcements via Wikipedia.
    Coverage: ~1996 to present. Backtest uses from COVERAGE_START = 2006-01-01.

    Membership query:
        SELECT * FROM sp500_membership
        WHERE ticker = :ticker
          AND added_date <= :date
          AND (removed_date IS NULL OR removed_date > :date)
    """
    __tablename__ = "sp500_membership"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    ticker       = Column(String(20), nullable=False)
    company_name = Column(String(200))

    added_date   = Column(String(10), nullable=False)   # YYYY-MM-DD
    removed_date = Column(String(10))                   # NULL = still in index

    # 'confirmed' — date taken from Wikipedia-sourced change record
    # 'baseline'  — ticker was in the 1996-03-25 baseline; actual add date unknown
    date_quality = Column(String(20), nullable=False, default="confirmed")

    source = Column(String(100))
    notes  = Column(String(500))

    __table_args__ = (
        Index("ix_sp500_membership_ticker", "ticker"),
        Index("ix_sp500_membership_dates",  "added_date", "removed_date"),
    )


class Sp500MarketRegime(Base):
    """
    Daily ^GSPC + ^VIX regime signals for the S&P 500 system.

    Written by SP500/backtest/regime.py (CP-S4).
    Dashboard joins trades.entry_date with this table to classify entry regime.

    200-DMA regime: 'bull' = close > ma200, 'bear' = close ≤ ma200.
    VIX tier:       'calm' = VIX < 20, 'elevated' = 20-25, 'stressed' = ≥25.
    """
    __tablename__ = "sp500_market_regime"

    date                 = Column(String(10), primary_key=True)   # YYYY-MM-DD
    gspc_close           = Column(Float)
    gspc_ma200           = Column(Float)
    gspc_regime          = Column(String(10))    # 'bull' | 'bear' | 'unknown'
    gspc_dist_200dma_pct = Column(Float)         # % distance above/below 200-DMA
    gspc_6m_return_pct   = Column(Float)         # 126-trading-day trailing return
    vix_close            = Column(Float)
    vix_tier             = Column(String(15))    # 'calm' | 'elevated' | 'stressed'

    __table_args__ = (
        Index("ix_sp500_regime_date", "date"),
    )


class TradeRegimeTag(Base):
    """
    Regime tags for every trade in the survivorship-corrected historic backtest.

    All measures are point-in-time as of entry_date (no lookahead on the
    time series). Quintile thresholds use the full history distribution
    across the dataset (mild cross-sectional lookahead; requested by design).

    Written by: 52WeekHigh/run_regime_analysis.py --checkpoint tag
    Read by:    dashboard/tabs/tab_regime.py
    """
    __tablename__ = "trade_regime_tags"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    trade_id         = Column(Integer, ForeignKey("trades.id"), nullable=False, unique=True)
    ticker           = Column(String(20), nullable=False)
    entry_date       = Column(String(10), nullable=False)
    strategy_version = Column(String(20), nullable=False)

    # Market regime (Nifty 500 index ^CRSLDX or synthetic fallback)
    market_index_used       = Column(String(60))   # e.g. "^CRSLDX" or "synthetic_N_stocks"
    market_vs_200dma        = Column(String(20))   # "above_200dma" | "below_200dma"
    market_dist_200dma_pct  = Column(Float)        # % distance (+/-)
    market_6m_return_pct    = Column(Float)        # raw 6-month trailing return %
    market_6m_quintile      = Column(String(30))   # quintile label

    # Official NSE sectoral index regime (null if no matching index for this stock)
    official_sector         = Column(String(40))   # e.g. "NIFTY_PHARMA"
    official_sector_ticker  = Column(String(20))   # Yahoo Finance ticker used
    official_vs_200dma      = Column(String(20))
    official_dist_200dma_pct = Column(Float)
    official_6m_return_pct  = Column(Float)
    official_6m_quintile    = Column(String(30))

    # Synthetic equal-weighted industry basket (from baseline CSV Industry column)
    industry_group           = Column(String(100)) # e.g. "PHARMA", "IT"
    synthetic_basket_size    = Column(Integer)     # number of stocks in the basket
    synthetic_vs_200dma      = Column(String(20))
    synthetic_dist_200dma_pct = Column(Float)
    synthetic_6m_return_pct  = Column(Float)
    synthetic_6m_quintile    = Column(String(30))

    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        Index("ix_regime_tag_entry_date", "entry_date"),
        Index("ix_regime_tag_strategy", "strategy_version"),
    )
