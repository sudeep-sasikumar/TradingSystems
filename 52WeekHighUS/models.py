"""
SQLAlchemy table definitions for the US S&P 500 52-Week High Breakout system.

These tables live in data/sp500_us_breakout.db — NOT in trading.db.
Use 52WeekHighUS/db.py to get the engine/session for this DB.

strategy_version: '52whu_v1'
"""
from datetime import datetime

from sqlalchemy import Column, Index, Integer, Float, String, Boolean, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _now() -> str:
    return datetime.utcnow().isoformat()


class US52WHSignal(Base):
    """
    Every signal evaluated by the live scanner (and ad hoc runs).

    Includes both signals that passed all hard gates AND those that were skipped
    (with skip_reason populated). action field controls what was done:
      'SIGNAL'           — passed all hard gates, Telegram alert sent or queued
      'SKIP_COOLDOWN'    — within 20 trading days of prior signal for this ticker
      'SKIP_HARD_GATE'   — failed one of B1-B4
      'SKIP_RISK'        — passed B1-B4 but failed SL% cap or R:R minimums
    """
    __tablename__ = "us52wh_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_run_id = Column(Integer, nullable=True)   # FK to us52wh_scan_runs.id (soft ref)

    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200))
    gics_sector = Column(String(100))

    signal_date = Column(String(10), nullable=False, index=True)   # YYYY-MM-DD (data date)
    scan_timestamp = Column(String(30), nullable=False)             # ISO UTC
    trigger_type = Column(String(20), nullable=False)               # 'scheduled' | 'manual'
    strategy_version = Column(String(20), nullable=False, default="52whu_v1")

    # Action and skip reason
    action = Column(String(30), nullable=False)   # SIGNAL | SKIP_COOLDOWN | SKIP_HARD_GATE | SKIP_RISK
    skip_reason = Column(String(200))             # human-readable reason when action != SIGNAL

    # Hard gate outcomes (B1-B4)
    b1_market_regime = Column(Boolean)    # SPY close > SPY 50-DMA
    b2_trend_filter = Column(Boolean)     # close > 50-DMA AND close > 200-DMA
    b3_golden_cross = Column(Boolean)     # 50-DMA > 200-DMA
    b4_fresh_breakout = Column(Boolean)   # close > Prior252High × 1.0025

    # Part C: computed levels (NULL when action is SKIP_HARD_GATE or SKIP_COOLDOWN)
    close = Column(Float)
    entry = Column(Float)            # close × 1.001
    structural_sl = Column(Float)    # MIN(TodayCandleLow, 5DaySwingLow) × 0.997
    risk_per_share = Column(Float)   # entry - structural_sl
    sl_pct = Column(Float)           # (risk_per_share / entry) × 100
    t1 = Column(Float)               # entry + 2.0 × ATR14
    t2 = Column(Float)               # entry + 3.5 × ATR14
    rr_t1 = Column(Float)            # (T1 - entry) / risk_per_share
    rr_t2 = Column(Float)            # (T2 - entry) / risk_per_share
    atr14 = Column(Float)
    prior_252_high = Column(Float)   # Prior252High (max daily High, prior 252 days)

    # Position sizing
    account_size = Column(Float)
    risk_pct = Column(Float)
    max_capital_per_trade = Column(Float)
    qty_risk_based = Column(Integer)
    qty_capital_based = Column(Integer)
    final_qty = Column(Integer)
    qty_t1 = Column(Integer)
    qty_trailing = Column(Integer)
    capital_deployed = Column(Float)   # final_qty × entry
    max_loss = Column(Float)           # final_qty × risk_per_share

    # Graded checks (B6-B10) — NULL when hard gates failed
    b6_sector_strength = Column(Boolean)
    b7_relative_strength = Column(Boolean)
    b8_volume = Column(Boolean)
    b9_candle_quality = Column(Boolean)
    b10_liquidity = Column(Boolean)
    graded_checks_passed = Column(Integer)   # 0-5
    tier = Column(String(5))                 # 'A' | 'B' | 'C'

    # Earnings (best-effort)
    earnings_date = Column(String(20))       # YYYY-MM-DD or 'not verified'
    earnings_warning = Column(Boolean)       # True if earnings within 7 days

    # Telegram delivery
    telegram_message_id = Column(String(50))
    alert_sent = Column(Boolean, default=False)
    alert_resent = Column(Boolean, default=False)

    # SPY context (logged for every signal, even skipped ones)
    spy_close = Column(Float)
    spy_sma50 = Column(Float)
    spy_sma200 = Column(Float)

    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        Index("ix_us52wh_sig_date_ticker", "signal_date", "ticker"),
        Index("ix_us52wh_sig_action", "action"),
    )


