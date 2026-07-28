"""
InsiderSwing — technical confirmation triggers.

WHY A TIMING OVERLAY AT ALL
---------------------------
The published insider-purchase edge shows up over 3–6 months, not days.  That
is a horizon mismatch for a swing system: entering on the filing date and
holding for 20 sessions captures only a sliver of a slow drift and eats a lot of
noise on the way.  The overlay says: take the insider signal as a watchlist,
then wait for price to confirm inside a bounded window.

That is a real bet, not a free improvement — requiring confirmation
mechanically discards signals, and if the discarded ones were the good ones the
overlay is a net negative.  This is exactly why the backtest reports the
``insider_only`` arm and the ``tech_only`` arm alongside ``combined``, and why
expired (never-confirmed) signals are logged rather than dropped.

TRIGGERS (any one fires; configurable via INSIDER_TRIGGER_TYPES)
----------------------------------------------------------------
    dma_reclaim     Close crosses back above the 20- or 50-day MA, having been
                    below it on the prior bar.  A trend-repair entry.
    range_breakout  Close exceeds the highest high of the prior N sessions
                    (Donchian).  A base-breakout entry.
    rsi_reset       RSI-14 was <= oversold within the window and has now crossed
                    back above the reset level.  A mean-reversion entry.

All three are evaluated on COMPLETED daily bars only, and every level they
compare against is built with shift(1) in prices.py, so no trigger can see the
bar it fires on before that bar closes.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402

logger = logging.getLogger(__name__)

TRIGGER_DMA = "dma_reclaim"
TRIGGER_BREAKOUT = "range_breakout"
TRIGGER_RSI = "rsi_reset"
ALL_TRIGGERS = (TRIGGER_DMA, TRIGGER_BREAKOUT, TRIGGER_RSI)


@dataclass
class TriggerResult:
    fired: bool
    trigger_date: Optional[pd.Timestamp] = None
    trigger_type: Optional[str] = None
    trigger_price: Optional[float] = None
    reason: Optional[str] = None      # why it did NOT fire, when fired is False


# ──────────────────────────────────────────────────────────────────────────────
#  Per-bar trigger tests
# ──────────────────────────────────────────────────────────────────────────────

def _dma_reclaim(prev: pd.Series, cur: pd.Series) -> bool:
    """Close crosses up through SMA20 or SMA50 (below on the prior bar)."""
    for ma in ("SMA20", "SMA50"):
        p_ma, c_ma = prev.get(ma), cur.get(ma)
        if pd.isna(p_ma) or pd.isna(c_ma):
            continue
        if prev["Close"] <= p_ma and cur["Close"] > c_ma:
            return True
    return False


def _range_breakout(cur: pd.Series, lookback: int) -> bool:
    """Close above the highest high of the prior ``lookback`` sessions."""
    level = cur.get(f"DonchianHigh{lookback}")
    if level is None or pd.isna(level) or level <= 0:
        return False
    return bool(cur["Close"] > level)


def _rsi_reset(window: pd.DataFrame, cur_pos: int, conf: cfg.InsiderConfig) -> bool:
    """
    RSI dipped to/below the oversold level somewhere earlier in the window and
    has now crossed back above the reset level.

    Both halves are required: a stock merely sitting above 50 is not a reset,
    and a stock still oversold has not confirmed anything.
    """
    if cur_pos < 1:
        return False
    rsi = window["RSI14"]
    cur, prev = rsi.iloc[cur_pos], rsi.iloc[cur_pos - 1]
    if pd.isna(cur) or pd.isna(prev):
        return False
    if not (prev <= conf.rsi_reset_level < cur):
        return False
    return bool((rsi.iloc[:cur_pos] <= conf.rsi_oversold).any())


# ──────────────────────────────────────────────────────────────────────────────
#  Window scan
# ──────────────────────────────────────────────────────────────────────────────

def find_trigger(
    price_df: pd.DataFrame,
    from_date: date,
    window_sessions: int,
    config: Optional[cfg.InsiderConfig] = None,
    allowed: Optional[list[str]] = None,
) -> TriggerResult:
    """
    Scan forward from ``from_date`` for the FIRST technical confirmation.

    ``from_date`` should already include the availability lag (the earliest
    session on which a trader could act on the filing).  The scan covers that
    session and the following ``window_sessions - 1`` sessions.

    Returns the first firing bar.  ``fired=False`` with ``reason='window_expired'``
    is a meaningful result and is recorded, not discarded.
    """
    conf = config or cfg.DEFAULT_CONFIG
    types = allowed if allowed is not None else conf.trigger_list()

    if price_df is None or price_df.empty:
        return TriggerResult(False, reason="no_price_data")

    idx = pd.DatetimeIndex(price_df.index)
    start_pos = int(idx.searchsorted(pd.Timestamp(from_date), side="left"))
    if start_pos >= len(idx):
        return TriggerResult(False, reason="no_sessions_after_signal")

    end_pos = min(start_pos + window_sessions, len(idx))
    # One extra bar of history so a cross can be detected on the first session.
    hist_start = max(0, start_pos - 1)
    window = price_df.iloc[hist_start:end_pos]
    offset = start_pos - hist_start

    for pos in range(offset, len(window)):
        cur = window.iloc[pos]
        prev = window.iloc[pos - 1] if pos > 0 else None

        if TRIGGER_DMA in types and prev is not None and _dma_reclaim(prev, cur):
            return TriggerResult(True, window.index[pos], TRIGGER_DMA, float(cur["Close"]))

        if TRIGGER_BREAKOUT in types and _range_breakout(cur, conf.breakout_lookback):
            return TriggerResult(True, window.index[pos], TRIGGER_BREAKOUT, float(cur["Close"]))

        if TRIGGER_RSI in types and _rsi_reset(window, pos, conf):
            return TriggerResult(True, window.index[pos], TRIGGER_RSI, float(cur["Close"]))

    return TriggerResult(False, reason="window_expired")


def trigger_series(
    price_df: pd.DataFrame,
    config: Optional[cfg.InsiderConfig] = None,
    allowed: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    Vectorised-ish trigger map over an entire price history.

    This powers the ``tech_only`` control arm — the base rate of the technical
    system with NO insider filter at all.  Without it there is no way to tell
    whether the insider data contributes anything over the timing rule alone.

    Returns a DataFrame indexed like ``price_df`` with boolean columns per
    trigger type and a combined ``fired`` / ``trigger_type`` pair.
    """
    conf = config or cfg.DEFAULT_CONFIG
    types = allowed if allowed is not None else conf.trigger_list()

    if price_df is None or price_df.empty:
        return pd.DataFrame()

    close = price_df["Close"]
    out = pd.DataFrame(index=price_df.index)

    # dma_reclaim
    dma = pd.Series(False, index=price_df.index)
    for ma in ("SMA20", "SMA50"):
        if ma in price_df.columns:
            m = price_df[ma]
            dma |= (close.shift(1) <= m.shift(1)) & (close > m)
    out[TRIGGER_DMA] = dma.fillna(False) if TRIGGER_DMA in types else False

    # range_breakout
    col = f"DonchianHigh{conf.breakout_lookback}"
    if TRIGGER_BREAKOUT in types and col in price_df.columns:
        out[TRIGGER_BREAKOUT] = (close > price_df[col]).fillna(False)
    else:
        out[TRIGGER_BREAKOUT] = False

    # rsi_reset — "was oversold recently" uses a rolling window equal to the
    # confirmation window, mirroring the per-signal scan's memory.
    if TRIGGER_RSI in types and "RSI14" in price_df.columns:
        rsi = price_df["RSI14"]
        crossed = (rsi.shift(1) <= conf.rsi_reset_level) & (rsi > conf.rsi_reset_level)
        # Kept as float through the rolling window: a boolean rolling().max()
        # round-trips through object dtype and trips pandas' downcasting warning.
        was_oversold = (
            (rsi <= conf.rsi_oversold).astype(float)
            .rolling(conf.confirmation_window_days, min_periods=1).max()
            .shift(1).fillna(0.0) > 0
        )
        out[TRIGGER_RSI] = (crossed.fillna(False) & was_oversold)
    else:
        out[TRIGGER_RSI] = False

    out["fired"] = out[[TRIGGER_DMA, TRIGGER_BREAKOUT, TRIGGER_RSI]].any(axis=1)
    out["trigger_type"] = np.select(
        [out[TRIGGER_DMA], out[TRIGGER_BREAKOUT], out[TRIGGER_RSI]],
        [TRIGGER_DMA, TRIGGER_BREAKOUT, TRIGGER_RSI],
        default=None,
    )
    return out
