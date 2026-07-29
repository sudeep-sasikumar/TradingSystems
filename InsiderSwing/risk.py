"""
InsiderSwing — position sizing, stops, exits, and the cost model.

SIZING
------
Volatility-based, not fixed-dollar.  A fixed $10k slice puts wildly different
risk on a 2%-ATR mega-cap and a 9%-ATR micro-cap; sizing off the stop distance
equalises them:

    qty = risk_budget / risk_per_share          risk_budget = account × risk_%

Then three caps, applied as a MIN so the tightest always wins:
    1. capital cap    — max_capital_per_trade_pct of the account
    2. liquidity cap  — max_pct_of_adv of average dollar volume
    3. whole shares   — floor

The liquidity cap is the one that matters for small caps and is the reason a
naive backtest of this factor looks better than it is: without it the simulator
happily buys $50k of a name that trades $200k a day.

STOPS
-----
``min(ATR-multiple stop, recent swing low)`` — whichever is TIGHTER (i.e. the
higher price), per spec.  Taking the tighter of the two keeps risk-per-share
honest rather than quietly widening it when structure is far away.

EXITS
-----
Three, first to fire wins:
    stop / trailing stop   — trail activates only once the trade is in profit
    R-multiple target      — target_r_multiple × initial risk
    hard time stop         — time_stop_days trading days, unconditional

The time stop is not optional.  This factor's edge decays; without it the
system silently mutates into a buy-and-hold, which is a different strategy with
different risk and would make the backtest a measurement of something else.

SLIPPAGE
--------
Square-root market impact, not a flat bps assumption:

    slippage_bps = base_spread_bps + impact_coef × sqrt(participation_rate)

where participation_rate = order notional / average daily dollar volume.  A
$5k order in a $100m-a-day name costs the spread; a $50k order in a
$500k-a-day name costs far more, and a flat-bps model would hide exactly that.
Commission is charged separately, both sides.
"""
from __future__ import annotations

import logging
import math
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


# ──────────────────────────────────────────────────────────────────────────────
#  Cost model
# ──────────────────────────────────────────────────────────────────────────────

def slippage_bps(notional: float, adv: Optional[float], config: Optional[cfg.InsiderConfig] = None) -> float:
    """
    One-way slippage in basis points for an order of ``notional`` dollars in a
    name averaging ``adv`` dollars of volume per day.

    When ADV is unknown we charge a deliberately punitive 3× base spread rather
    than assuming the best case — an unmeasurable name is a risky fill, and
    optimistic defaults are how cost models flatter a strategy.
    """
    conf = config or cfg.DEFAULT_CONFIG
    if not adv or adv <= 0:
        return conf.base_spread_bps * 3.0
    participation = max(notional, 0.0) / adv
    return conf.base_spread_bps + conf.impact_coefficient * math.sqrt(participation)


def fill_price(
    reference_price: float,
    side: str,
    notional: float,
    adv: Optional[float],
    config: Optional[cfg.InsiderConfig] = None,
) -> tuple[float, float]:
    """
    (fill_price, cost_per_share).  ``side`` is 'buy' or 'sell'.

    Buys fill above the reference, sells below — slippage always works against
    the trade, in both directions.
    """
    bps = slippage_bps(notional, adv, config)
    adj = reference_price * (bps / 10_000.0)
    return (reference_price + adj, adj) if side == "buy" else (reference_price - adj, adj)


def commission(notional: float, config: Optional[cfg.InsiderConfig] = None) -> float:
    conf = config or cfg.DEFAULT_CONFIG
    return abs(notional) * (conf.commission_bps / 10_000.0)


# ──────────────────────────────────────────────────────────────────────────────
#  Entry plan
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class EntryPlan:
    ok: bool
    reason: Optional[str] = None

    entry_ref_price: Optional[float] = None
    entry_price: Optional[float] = None        # incl. slippage
    qty: int = 0
    notional: float = 0.0

    initial_stop: Optional[float] = None
    stop_basis: Optional[str] = None           # 'atr' | 'swing_low'
    risk_per_share: Optional[float] = None
    target_price: Optional[float] = None
    atr: Optional[float] = None

    adv: Optional[float] = None
    participation_rate: Optional[float] = None
    entry_slippage_cost: float = 0.0
    entry_commission: float = 0.0


