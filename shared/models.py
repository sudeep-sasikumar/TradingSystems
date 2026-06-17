"""
SQLAlchemy table definitions — shared across all strategy phases and all services.

All phases write to the same trading.db. Use the strategy_version column to
filter by phase (e.g., "52wh_v1" for Phase 1).
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, ForeignKey, UniqueConstraint
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
