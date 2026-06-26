"""
52WeekHighUS Backtest Engine — Three-Version Comparison (Part I)

Version A: buggy original (MAX stop, no capital cap, no regime/RS filters)
Version B: corrected stop only (MIN + 6% cap), no regime/RS filters
Version C: full spec (B1-B4 hard gates + B5 + graded checks, correct sizing)

IMPORTANT: Version A formulas are physically isolated here and must never
be imported or called from signal_logic.py, scanner.py, or bot.py.

Execution assumptions (exact, per spec):
  - Signal fires after close on day t using day t's OHLCV.
  - Entry attempts on days t+1, t+2, t+3: fills at Entry or Open (gap-up).
  - Signal cancelled if entry not triggered within 3 trading days.
  - SL: Low <= StructuralSL -> exit at SL, or Open if gap-down.
  - T1: High >= T1 -> sell QtyT1 at T1.
  - Same-day SL/T1 conflict: SL first, UNLESS Open >= T1 (gap-up past T1).
  - After T1: trailing stop = max_high_since_entry - ATR14_at_signal (fixed).
    Evaluated starting the NEXT day after T1 fills.
  - Time stop: exit remaining position at Close 15 days after entry if T1 never hit.
  - EMA-14 exit (secondary): 2 consecutive closes below EMA-14.
"""
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE   = Path(__file__).resolve().parent   # 52WeekHighUS/backtest/
_MODULE = _HERE.parent                       # 52WeekHighUS/
_ROOT   = _MODULE.parent                     # project root
for _p in (str(_ROOT), str(_MODULE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

from signal_logic import (
    SignalConfig, evaluate_ticker,
    BREAKOUT_BUFFER, ENTRY_BUFFER, SL_BUFFER,
    SL_PCT_CAP, RR_T1_MIN, RR_T2_MIN, T1_ATR_MULT, T2_ATR_MULT,
    assign_tier,
)

logger = logging.getLogger(__name__)

# ── Backtest constants ─────────────────────────────────────────────────────────
BACKTEST_START      = date(2022, 1, 1)   # indicators need 252-day window; data from 2020-01-01
ENTRY_MAX_DAYS      = 3                  # cancel if entry not triggered within N trading days
TIME_STOP_DAYS      = 15                 # exit remaining if T1 never hit after N days
EMA14_CONSEC_CLOSES = 2                  # exit trailing half after N consecutive closes < EMA14

SURVIVORSHIP_BIAS_NOTE = (
    "Backtested on CURRENT S&P 500 constituents only — survivorship bias present, "
    "historical performance likely overstated versus a true point-in-time universe. "
    "Illustrative research estimate only. Not a performance record."
)


# ── Helper ─────────────────────────────────────────────────────────────────────

def _f(row: pd.Series, col: str) -> Optional[float]:
    """Extract a float from a row, returning None if missing/NaN."""
    if col not in row.index:
        return None
    v = row[col]
    if isinstance(v, (pd.Series, np.ndarray)):
        v = v.iloc[0] if isinstance(v, pd.Series) else v[0]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class _Signal:
    """Intermediate signal record produced during scanning."""
    ticker: str
    signal_date: date
    entry_level: float
    structural_sl: float
    atr14: float
    t1: float
    t2: float
    risk_per_share: float
    sl_pct: float
    qty_t1: int
    qty_trailing: int
    final_qty: int
    version: str
    tier: str = "C"


@dataclass
class BacktestTrade:
    """Full lifecycle of one backtest trade."""
    version: str
    ticker: str
    signal_date: date
    entry_level: float
    structural_sl: float
    atr14_at_signal: float
    t1: float
    t2: float
    risk_per_share: float
    sl_pct: float
    qty_t1: int
    qty_trailing: int
    final_qty: int
    tier: str = "C"

    # Entry outcome
    entry_date: Optional[date] = None
    entry_fill: Optional[float] = None

    # T1 stage
    t1_filled: bool = False
    t1_fill_date: Optional[date] = None
    t1_fill_price: Optional[float] = None

    # Exit — trailing half (after T1)
    trailing_exit_date: Optional[date] = None
    trailing_exit_price: Optional[float] = None
    trailing_exit_reason: str = ""   # trailing_stop | time_stop | ema_exit | end_of_data

    # Exit — full position (when T1 never hits)
    full_exit_date: Optional[date] = None
    full_exit_price: Optional[float] = None
    full_exit_reason: str = ""       # sl | time_stop | ema_exit | end_of_data

    # P&L
    initial_risk_dollars: Optional[float] = None
    realized_pl_dollars: Optional[float] = None
    trade_r: Optional[float] = None


@dataclass
class BacktestResult:
    version: str
    description: str
    signals_generated: int = 0
    signals_skipped_sl_pct: int = 0
    signals_skipped_hard_gate: int = 0
    signals_skipped_rr: int = 0
    entries_attempted: int = 0
    entries_expired: int = 0
    entries_filled: int = 0

    # After compute_metrics()
    total_completed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_r: float = 0.0
    median_r: float = 0.0
    profit_factor: float = 0.0
    t1_hit_count: int = 0
    t1_hit_rate: float = 0.0
    avg_r_trailing_half: float = 0.0
    max_drawdown_r: float = 0.0

    trades: list = field(default_factory=list)   # list[BacktestTrade]


# ── Version A signal evaluation (ISOLATED — buggy formulas) ───────────────────

def _v_a_signal(ticker: str, signal_date: date, row: pd.Series,
                config: SignalConfig) -> Optional[_Signal]:
    """
    VERSION A ONLY — BUGGY BASELINE. NEVER import or call from live code paths.

    Bugs (preserved intentionally for comparison):
      StructuralSL = MAX(low, swing_low) * SL_BUFFER  [should be MIN]
      FinalQty = floor(risk_budget / rps)              [no capital cap]
      No SL% cap.  No regime/trend gates.  Only B4.
    """
    close      = _f(row, "Close")
    low_today  = _f(row, "Low")
    atr14      = _f(row, "ATR14")
    prior_252h = _f(row, "Prior252High")
    swing_low5 = _f(row, "SwingLow5")

    if any(v is None for v in [close, low_today, atr14, prior_252h]):
        return None
    if prior_252h <= 0 or close <= prior_252h * BREAKOUT_BUFFER:
        return None

    entry = close * ENTRY_BUFFER

    # BUG: MAX instead of MIN
    sl_base = max(low_today, swing_low5) if swing_low5 is not None else low_today
    structural_sl = sl_base * SL_BUFFER

    rps = entry - structural_sl
    if rps <= 0:
        return None

    sl_pct = rps / entry * 100.0   # no cap in Version A

    t1 = entry + T1_ATR_MULT * atr14
    t2 = entry + T2_ATR_MULT * atr14

    # BUG: no capital cap
    qty = math.floor(config.risk_budget() / rps)
    if qty <= 0:
        return None
    qty_t1 = math.floor(qty / 2)

    return _Signal(
        ticker=ticker, signal_date=signal_date,
        entry_level=entry, structural_sl=structural_sl, atr14=atr14,
        t1=t1, t2=t2, risk_per_share=rps, sl_pct=sl_pct,
        qty_t1=qty_t1, qty_trailing=qty - qty_t1, final_qty=qty,
        version="A",
    )


# ── Version B signal evaluation ────────────────────────────────────────────────
# Return type: (signal_or_None, skip_reason_or_None)
# skip_reason: "sl_pct" | "qty" | None (None means signal generated)
# NOTE: Version B intentionally omits the R:R check — that gate (B5 full spec)
# belongs only in Version C via evaluate_ticker.  B applies B4 + the 6% SL% cap
# only, so the signal count comparison shows purely the effect of the stop formula.

def _v_b_signal(
    ticker: str, signal_date: date, row: pd.Series, config: SignalConfig,
) -> tuple[Optional[_Signal], Optional[str]]:
    """
    VERSION B — Corrected stop (MIN + 6% SL cap) + capital cap.
    Gates applied: B4 (fresh breakout) + 6% SL% cap.
    R:R check excluded — that belongs in Version C (full spec).
    Returns (signal, skip_reason) where skip_reason is None on success.
    """
    close      = _f(row, "Close")
    low_today  = _f(row, "Low")
    atr14      = _f(row, "ATR14")
    prior_252h = _f(row, "Prior252High")
    swing_low5 = _f(row, "SwingLow5")

    if any(v is None for v in [close, low_today, atr14, prior_252h]):
        return None, None   # missing data — not a candidate, don't count as skip

    if prior_252h <= 0 or close <= prior_252h * BREAKOUT_BUFFER:
        return None, None   # B4 failed — not a breakout candidate

    entry = close * ENTRY_BUFFER

    # Correct: MIN (not MAX like Version A)
    sl_base = min(low_today, swing_low5) if swing_low5 is not None else low_today
    structural_sl = sl_base * SL_BUFFER

    rps = entry - structural_sl
    if rps <= 0:
        return None, None

    sl_pct = rps / entry * 100.0
    if sl_pct > SL_PCT_CAP:
        return None, "sl_pct"   # B5 partial: 6% cap (R:R excluded — see Version C)

    t1 = entry + T1_ATR_MULT * atr14
    t2 = entry + T2_ATR_MULT * atr14

    qty_risk = math.floor(config.risk_budget() / rps)
    qty_cap  = math.floor(config.max_capital_per_trade / entry)
    qty      = min(qty_risk, qty_cap)
    if qty <= 0:
        return None, "qty"
    qty_t1 = math.floor(qty / 2)

    return _Signal(
        ticker=ticker, signal_date=signal_date,
        entry_level=entry, structural_sl=structural_sl, atr14=atr14,
        t1=t1, t2=t2, risk_per_share=rps, sl_pct=sl_pct,
        qty_t1=qty_t1, qty_trailing=qty - qty_t1, final_qty=qty,
        version="B",
    ), None


# ── Version C signal evaluation ────────────────────────────────────────────────

def _v_c_signal(
    ticker: str,
    company_name: str,
    gics_sector: str,
    signal_date: date,
    ticker_df: pd.DataFrame,
    spy_df: pd.DataFrame,
    sector_etf_df: Optional[pd.DataFrame],
    prior_signals_df: Optional[pd.DataFrame],
    config: SignalConfig,
) -> Optional[_Signal]:
    """
    VERSION C — Full spec: uses evaluate_ticker from signal_logic.py.
    Hard gates B1-B4, risk gate B5, and graded checks B6-B10.
    """
    r = evaluate_ticker(
        ticker=ticker,
        company_name=company_name,
        gics_sector=gics_sector,
        ticker_df=ticker_df,
        spy_df=spy_df,
        sector_etf_df=sector_etf_df,
        prior_signals_df=prior_signals_df,
        config=config,
        as_of_date=signal_date,
        fetch_earnings=False,  # earnings lookup too slow for 500-ticker backtest
    )
    if r.action != "SIGNAL":
        return None
    return _Signal(
        ticker=ticker, signal_date=signal_date,
        entry_level=r.entry, structural_sl=r.structural_sl, atr14=r.atr14,
        t1=r.t1, t2=r.t2, risk_per_share=r.risk_per_share, sl_pct=r.sl_pct,
        qty_t1=r.qty_t1, qty_trailing=r.qty_trailing, final_qty=r.final_qty,
        version="C", tier=r.tier or "C",
    )


# ── Position simulation ────────────────────────────────────────────────────────

def _simulate_trade(
    sig: _Signal,
    ticker_df: pd.DataFrame,
    all_trade_dates: list[date],
) -> Optional[BacktestTrade]:
    """
    Simulate one trade from signal through full exit.
    Returns a BacktestTrade (possibly expired or end-of-data).
    Returns None only if the ticker_df has no data at or after signal_date.
    """
    # Index ticker_df by date for fast lookup
    df_by_date: dict[date, pd.Series] = {
        d.date() if isinstance(d, pd.Timestamp) else d: row
        for d, row in ticker_df.iterrows()
    }

    trade = BacktestTrade(
        version=sig.version, ticker=sig.ticker, signal_date=sig.signal_date,
        entry_level=sig.entry_level, structural_sl=sig.structural_sl,
        atr14_at_signal=sig.atr14, t1=sig.t1, t2=sig.t2,
        risk_per_share=sig.risk_per_share, sl_pct=sig.sl_pct,
        qty_t1=sig.qty_t1, qty_trailing=sig.qty_trailing, final_qty=sig.final_qty,
        tier=sig.tier,
    )

    # Find position of signal_date in all_trade_dates
    try:
        sig_idx = all_trade_dates.index(sig.signal_date)
    except ValueError:
        return None   # signal date not in trading calendar (shouldn't happen)

    future_dates = all_trade_dates[sig_idx + 1:]
    if not future_dates:
        return None

    # ── Phase 1: Entry attempt ─────────────────────────────────────────────────
    entry_fill = None
    entry_date = None
    for i, dt in enumerate(future_dates[:ENTRY_MAX_DAYS]):
        row = df_by_date.get(dt)
        if row is None:
            continue
        high = _f(row, "High")
        open_ = _f(row, "Open")
        if high is None:
            continue
        if high >= sig.entry_level:
            entry_date = dt
            entry_fill = open_ if (open_ is not None and open_ > sig.entry_level) else sig.entry_level
            break

    if entry_fill is None:
        # Signal expired — entry never triggered within ENTRY_MAX_DAYS
        return None   # caller handles count

    trade.entry_date = entry_date
    trade.entry_fill = entry_fill
    trade.initial_risk_dollars = sig.final_qty * (entry_fill - sig.structural_sl)

    # ── Phase 2: Pre-T1 exit simulation ───────────────────────────────────────
    entry_idx = future_dates.index(entry_date)
    post_entry_dates = future_dates[entry_idx + 1:]  # start next day after entry

    days_in_trade = 0
    consec_below_ema = 0
    t1_fill_date = None
    t1_fill_price = None

    for dt in post_entry_dates:
        row = df_by_date.get(dt)
        if row is None:
            continue

        days_in_trade += 1
        open_  = _f(row, "Open")
        high   = _f(row, "High")
        low    = _f(row, "Low")
        close  = _f(row, "Close")
        ema14  = _f(row, "EMA14")

        if any(v is None for v in [open_, high, low, close]):
            continue

        # Gap-up past T1 at open → T1 fills first (regardless of SL)
        if open_ >= sig.t1:
            t1_fill_date  = dt
            t1_fill_price = open_
            break

        # Same-day SL/T1 conflict (both touched intraday):
        # conservative: SL first, since we can't tell intraday sequence
        sl_touched = low <= sig.structural_sl
        t1_touched = high >= sig.t1

        if sl_touched and t1_touched:
            # SL fires first (conservative)
            exit_price = (open_ if open_ < sig.structural_sl else sig.structural_sl)
            trade.full_exit_date   = dt
            trade.full_exit_price  = exit_price
            trade.full_exit_reason = "sl"
            break

        if sl_touched:
            exit_price = (open_ if open_ < sig.structural_sl else sig.structural_sl)
            trade.full_exit_date   = dt
            trade.full_exit_price  = exit_price
            trade.full_exit_reason = "sl"
            break

        if t1_touched:
            t1_fill_date  = dt
            t1_fill_price = sig.t1
            break

        # EMA-14 exit check (secondary, before time stop)
        if ema14 is not None and close < ema14:
            consec_below_ema += 1
            if consec_below_ema >= EMA14_CONSEC_CLOSES:
                trade.full_exit_date   = dt
                trade.full_exit_price  = close
                trade.full_exit_reason = "ema_exit"
                break
        else:
            consec_below_ema = 0

        # Time stop: 15 trading days without T1
        if days_in_trade >= TIME_STOP_DAYS:
            trade.full_exit_date   = dt
            trade.full_exit_price  = close
            trade.full_exit_reason = "time_stop"
            break
    else:
        # End of data without exit in phase 2
        if t1_fill_date is None and trade.full_exit_date is None:
            # No exit found — last known price
            last_dt = post_entry_dates[-1]
            last_row = df_by_date.get(last_dt)
            if last_row is not None and _f(last_row, "Close") is not None:
                trade.full_exit_date   = last_dt
                trade.full_exit_price  = _f(last_row, "Close")
                trade.full_exit_reason = "end_of_data"

    # ── Phase 3: Post-T1 trailing simulation ──────────────────────────────────
    if t1_fill_date is not None:
        trade.t1_filled     = True
        trade.t1_fill_date  = t1_fill_date
        trade.t1_fill_price = t1_fill_price

        # Trailing stop: max_high tracks from entry (initial reference = entry)
        max_high = max(entry_fill, _f(df_by_date.get(t1_fill_date, pd.Series()), "High") or entry_fill)
        trail_amount = sig.atr14   # ATR14 at signal date, fixed

        # Get dates after T1 fill
        t1_idx = future_dates.index(t1_fill_date)
        post_t1_dates = future_dates[t1_idx + 1:]   # start NEXT day

        consec_below_ema = 0

        for dt in post_t1_dates:
            row = df_by_date.get(dt)
            if row is None:
                continue

            open_  = _f(row, "Open")
            high   = _f(row, "High")
            low    = _f(row, "Low")
            close  = _f(row, "Close")
            ema14  = _f(row, "EMA14")

            if any(v is None for v in [open_, high, low, close]):
                continue

            if high is not None:
                max_high = max(max_high, high)

            trail_stop = max_high - trail_amount

            # Trailing stop hit
            if low <= trail_stop:
                exit_price = (open_ if open_ < trail_stop else trail_stop)
                trade.trailing_exit_date   = dt
                trade.trailing_exit_price  = exit_price
                trade.trailing_exit_reason = "trailing_stop"
                break

            # EMA-14 exit (2 consecutive closes below)
            if ema14 is not None and close < ema14:
                consec_below_ema += 1
                if consec_below_ema >= EMA14_CONSEC_CLOSES:
                    trade.trailing_exit_date   = dt
                    trade.trailing_exit_price  = close
                    trade.trailing_exit_reason = "ema_exit"
                    break
            else:
                consec_below_ema = 0
        else:
            # End of data — exit trailing half at last close
            if post_t1_dates:
                last_dt  = post_t1_dates[-1]
                last_row = df_by_date.get(last_dt)
                if last_row is not None:
                    trade.trailing_exit_date   = last_dt
                    trade.trailing_exit_price  = _f(last_row, "Close") or t1_fill_price
                    trade.trailing_exit_reason = "end_of_data"

    # ── Compute TradeR ─────────────────────────────────────────────────────────
    _compute_trade_r(trade)
    return trade


def _compute_trade_r(trade: BacktestTrade) -> None:
    """
    Fill trade.trade_r and trade.realized_pl_dollars.

    TradeRealizedPL = (QtyT1 × (T1Fill - EntryFill)) + (QtyTrailing × (TrailExit - EntryFill))
    If T1 never filled, collapses to full-position SL exit:
      TradeRealizedPL = FinalQty × (FullExit - EntryFill)
    TradeR = TradeRealizedPL / InitialRisk
    """
    if trade.entry_fill is None or trade.initial_risk_dollars is None:
        return
    if trade.initial_risk_dollars <= 0:
        return

    if trade.t1_filled and trade.t1_fill_price is not None and trade.trailing_exit_price is not None:
        pl = (trade.qty_t1 * (trade.t1_fill_price - trade.entry_fill) +
              trade.qty_trailing * (trade.trailing_exit_price - trade.entry_fill))
    elif not trade.t1_filled and trade.full_exit_price is not None:
        pl = trade.final_qty * (trade.full_exit_price - trade.entry_fill)
    else:
        return   # incomplete trade, skip

    trade.realized_pl_dollars = pl
    trade.trade_r = pl / trade.initial_risk_dollars


# ── Metrics computation ────────────────────────────────────────────────────────

def compute_metrics(result: BacktestResult) -> None:
    """Compute summary statistics from result.trades, in-place."""
    completed = [t for t in result.trades if t.trade_r is not None]
    result.total_completed = len(completed)
    if not completed:
        return

    rs = [t.trade_r for t in completed]
    result.wins   = sum(1 for r in rs if r > 0)
    result.losses = sum(1 for r in rs if r <= 0)
    result.win_rate    = result.wins / len(rs) if rs else 0.0
    result.avg_r       = float(np.mean(rs))
    result.median_r    = float(np.median(rs))

    gross_win  = sum(r for r in rs if r > 0)
    gross_loss = abs(sum(r for r in rs if r <= 0))
    result.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # T1 stats
    t1_hit = [t for t in completed if t.t1_filled]
    result.t1_hit_count = len(t1_hit)
    result.t1_hit_rate  = len(t1_hit) / len(completed) if completed else 0.0

    # Average R on trailing half for T1-hit trades
    if t1_hit:
        trailing_rs = []
        for t in t1_hit:
            if t.trailing_exit_price is not None and t.initial_risk_dollars and t.initial_risk_dollars > 0:
                trailing_pl = t.qty_trailing * (t.trailing_exit_price - t.entry_fill)
                trailing_r  = trailing_pl / t.initial_risk_dollars
                trailing_rs.append(trailing_r)
        result.avg_r_trailing_half = float(np.mean(trailing_rs)) if trailing_rs else 0.0

    # Max drawdown in cumulative R terms
    cumulative = np.cumsum(rs)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = cumulative - running_max
    result.max_drawdown_r = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0


# ── Main backtest runner ───────────────────────────────────────────────────────

def run_backtest(
    ticker_data: dict[str, pd.DataFrame],     # {ticker: df_with_indicators}
    spy_df: pd.DataFrame,
    sector_dfs: dict[str, pd.DataFrame],      # {etf_ticker: df_with_indicators}
    universe_df: pd.DataFrame,                # columns: ticker, company_name, gics_sector, sector_etf
    config: Optional[SignalConfig] = None,
    backtest_start: date = BACKTEST_START,
    backtest_end: Optional[date] = None,
) -> dict[str, BacktestResult]:
    """
    Run Versions A, B, and C over the same historical period.

    Returns dict with keys "A", "B", "C" mapping to BacktestResult objects.
    """
    if config is None:
        config = SignalConfig()

    # Build {ticker: company_name/sector} lookup from universe_df
    meta: dict[str, dict] = {}
    for _, row in universe_df.iterrows():
        meta[row["ticker"]] = {
            "company_name": row.get("company_name", row["ticker"]),
            "gics_sector":  row.get("gics_sector", ""),
            "sector_etf":   row.get("sector_etf", None),
        }

    # Collect all trading dates across the full period (use SPY as the market calendar)
    spy_dates: list[date] = sorted(
        [d.date() if isinstance(d, pd.Timestamp) else d for d in spy_df.index]
    )
    if backtest_end is None:
        backtest_end = spy_dates[-1] if spy_dates else date.today()

    trading_dates = [d for d in spy_dates if backtest_start <= d <= backtest_end]
    if not trading_dates:
        logger.warning("No trading dates in backtest window %s – %s", backtest_start, backtest_end)
        return {}

    logger.info(
        "Backtest window: %s to %s (%d trading days, %d tickers)",
        trading_dates[0], trading_dates[-1], len(trading_dates), len(ticker_data),
    )

    results = {
        "A": BacktestResult(version="A",
             description="Version A — buggy baseline (MAX stop, no capital cap, no regime filters)"),
        "B": BacktestResult(version="B",
             description="Version B — corrected stop (MIN + 6% cap), no regime filters"),
        "C": BacktestResult(version="C",
             description="Version C — full spec (B1-B4 + B5 + graded checks)"),
    }

    # Precompute date → row index for each ticker and for SPY
    spy_by_date: dict[date, pd.Series] = {
        (d.date() if isinstance(d, pd.Timestamp) else d): row
        for d, row in spy_df.iterrows()
    }
    sector_by_date: dict[str, dict[date, pd.Series]] = {
        etf: {
            (d.date() if isinstance(d, pd.Timestamp) else d): row
            for d, row in df.iterrows()
        }
        for etf, df in sector_dfs.items()
    }
    ticker_by_date: dict[str, dict[date, pd.Series]] = {
        ticker: {
            (d.date() if isinstance(d, pd.Timestamp) else d): row
            for d, row in df.iterrows()
        }
        for ticker, df in ticker_data.items()
    }

    # Pending signals: {version: list of (signal, attempts_remaining)}
    # attempts_remaining counts down from ENTRY_MAX_DAYS on each trading day with data.
    pending: dict[str, list[tuple[_Signal, int]]] = {"A": [], "B": [], "C": []}

    # Open positions: {version: {ticker: BacktestTrade}}
    open_pos: dict[str, dict[str, BacktestTrade]] = {"A": {}, "B": {}, "C": {}}

    # Ephemeral simulation state per open position (not stored in BacktestTrade):
    # {version: {ticker: {"days_held": int, "consec_ema": int, "max_high": float}}}
    pos_state: dict[str, dict[str, dict]] = {"A": {}, "B": {}, "C": {}}

    completed_trades: dict[str, list[BacktestTrade]] = {"A": [], "B": [], "C": []}
    prior_signals_c: list[dict] = []

    total_dates = len(trading_dates)
    for day_idx, today in enumerate(trading_dates):
        if day_idx % 50 == 0:
            logger.info("  Scanning day %d/%d (%s)...", day_idx + 1, total_dates, today)

        # ── Step 1: Attempt entries for pending signals (from PREVIOUS days) ────
        # Signals fire AFTER close on signal_date; entry attempts begin on signal_date+1.
        for version in ("A", "B", "C"):
            still_pending: list[tuple[_Signal, int]] = []
            for sig, remaining in pending[version]:
                # Skip if signal fired today — entry not allowed until tomorrow
                if sig.signal_date >= today:
                    still_pending.append((sig, remaining))
                    continue

                # Skip if ticker already has an open position (shouldn't happen,
                # but guard against a late-arriving duplicate signal)
                if sig.ticker in open_pos[version]:
                    continue

                row = ticker_by_date.get(sig.ticker, {}).get(today)
                if row is None:
                    # No data for this ticker today — don't consume an attempt
                    still_pending.append((sig, remaining))
                    continue

                high  = _f(row, "High")
                open_ = _f(row, "Open")

                if high is not None and high >= sig.entry_level:
                    fill = (open_ if (open_ is not None and open_ > sig.entry_level)
                            else sig.entry_level)
                    trade = BacktestTrade(
                        version=sig.version, ticker=sig.ticker,
                        signal_date=sig.signal_date,
                        entry_level=sig.entry_level,
                        structural_sl=sig.structural_sl,
                        atr14_at_signal=sig.atr14,
                        t1=sig.t1, t2=sig.t2,
                        risk_per_share=sig.risk_per_share,
                        sl_pct=sig.sl_pct,
                        qty_t1=sig.qty_t1, qty_trailing=sig.qty_trailing,
                        final_qty=sig.final_qty, tier=sig.tier,
                        entry_date=today, entry_fill=fill,
                        initial_risk_dollars=sig.final_qty * (fill - sig.structural_sl),
                    )
                    open_pos[version][sig.ticker] = trade
                    pos_state[version][sig.ticker] = {
                        "days_held":  0,
                        "consec_ema": 0,
                        "max_high":   fill,   # initial reference = entry fill (per spec)
                    }
                    results[version].entries_filled += 1
                else:
                    # Entry not triggered — consume one attempt
                    new_remaining = remaining - 1
                    if new_remaining <= 0:
                        results[version].entries_expired += 1
                    else:
                        still_pending.append((sig, new_remaining))
            pending[version] = still_pending

        # ── Step 2: Evaluate exits for open positions ────────────────────────────
        for version in ("A", "B", "C"):
            closed_tickers: list[str] = []
            for ticker, trade in open_pos[version].items():
                row = ticker_by_date.get(ticker, {}).get(today)
                if row is None:
                    continue

                open_  = _f(row, "Open")
                high   = _f(row, "High")
                low    = _f(row, "Low")
                close  = _f(row, "Close")
                ema14  = _f(row, "EMA14")

                if any(v is None for v in [open_, high, low, close]):
                    continue

                state = pos_state[version][ticker]

                if not trade.t1_filled:
                    state["days_held"] += 1

                    # Gap-up past T1 at open → T1 fills at open (spec: if Open >= T1)
                    if open_ >= trade.t1:
                        trade.t1_filled     = True
                        trade.t1_fill_date  = today
                        trade.t1_fill_price = open_
                        state["max_high"]   = max(state["max_high"], open_)
                        state["consec_ema"] = 0
                        continue   # trailing portion still open; evaluated next day

                    sl_hit = low  <= trade.structural_sl
                    t1_hit = high >= trade.t1

                    if sl_hit and t1_hit:
                        # Same-day conflict: SL first (conservative, per spec)
                        ep = open_ if open_ < trade.structural_sl else trade.structural_sl
                        trade.full_exit_date   = today
                        trade.full_exit_price  = ep
                        trade.full_exit_reason = "sl"
                        closed_tickers.append(ticker)
                    elif sl_hit:
                        ep = open_ if open_ < trade.structural_sl else trade.structural_sl
                        trade.full_exit_date   = today
                        trade.full_exit_price  = ep
                        trade.full_exit_reason = "sl"
                        closed_tickers.append(ticker)
                    elif t1_hit:
                        trade.t1_filled     = True
                        trade.t1_fill_date  = today
                        trade.t1_fill_price = trade.t1
                        state["max_high"]   = max(state["max_high"], high)
                        state["consec_ema"] = 0
                        # trailing portion still open; trailing stop evaluated next day
                    else:
                        # EMA-14 exit check (secondary, before time stop)
                        if ema14 is not None and close < ema14:
                            state["consec_ema"] += 1
                            if state["consec_ema"] >= EMA14_CONSEC_CLOSES:
                                trade.full_exit_date   = today
                                trade.full_exit_price  = close
                                trade.full_exit_reason = "ema_exit"
                                closed_tickers.append(ticker)
                        else:
                            state["consec_ema"] = 0

                        # Time stop: 15 trading days after entry without T1
                        if not trade.full_exit_date and state["days_held"] >= TIME_STOP_DAYS:
                            trade.full_exit_date   = today
                            trade.full_exit_price  = close
                            trade.full_exit_reason = "time_stop"
                            closed_tickers.append(ticker)

                else:
                    # Post-T1: trailing stop evaluation — starts NEXT day after T1 fill
                    if trade.t1_fill_date == today:
                        continue   # T1 filled today; evaluate trailing from tomorrow

                    state["max_high"] = max(state["max_high"], high)
                    trail_stop = state["max_high"] - trade.atr14_at_signal

                    if low <= trail_stop:
                        ep = open_ if open_ < trail_stop else trail_stop
                        trade.trailing_exit_date   = today
                        trade.trailing_exit_price  = ep
                        trade.trailing_exit_reason = "trailing_stop"
                        closed_tickers.append(ticker)
                    else:
                        if ema14 is not None and close < ema14:
                            state["consec_ema"] += 1
                            if state["consec_ema"] >= EMA14_CONSEC_CLOSES:
                                trade.trailing_exit_date   = today
                                trade.trailing_exit_price  = close
                                trade.trailing_exit_reason = "ema_exit"
                                closed_tickers.append(ticker)
                        else:
                            state["consec_ema"] = 0

            # Remove closed positions and compute P&L
            for ticker in closed_tickers:
                trade = open_pos[version].pop(ticker)
                pos_state[version].pop(ticker, None)
                _compute_trade_r(trade)
                completed_trades[version].append(trade)

        # ── Step 3: Detect new signals on day today (for entry starting tomorrow) ─
        for ticker, t_by_date in ticker_by_date.items():
            row = t_by_date.get(today)
            if row is None:
                continue

            m = meta.get(ticker, {})

            for version in ("A", "B", "C"):
                # No re-entry while position is open
                if ticker in open_pos[version]:
                    continue
                # Don't add a duplicate pending signal for the same ticker
                if any(s.ticker == ticker for s, _ in pending[version]):
                    continue

                if version == "A":
                    sig = _v_a_signal(ticker, today, row, config)
                    if sig is None:
                        continue

                elif version == "B":
                    sig, skip_reason = _v_b_signal(ticker, today, row, config)
                    if sig is None:
                        if skip_reason == "sl_pct":
                            results["B"].signals_skipped_sl_pct += 1
                        continue

                else:  # C
                    sector_etf = m.get("sector_etf")
                    sector_df_slice = sector_dfs.get(sector_etf) if sector_etf else None
                    prior_df = (
                        pd.DataFrame(prior_signals_c, columns=["ticker", "signal_date"])
                        if prior_signals_c else None
                    )
                    # Call evaluate_ticker directly so we can count skip reasons
                    r_c = evaluate_ticker(
                        ticker=ticker,
                        company_name=m.get("company_name", ticker),
                        gics_sector=m.get("gics_sector", ""),
                        ticker_df=ticker_data[ticker],
                        spy_df=spy_df,
                        sector_etf_df=sector_df_slice,
                        prior_signals_df=prior_df,
                        config=config,
                        as_of_date=today,
                        fetch_earnings=False,
                    )
                    if r_c.action != "SIGNAL":
                        if r_c.action == "SKIP_HARD_GATE":
                            results["C"].signals_skipped_hard_gate += 1
                        elif r_c.action == "SKIP_RISK":
                            results["C"].signals_skipped_sl_pct += 1
                        # SKIP_COOLDOWN / SKIP_DATA: not counted (not a breakout candidate)
                        continue
                    sig = _Signal(
                        ticker=ticker, signal_date=today,
                        entry_level=r_c.entry, structural_sl=r_c.structural_sl,
                        atr14=r_c.atr14, t1=r_c.t1, t2=r_c.t2,
                        risk_per_share=r_c.risk_per_share, sl_pct=r_c.sl_pct,
                        qty_t1=r_c.qty_t1, qty_trailing=r_c.qty_trailing,
                        final_qty=r_c.final_qty, version="C", tier=r_c.tier or "C",
                    )
                    prior_signals_c.append({"ticker": ticker, "signal_date": today})

                results[version].signals_generated += 1
                pending[version].append((sig, ENTRY_MAX_DAYS))

    # ── End of backtest: close any still-open positions at last available price ──
    last_date = trading_dates[-1]
    for version in ("A", "B", "C"):
        for ticker, trade in open_pos[version].items():
            row = ticker_by_date.get(ticker, {}).get(last_date)
            close = _f(row, "Close") if row is not None else None
            if close is None:
                continue
            if trade.t1_filled:
                trade.trailing_exit_date   = last_date
                trade.trailing_exit_price  = close
                trade.trailing_exit_reason = "end_of_data"
            else:
                trade.full_exit_date   = last_date
                trade.full_exit_price  = close
                trade.full_exit_reason = "end_of_data"
            _compute_trade_r(trade)
            completed_trades[version].append(trade)

    # Attach trades to results and compute metrics
    for version in ("A", "B", "C"):
        results[version].trades = completed_trades[version]
        results[version].entries_attempted = (
            results[version].entries_filled + results[version].entries_expired
        )
        compute_metrics(results[version])

    return results


# ── Reporting ──────────────────────────────────────────────────────────────────

def print_report(results: dict[str, BacktestResult]) -> None:
    """Print a side-by-side comparison of all three versions."""
    sep = "-" * 70

    print(f"\n{sep}")
    print("  US S&P 500 52-Week High Breakout — Backtest Research Estimate")
    print(f"  {SURVIVORSHIP_BIAS_NOTE[:80]}")
    print(f"  {SURVIVORSHIP_BIAS_NOTE[80:]}")
    print(sep)

    for version, res in results.items():
        print(f"\n  {res.description}")
        print(f"  {sep[:60]}")
        print(f"  Signals generated         : {res.signals_generated:>8,}")
        if version in ("B", "C"):
            print(f"  Skipped (SL% > 6%)        : {res.signals_skipped_sl_pct:>8,}")
        if version == "C":
            print(f"  Skipped (hard gate B1-B4) : {res.signals_skipped_hard_gate:>8,}")
        print(f"  Entry attempts            : {res.entries_attempted:>8,}")
        print(f"    Entries filled          : {res.entries_filled:>8,}")
        print(f"    Entries expired (3d)    : {res.entries_expired:>8,}")
        print(f"  Completed trades          : {res.total_completed:>8,}")
        if res.total_completed > 0:
            print(f"  Win rate                  : {res.win_rate*100:>7.1f}%")
            print(f"  Avg R                     : {res.avg_r:>8.3f}")
            print(f"  Median R                  : {res.median_r:>8.3f}")
            print(f"  Profit factor             : {res.profit_factor:>8.2f}")
            print(f"  T1 hit rate               : {res.t1_hit_rate*100:>7.1f}%")
            print(f"  Avg R (trailing half)     : {res.avg_r_trailing_half:>8.3f}")
            print(f"  Max drawdown (cumulative R): {res.max_drawdown_r:>7.2f}R")

    print(f"\n{sep}\n")


def trades_to_df(trades: list[BacktestTrade]) -> pd.DataFrame:
    """Convert a list of BacktestTrade to a flat DataFrame for export/display."""
    rows = []
    for t in trades:
        rows.append({
            "version":             t.version,
            "ticker":              t.ticker,
            "tier":                t.tier,
            "signal_date":         t.signal_date,
            "entry_date":          t.entry_date,
            "entry_fill":          t.entry_fill,
            "structural_sl":       t.structural_sl,
            "sl_pct":              t.sl_pct,
            "t1":                  t.t1,
            "t2":                  t.t2,
            "atr14_at_signal":     t.atr14_at_signal,
            "qty_t1":              t.qty_t1,
            "qty_trailing":        t.qty_trailing,
            "final_qty":           t.final_qty,
            "t1_filled":           t.t1_filled,
            "t1_fill_date":        t.t1_fill_date,
            "t1_fill_price":       t.t1_fill_price,
            "trailing_exit_date":  t.trailing_exit_date,
            "trailing_exit_price": t.trailing_exit_price,
            "trailing_exit_reason":t.trailing_exit_reason,
            "full_exit_date":      t.full_exit_date,
            "full_exit_price":     t.full_exit_price,
            "full_exit_reason":    t.full_exit_reason,
            "initial_risk_$":      t.initial_risk_dollars,
            "realized_pl_$":       t.realized_pl_dollars,
            "trade_r":             t.trade_r,
        })
    return pd.DataFrame(rows)