def build_entry(
    bar: pd.Series,
    reference_price: float,
    config: Optional[cfg.InsiderConfig] = None,
) -> EntryPlan:
    """
    Turn a confirmation bar into a sized, stopped, targeted entry.

    ``bar`` must carry ATR14, the configured SwingLow column, and AvgDollarVol
    (all produced by prices.add_insider_indicators).  ``reference_price`` is the
    unslipped intended fill — the next session's open in the backtest.
    """
    conf = config or cfg.DEFAULT_CONFIG

    def _val(col: str) -> Optional[float]:
        if col not in bar.index:
            return None
        v = bar[col]
        return None if pd.isna(v) else float(v)

    atr = _val("ATR14")
    if atr is None or atr <= 0:
        return EntryPlan(False, "no ATR available")
    if not reference_price or reference_price <= 0:
        return EntryPlan(False, "no reference price")

    swing_low = _val(f"SwingLow{conf.swing_low_lookback}")
    atr_stop = reference_price - conf.atr_stop_mult * atr

    # "Whichever is tighter" = the HIGHER stop price = the smaller risk.
    if swing_low is not None and swing_low > atr_stop and swing_low < reference_price:
        stop, basis = swing_low, "swing_low"
    else:
        stop, basis = atr_stop, "atr"

    # Floor the stop distance.  A swing low sitting a cent below the entry bar
    # would otherwise give a near-zero risk-per-share, which both explodes the
    # R-multiple and makes the risk-based share count meaningless.  This is a
    # guard against a degenerate structure, not a widening of the stop rule.
    min_distance = max(conf.min_stop_atr_mult * atr,
                       reference_price * conf.min_stop_pct / 100.0)
    if reference_price - stop < min_distance:
        stop = reference_price - min_distance
        basis = f"{basis}_floored"

    risk_per_share = reference_price - stop
    if risk_per_share <= 0:
        return EntryPlan(False, "non-positive risk per share")

    adv = _val("AvgDollarVol")
    if adv is not None and adv < conf.min_avg_dollar_volume:
        return EntryPlan(False, f"ADV ${adv:,.0f} below ${conf.min_avg_dollar_volume:,.0f} floor",
                         adv=adv)

    qty_risk = math.floor(conf.risk_budget() / risk_per_share)
    qty_capital = math.floor(conf.max_capital_per_trade() / reference_price)
    qty_liquidity = (
        math.floor((adv * conf.max_pct_of_adv / 100.0) / reference_price)
        if adv and adv > 0 else qty_capital
    )
    qty = int(min(qty_risk, qty_capital, qty_liquidity))

    if qty <= 0:
        return EntryPlan(
            False,
            f"size floors to zero (risk={qty_risk}, capital={qty_capital}, liquidity={qty_liquidity})",
            adv=adv,
        )

    notional_ref = qty * reference_price
    px, cost_ps = fill_price(reference_price, "buy", notional_ref, adv, conf)
    notional = qty * px

    return EntryPlan(
        ok=True,
        entry_ref_price=reference_price,
        entry_price=px,
        qty=qty,
        notional=notional,
        initial_stop=stop,
        stop_basis=basis,
        risk_per_share=risk_per_share,
        target_price=reference_price + conf.target_r_multiple * risk_per_share,
        atr=atr,
        adv=adv,
        participation_rate=(notional / adv) if adv and adv > 0 else None,
        entry_slippage_cost=cost_ps * qty,
        entry_commission=commission(notional, conf),
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Open-position state machine
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExitEvent:
    exited: bool
    exit_date: Optional[pd.Timestamp] = None
    exit_ref_price: Optional[float] = None
    exit_reason: Optional[str] = None


class OpenPosition:
    """
    Tracks one live/simulated position bar by bar.

    Intrabar convention: when a bar's low breaches the stop AND its high reaches
    the target, the STOP is assumed to fill first.  Daily bars can't resolve the
    ordering, and assuming the favourable one is how backtests invent returns.
    """

    def __init__(self, plan: EntryPlan, entry_date: pd.Timestamp,
                 config: Optional[cfg.InsiderConfig] = None):
        self.conf = config or cfg.DEFAULT_CONFIG
        self.plan = plan
        self.entry_date = entry_date
        self.stop = float(plan.initial_stop)
        self.target = float(plan.target_price)
        self.atr = float(plan.atr)
        self.highest_close = float(plan.entry_ref_price)
        self.bars_held = 0

    def update(self, bar: pd.Series, bar_date: pd.Timestamp) -> ExitEvent:
        """Advance one session.  Returns an ExitEvent (exited=False if still open)."""
        self.bars_held += 1
        low, high, close = float(bar["Low"]), float(bar["High"]), float(bar["Close"])

        # Stop first — see intrabar convention above.
        if low <= self.stop:
            reason = "trail" if self.stop > self.plan.initial_stop else "stop"
            return ExitEvent(True, bar_date, self.stop, reason)

        if high >= self.target:
            return ExitEvent(True, bar_date, self.target, "target")

        if self.bars_held >= self.conf.time_stop_days:
            return ExitEvent(True, bar_date, close, "time_stop")

        # Trail only once the trade is genuinely in profit, and never down.
        if close > self.highest_close:
            self.highest_close = close
        candidate = self.highest_close - self.conf.trail_atr_mult * self.atr
        if candidate > self.stop and self.highest_close > self.plan.entry_ref_price:
            self.stop = candidate

        return ExitEvent(False)

    def close_out(self, ref_price: float, when: pd.Timestamp, reason: str) -> ExitEvent:
        """Force a close — used for delisting and for still-open trades at run end."""
        return ExitEvent(True, when, ref_price, reason)

    def realise(self, exit_ref_price: float) -> dict:
        """P&L for a completed round trip, net of slippage and commission."""
        conf = self.conf
        qty = self.plan.qty
        exit_px, exit_cost_ps = fill_price(
            exit_ref_price, "sell", qty * exit_ref_price, self.plan.adv, conf
        )
        gross = (exit_ref_price - self.plan.entry_ref_price) * qty
        exit_comm = commission(qty * exit_px, conf)
        slippage_total = self.plan.entry_slippage_cost + exit_cost_ps * qty
        costs = slippage_total + self.plan.entry_commission + exit_comm
        net = gross - costs
        notional = self.plan.notional

        return {
            "exit_price": exit_px,
            "exit_ref_price": exit_ref_price,
            "gross_pnl": gross,
            "slippage_cost": costs,          # slippage + both commissions
            "net_pnl": net,
            "return_pct": (net / notional) if notional else None,
            "r_multiple": (net / (qty * self.plan.risk_per_share))
                          if qty and self.plan.risk_per_share else None,
            "holding_days": self.bars_held,
        }


# ──────────────────────────────────────────────────────────────────────────────
#  Portfolio-level cap
# ──────────────────────────────────────────────────────────────────────────────

class PositionBook:
    """
    Concurrency cap for the backtest.

    Signals arriving when the book is full are recorded as skipped with a
    reason, never silently dropped — the same "never silently suppress a signal"
    rule the Nifty and S&P 500 systems follow.
    """

    def __init__(self, max_positions: int):
        self.max_positions = max_positions
        self.open: dict[str, object] = {}
        self.skipped_for_cap = 0

    def has_room(self) -> bool:
        return len(self.open) < self.max_positions

    def holds(self, ticker: str) -> bool:
        return ticker in self.open

    def add(self, ticker: str, position) -> bool:
        if ticker in self.open:
            return False
        if not self.has_room():
            self.skipped_for_cap += 1
            return False
        self.open[ticker] = position
        return True

    def remove(self, ticker: str):
        return self.open.pop(ticker, None)