class US52WHScanRun(Base):
    """
    Log of every scan run (scheduled + ad hoc).

    Used to detect stale data (compare data_end_date to previous run)
    and to diagnose silent Telegram channels or weird scan results.
    """
    __tablename__ = "us52wh_scan_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_type = Column(String(20), nullable=False)   # 'scheduled' | 'manual'
    start_time = Column(String(30), nullable=False)
    end_time = Column(String(30))
    data_end_date = Column(String(10))     # most recent bar date actually retrieved (YYYY-MM-DD)
    status = Column(String(20))            # 'ok' | 'stale_data' | 'error' | 'running'

    tickers_scanned = Column(Integer)
    tickers_failed = Column(Integer)
    failed_tickers_detail = Column(Text)   # JSON: [{ticker, reason}, ...]

    # Hard gate skip counts
    skips_cooldown = Column(Integer, default=0)
    skips_b1_market_regime = Column(Integer, default=0)
    skips_b2_trend = Column(Integer, default=0)
    skips_b3_golden_cross = Column(Integer, default=0)
    skips_b4_breakout = Column(Integer, default=0)
    skips_risk_gate = Column(Integer, default=0)

    signals_generated = Column(Integer, default=0)   # action='SIGNAL'
    alerts_sent = Column(Integer, default=0)

    error_message = Column(Text)

    created_at = Column(String(30), nullable=False, default=_now)


class US52WHPosition(Base):
    """
    Manually-tracked open positions for the US S&P 500 system.

    Created when a signal is accepted via Telegram. Updated as price moves.
    Exit fields populated when closed (trailing stop, T1 hit, or manual).

    This is the dashboard's "Open Positions" section data source.
    """
    __tablename__ = "us52wh_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, nullable=True)   # soft ref to us52wh_signals.id

    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200))

    entry_date = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=False)
    final_qty = Column(Integer, nullable=False)
    qty_t1 = Column(Integer)
    qty_trailing = Column(Integer)

    # Risk levels from original signal
    structural_sl = Column(Float, nullable=False)
    t1 = Column(Float)
    t2 = Column(Float)
    atr14_at_signal = Column(Float)   # ATR as of signal date (fixed for trailing stop calc)

    # Running state
    status = Column(String(20), nullable=False, default="open")   # open | t1_hit | closed
    t1_hit_date = Column(String(10))
    t1_hit_price = Column(Float)
    highest_high_since_entry = Column(Float)
    trailing_stop_level = Column(Float)   # after T1: highest_high - 1×ATR14_at_signal

    # Exit
    exit_date = Column(String(10))
    exit_price = Column(Float)
    exit_reason = Column(String(50))   # 'trailing_stop' | 'sl_hit' | 'time_stop' | 'ema14_exit' | 'manual'

    # Performance
    realized_pl = Column(Float)        # total realized P&L across both legs
    trade_r = Column(Float)            # TradeRealizedPL / InitialRisk

    strategy_version = Column(String(20), nullable=False, default="52whu_v1")
    created_at = Column(String(30), nullable=False, default=_now)
    updated_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        Index("ix_us52wh_pos_status", "status"),
        Index("ix_us52wh_pos_ticker", "ticker"),
    )
