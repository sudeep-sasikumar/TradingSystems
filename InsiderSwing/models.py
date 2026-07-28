"""
InsiderSwing — SQLAlchemy table definitions.

These tables live in ``data/insider_swing.db`` — NOT in trading.db.  Use
``InsiderSwing/db.py`` for the engine/session.  (Same separate-DB pattern the
52WeekHighUS system uses, so an insider backfill can never corrupt the Nifty /
S&P 500 tables.)

Point-in-time contract, enforced by schema shape
------------------------------------------------
Every table that feeds a trading decision carries ``filing_date``.
``transaction_date`` exists on the transaction table for analysis only and is
NEVER indexed for signal lookup — the indices are on ``filing_date`` precisely
so that the fast query path is also the correct one.

strategy_version: 'insider_v1'
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Float, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

STRATEGY_VERSION = "insider_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
#  RAW FILING DATA
# ──────────────────────────────────────────────────────────────────────────────

class InsiderFiling(Base):
    """
    One row per Form 4 accession per reporting owner.

    Stores the filing envelope: who filed, about which issuer, when it was
    executed (period_of_report) and when it became public (filing_date).
    Individual transaction lines live in InsiderTransaction.
    """
    __tablename__ = "ins_filings"

    id = Column(Integer, primary_key=True, autoincrement=True)

    accession_no  = Column(String(25), nullable=False, index=True)   # 0001234567-26-000123
    source        = Column(String(10), nullable=False)               # 'edgar' | 'fmp'
    form_type     = Column(String(10), nullable=False, default="4")

    issuer_cik    = Column(String(12), index=True)
    issuer_name   = Column(String(250))
    ticker        = Column(String(20), index=True)

    reporting_cik  = Column(String(12), index=True)
    reporting_name = Column(String(250))

    # Section 16 relationship flags, straight off the Form 4.
    is_director    = Column(Boolean, default=False)
    is_officer     = Column(Boolean, default=False)
    is_ten_percent = Column(Boolean, default=False)
    is_other       = Column(Boolean, default=False)
    officer_title  = Column(String(250))

    period_of_report = Column(String(10))                       # YYYY-MM-DD
    filing_date      = Column(String(10), nullable=False, index=True)   # <- the only date signals may use
    acceptance_datetime = Column(String(30))                    # ISO, when EDGAR accepted it

    # SEC's Rule 10b5-1 checkbox (added to Form 4 by the 2022 amendments).
    # None = the filing predates the checkbox or the source doesn't expose it.
    # None is NOT the same as False and must not be treated as "not a plan".
    aff_10b5_one = Column(Boolean, nullable=True)

    footnotes = Column(Text)     # concatenated footnote text (10b5-1 keyword fallback)
    url       = Column(Text)

    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("accession_no", "reporting_cik", name="uq_ins_filing_acc_owner"),
        Index("ix_ins_filing_ticker_date", "ticker", "filing_date"),
    )


class InsiderTransaction(Base):
    """
    One row per transaction line on a Form 4 (non-derivative and derivative).

    ``classification`` is set at ingest by InsiderSwing/filters.py and is the
    only field the scoring layer reads:

      'open_market_buy'   — P-Purchase, cash, non-derivative, not a 10b5-1 plan
      'open_market_sale'  — S-Sale, cash, non-derivative, not a 10b5-1 plan
      'excluded'          — everything else; see exclude_reason

    Re-running the classifier is safe: it only rewrites classification /
    exclude_reason, never the raw filing facts.
    """
    __tablename__ = "ins_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filing_id = Column(Integer, index=True)       # soft ref to ins_filings.id

    accession_no = Column(String(25), nullable=False, index=True)
    line_no      = Column(Integer, nullable=False, default=0)   # ordinal within the filing

    ticker         = Column(String(20), index=True)
    issuer_cik     = Column(String(12), index=True)
    reporting_cik  = Column(String(12), index=True)
    reporting_name = Column(String(250))

    # Denormalised role flags — avoids a join on every scoring pass.
    is_director    = Column(Boolean, default=False)
    is_officer     = Column(Boolean, default=False)
    is_ten_percent = Column(Boolean, default=False)
    officer_title  = Column(String(250))
    role_bucket    = Column(String(20), index=True)   # ceo_cfo | officer | director | ten_pct | other

    security_title = Column(String(250))
    is_derivative  = Column(Boolean, default=False)

    transaction_date = Column(String(10))                          # analysis only — never a signal key
    filing_date      = Column(String(10), nullable=False, index=True)

    transaction_code    = Column(String(4), index=True)   # P, S, A, M, F, G, D, C, ...
    acquired_disposed   = Column(String(2))               # 'A' | 'D'
    shares              = Column(Float)
    price_per_share     = Column(Float)
    value_usd           = Column(Float)                   # shares * price_per_share
    shares_owned_after  = Column(Float)
    direct_or_indirect  = Column(String(2))               # 'D' | 'I'

    rule_10b5_1 = Column(Boolean, nullable=True)   # None = unknown, see InsiderFiling.aff_10b5_one

    classification = Column(String(30), index=True)
    exclude_reason = Column(String(120))

    source     = Column(String(10), nullable=False, default="edgar")
    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("accession_no", "reporting_cik", "line_no", name="uq_ins_txn_line"),
        Index("ix_ins_txn_ticker_filing", "ticker", "filing_date"),
        Index("ix_ins_txn_owner_filing", "reporting_cik", "filing_date"),
        Index("ix_ins_txn_class_filing", "classification", "filing_date"),
    )


class InsiderIngestRun(Base):
    """Audit log of every ingestion run — which source, what range, what failed."""
    __tablename__ = "ins_ingest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source      = Column(String(10), nullable=False)
    mode        = Column(String(20))          # 'backfill' | 'incremental'
    start_date  = Column(String(10))
    end_date    = Column(String(10))
    started_at  = Column(String(30), nullable=False, default=_now)
    finished_at = Column(String(30))
    status      = Column(String(20))          # 'ok' | 'partial' | 'error' | 'running'

    filings_seen     = Column(Integer, default=0)
    filings_inserted = Column(Integer, default=0)
    txns_inserted    = Column(Integer, default=0)
    fetch_failures   = Column(Integer, default=0)
    failure_detail   = Column(Text)           # JSON list
    error_message    = Column(Text)


# ──────────────────────────────────────────────────────────────────────────────
#  SIGNALS
# ──────────────────────────────────────────────────────────────────────────────

class InsiderScore(Base):
    """
    Daily conviction score per symbol, with the full component breakdown so any
    high score can be audited after the fact (same auditability contract as the
    conviction scoring in the Nifty / S&P 500 systems).
    """
    __tablename__ = "ins_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)

    ticker     = Column(String(20), nullable=False, index=True)
    as_of_date = Column(String(10), nullable=False, index=True)   # a filing_date
    param_key  = Column(String(60), nullable=False, index=True)

    score = Column(Float, nullable=False)     # 0-100

    # Components
    cluster_count        = Column(Integer)    # distinct reporting CIKs with qualifying buys
    cluster_component    = Column(Float)
    role_bucket_top      = Column(String(20))
    role_component       = Column(Float)
    size_ratio_max       = Column(Float)      # largest buy / that insider's own trailing avg
    size_component       = Column(Float)
    novelty_count        = Column(Integer)    # buyers who are first-time in novelty window
    novelty_component    = Column(Float)

    buy_value_total   = Column(Float)
    sell_value_total  = Column(Float)
    sell_cluster_count = Column(Integer)
    sell_pressure_flag = Column(Boolean, default=False)

    earnings_date            = Column(String(20))
    earnings_proximity_flag  = Column(Boolean, default=False)
    earnings_penalty_applied = Column(Float)

    window_days   = Column(Integer)
    components_json = Column(Text)            # full audit blob incl. per-insider detail

    strategy_version = Column(String(20), nullable=False, default=STRATEGY_VERSION)
    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("ticker", "as_of_date", "param_key", name="uq_ins_score"),
        Index("ix_ins_score_date_score", "as_of_date", "score"),
    )


class InsiderSignal(Base):
    """
    A symbol that cleared the conviction threshold on a given filing date.

    status lifecycle:
      'pending'   — awaiting technical confirmation inside the window
      'confirmed' — trigger fired; a trade was (or would be) entered
      'expired'   — window elapsed with no trigger.  Logged deliberately: the
                    expired set is what quantifies how much edge the timing
                    overlay costs vs. taking the insider signal alone.
      'blocked'   — sell-pressure or liquidity filter vetoed the entry
    """
    __tablename__ = "ins_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)

    ticker      = Column(String(20), nullable=False, index=True)
    signal_date = Column(String(10), nullable=False, index=True)   # filing_date the score crossed on
    param_key   = Column(String(60), nullable=False, index=True)
    run_id      = Column(Integer, index=True)   # ins_backtest_runs.id; NULL for live

    score              = Column(Float, nullable=False)
    cluster_count      = Column(Integer)
    role_bucket_top    = Column(String(20))
    sell_pressure_flag = Column(Boolean, default=False)
    earnings_proximity_flag = Column(Boolean, default=False)
    components_json    = Column(Text)

    status        = Column(String(15), nullable=False, default="pending", index=True)
    block_reason  = Column(String(120))
    expiry_date   = Column(String(10))
    trigger_date  = Column(String(10))
    trigger_type  = Column(String(30))          # dma_reclaim | range_breakout | rsi_reset
    trigger_price = Column(Float)

    source = Column(String(10), nullable=False, default="backtest")   # 'backtest' | 'live'

    # Telegram delivery (live only)
    telegram_message_id = Column(String(50))
    alert_sent          = Column(Boolean, default=False)
    trigger_alert_sent  = Column(Boolean, default=False)

    strategy_version = Column(String(20), nullable=False, default=STRATEGY_VERSION)
    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        Index("ix_ins_signal_ticker_date", "ticker", "signal_date"),
        Index("ix_ins_signal_run_status", "run_id", "status"),
    )


# ──────────────────────────────────────────────────────────────────────────────
#  TRADES
# ──────────────────────────────────────────────────────────────────────────────

class InsiderTrade(Base):
    """
    One row per simulated or live trade.

    ``arm`` identifies which of the three comparison strategies produced it:
      'insider_only' — enter on signal_lag after filing, no technical trigger
      'tech_only'    — technical trigger with NO insider filter (base rate)
      'combined'     — insider signal AND technical confirmation
    Reporting all three is the only way to tell whether the insider data or the
    technical overlay is actually contributing anything.
    """
    __tablename__ = "ins_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id    = Column(Integer, index=True)
    signal_id = Column(Integer, index=True)
    arm       = Column(String(20), nullable=False, index=True)
    param_key = Column(String(60), index=True)

    ticker      = Column(String(20), nullable=False, index=True)
    signal_date = Column(String(10), index=True)
    entry_date  = Column(String(10), index=True)
    entry_price = Column(Float)          # fill price incl. slippage
    entry_ref_price = Column(Float)      # unslipped reference (next open)
    qty         = Column(Integer)
    notional    = Column(Float)

    initial_stop   = Column(Float)
    stop_basis     = Column(String(20))   # 'atr' | 'swing_low'
    target_price   = Column(Float)
    initial_risk   = Column(Float)        # per-share
    atr_at_entry   = Column(Float)

    exit_date   = Column(String(10), index=True)
    exit_price  = Column(Float)           # fill price incl. slippage
    exit_ref_price = Column(Float)
    exit_reason = Column(String(25))      # stop | target | trail | time_stop | delisted | open

    gross_pnl      = Column(Float)
    slippage_cost  = Column(Float)        # $ lost to spread/impact/commission, both sides
    net_pnl        = Column(Float)
    return_pct     = Column(Float)        # net_pnl / notional
    r_multiple     = Column(Float)        # net_pnl / (qty * initial_risk)
    holding_days   = Column(Integer)      # trading days
    participation_rate = Column(Float)    # notional / ADV, the slippage model input

    status = Column(String(10), nullable=False, default="closed")   # closed | open

    # Segmentation buckets — populated at entry so reporting never re-derives them
    cluster_bucket = Column(String(10), index=True)   # '1' | '2' | '3+' | 'n/a'
    role_bucket    = Column(String(20), index=True)   # ceo_cfo | officer | director | ten_pct | n/a
    mcap_bucket    = Column(String(15), index=True)   # micro | small | mid | large | unknown
    earnings_flag  = Column(Boolean, default=False)
    score_at_entry = Column(Float)
    trigger_type   = Column(String(30))
    wf_window      = Column(String(30), index=True)   # walk-forward window label

    strategy_version = Column(String(20), nullable=False, default=STRATEGY_VERSION)
    created_at = Column(String(30), nullable=False, default=_now)

    __table_args__ = (
        Index("ix_ins_trade_run_arm", "run_id", "arm"),
        Index("ix_ins_trade_arm_entry", "arm", "entry_date"),
    )


class InsiderBacktestRun(Base):
    """Run metadata: parameters, universe coverage, headline metrics, report path."""
    __tablename__ = "ins_backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    label      = Column(String(120))
    param_key  = Column(String(60), index=True)
    params_json = Column(Text)

    start_date = Column(String(10))
    end_date   = Column(String(10))

    started_at  = Column(String(30), nullable=False, default=_now)
    finished_at = Column(String(30))
    status      = Column(String(20), default="running")   # running | ok | error

    universe_size      = Column(Integer)
    universe_with_price = Column(Integer)
    price_coverage_pct = Column(Float)
    filings_considered = Column(Integer)
    scores_computed    = Column(Integer)
    signals_generated  = Column(Integer)
    signals_expired    = Column(Integer)
    signals_blocked    = Column(Integer)

    metrics_json = Column(Text)     # per-arm metric dict
    report_path  = Column(Text)
    notes        = Column(Text)
    error_message = Column(Text)

    strategy_version = Column(String(20), nullable=False, default=STRATEGY_VERSION)


class InsiderScanRun(Base):
    """Live scanner run log — mirrors us52wh_scan_runs so stale data is detectable."""
    __tablename__ = "ins_scan_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_type = Column(String(20), nullable=False)   # 'scheduled' | 'manual'
    start_time   = Column(String(30), nullable=False, default=_now)
    end_time     = Column(String(30))
    status       = Column(String(20))                   # ok | stale_data | error | running

    data_end_date       = Column(String(10))
    latest_filing_date  = Column(String(10))
    universe_scanned    = Column(Integer)
    filings_ingested    = Column(Integer)
    scores_computed     = Column(Integer)
    new_signals         = Column(Integer, default=0)
    triggers_fired      = Column(Integer, default=0)
    signals_expired     = Column(Integer, default=0)
    exits_recorded      = Column(Integer, default=0)
    alerts_sent         = Column(Integer, default=0)

    error_message = Column(Text)
    created_at = Column(String(30), nullable=False, default=_now)


class InsiderPosition(Base):
    """
    Live open positions (created when a Telegram signal is accepted).
    Backtest trades live in ins_trades; this table is live-only state.
    """
    __tablename__ = "ins_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, index=True)

    ticker      = Column(String(20), nullable=False, index=True)
    entry_date  = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=False)
    qty         = Column(Integer, nullable=False)

    initial_stop = Column(Float, nullable=False)
    target_price = Column(Float)
    atr_at_entry = Column(Float)
    initial_risk = Column(Float)

    highest_close_since_entry = Column(Float)
    trailing_stop = Column(Float)
    time_stop_date = Column(String(10))

    status      = Column(String(10), nullable=False, default="open", index=True)
    exit_date   = Column(String(10))
    exit_price  = Column(Float)
    exit_reason = Column(String(25))
    realized_pnl = Column(Float)
    r_multiple   = Column(Float)
    exit_notified = Column(Boolean, default=False)

    score_at_entry = Column(Float)
    cluster_count  = Column(Integer)

    strategy_version = Column(String(20), nullable=False, default=STRATEGY_VERSION)
    created_at = Column(String(30), nullable=False, default=_now)
    updated_at = Column(String(30), nullable=False, default=_now)
