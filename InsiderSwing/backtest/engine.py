"""
InsiderSwing — three-arm event-driven backtest engine.

THE THREE ARMS (all run over the same universe, calendar, costs and caps)
-------------------------------------------------------------------------
    insider_only  Enter ``signal_lag_days`` sessions after the filing, with no
                  technical condition at all.  The pure factor.
    tech_only     Enter on the technical trigger with NO insider filter.  The
                  base rate of the timing rule on its own.
    combined      Insider signal AND technical confirmation inside the window.

Reporting only ``combined`` would be meaningless.  If it beats buy-and-hold but
not ``tech_only``, the insider data contributes nothing.  If it loses to
``insider_only``, the timing overlay is destroying edge rather than adding it.
Both of those are real possible outcomes and the report states them plainly.

LOOKAHEAD PREVENTION — the things that actually go wrong here
-------------------------------------------------------------
1. Signals key on ``filing_date``, never ``transaction_date``.  Enforced in
   scoring.py; the engine only ever sees filing dates.
2. Availability lag: entry is scheduled ``signal_lag_days`` sessions AFTER the
   filing date, then filled at the NEXT session's open.  A filing accepted
   after hours is not tradeable at that day's close.
3. Index membership is checked on the ENTRY date against the point-in-time
   membership table, so a company that joined the index in 2021 is not tradeable
   in 2018.
4. Delisted names are held in the universe for the period they existed and are
   closed at their last print with ``exit_reason='delisted'`` — not silently
   removed, which would delete their losses.
5. Every rolling indicator is built with shift(1) in prices.py, so no bar can
   see itself.

INTRABAR CONVENTION
-------------------
Stop before target on a bar that touches both (see risk.OpenPosition.update).
Daily bars cannot resolve the ordering and assuming the favourable one is how a
backtest invents returns it never had.

CAPITAL CONSTRAINT AND SAME-DAY RANKING
---------------------------------------
All three arms share one concurrency cap so the comparison is like-for-like.
When more candidates arrive on a day than there is room for:

  * insider arms rank by conviction score, descending — that is the actual
    policy, not a bias.
  * ``tech_only`` has no score, so it ranks by a deterministic hash of
    (ticker, date).  Ranking by liquidity or alphabetically would systematically
    select a subset of the trigger population; a stable hash gives an unbiased
    sample under the same constraint, and it is reproducible across runs.

Per-trade statistics (expectancy, win rate, R) are cap-insensitive; the equity
curve statistics (CAGR, Sharpe, max drawdown) are not.  The report shows both
and says which is which.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent      # InsiderSwing/backtest
_PKG = _HERE.parent                          # InsiderSwing
_ROOT = _PKG.parent                          # project root
for _p in (str(_ROOT), str(_PKG), str(_PKG / "sources"), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg          # noqa: E402
import prices as price_mod    # noqa: E402
import risk                   # noqa: E402
import scoring                # noqa: E402
import technical              # noqa: E402
import universe as univ       # noqa: E402

logger = logging.getLogger(__name__)

ARM_INSIDER = "insider_only"
ARM_TECH = "tech_only"
ARM_COMBINED = "combined"
ALL_ARMS = (ARM_INSIDER, ARM_TECH, ARM_COMBINED)

# Price history is pulled this far before the backtest start so the 252-day and
# 200-day windows are warm on day one rather than NaN.
WARMUP_DAYS = 420


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _stable_rank(ticker: str, when: pd.Timestamp) -> float:
    """Deterministic, reproducible pseudo-random tiebreak in [0, 1)."""
    h = hashlib.md5(f"{ticker}|{when.date()}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _membership_index(members: pd.DataFrame) -> dict[str, list[tuple[str, Optional[str]]]]:
    """ticker → [(added_date, removed_date), ...] for O(1)-ish membership tests."""
    idx: dict[str, list[tuple[str, Optional[str]]]] = {}
    if members is None or members.empty:
        return idx
    for row in members.itertuples(index=False):
        add = str(row.added_date)[:10] if isinstance(row.added_date, str) else "1900-01-01"
        rem = str(row.removed_date)[:10] if isinstance(row.removed_date, str) else None
        idx.setdefault(str(row.ticker).upper(), []).append((add, rem))
    return idx


def _is_member(idx: dict, ticker: str, d: str) -> bool:
    spans = idx.get(ticker)
    if not spans:
        return False
    return any(add <= d and (rem is None or rem > d) for add, rem in spans)


@dataclass
class _Candidate:
    """A scheduled entry attempt on a specific session."""
    entry_session: pd.Timestamp
    ticker: str
    priority: float                 # lower = filled first
    signal_date: Optional[str] = None
    score: Optional[float] = None
    cluster_count: Optional[int] = None
    role_bucket: Optional[str] = None
    earnings_flag: bool = False
    trigger_type: Optional[str] = None
    signal_id: Optional[int] = None


@dataclass
class ArmResult:
    arm: str
    trades: pd.DataFrame
    equity: pd.DataFrame
    candidates: int = 0
    filled: int = 0
    skipped_cap: int = 0
    skipped_already_open: int = 0
    skipped_not_member: int = 0
    skipped_sizing: int = 0
    skipped_no_price: int = 0

    def diagnostics(self) -> dict:
        return {
            "candidates": self.candidates,
            "filled": self.filled,
            "skipped_position_cap": self.skipped_cap,
            "skipped_already_open": self.skipped_already_open,
            "skipped_not_index_member": self.skipped_not_member,
            "skipped_sizing_or_liquidity": self.skipped_sizing,
            "skipped_no_price_data": self.skipped_no_price,
        }


# ──────────────────────────────────────────────────────────────────────────────
#  Candidate generation
# ──────────────────────────────────────────────────────────────────────────────

def _insider_candidates(
    signals: pd.DataFrame,
    price_data: dict[str, pd.DataFrame],
    conf: cfg.InsiderConfig,
    require_trigger: bool,
) -> tuple[list[_Candidate], list[dict]]:
    """
    Turn qualifying signals into scheduled entries.

    ``require_trigger=False`` → the ``insider_only`` arm: entry is scheduled
    ``signal_lag_days`` sessions after the filing, unconditionally.

    ``require_trigger=True``  → the ``combined`` arm: we scan the confirmation
    window for the first technical trigger.  Signals whose window elapses are
    returned in ``outcomes`` with status 'expired'.  They are recorded, not
    discarded: the expired set is precisely what quantifies the cost of
    requiring confirmation.
    """
    cands: list[_Candidate] = []
    outcomes: list[dict] = []

    for row in signals.itertuples(index=False):
        ticker = str(row.ticker).upper()
        sig_date = datetime.strptime(str(row.as_of_date)[:10], "%Y-%m-%d").date()

        base = {
            "ticker": ticker,
            "signal_date": sig_date.isoformat(),
            "score": float(row.score),
            "cluster_count": int(getattr(row, "cluster_count", 0) or 0),
            "role_bucket_top": str(getattr(row, "role_bucket_top", "") or ""),
            "sell_pressure_flag": bool(getattr(row, "sell_pressure_flag", False)),
            "earnings_proximity_flag": bool(getattr(row, "earnings_proximity_flag", False)),
        }

        if str(getattr(row, "status", "pending")) == "blocked":
            outcomes.append({**base, "status": "blocked",
                             "block_reason": getattr(row, "block_reason", None)})
            continue

        pdf = price_data.get(ticker)
        if pdf is None or pdf.empty:
            outcomes.append({**base, "status": "blocked", "block_reason": "no price data"})
            continue

        # Earliest actionable session: the filing date plus the availability lag.
        actionable = price_mod.shift_sessions(pdf, sig_date, conf.signal_lag_days)
        if actionable is None:
            outcomes.append({**base, "status": "blocked",
                             "block_reason": "price series ends before signal is actionable"})
            continue

        if not require_trigger:
            entry_session = price_mod.shift_sessions(pdf, actionable.date(), 0)
            if entry_session is None:
                outcomes.append({**base, "status": "blocked", "block_reason": "no session to enter"})
                continue
            cands.append(_Candidate(
                entry_session=entry_session, ticker=ticker,
                priority=-float(row.score),
                signal_date=sig_date.isoformat(), score=float(row.score),
                cluster_count=base["cluster_count"], role_bucket=base["role_bucket_top"],
                earnings_flag=base["earnings_proximity_flag"], trigger_type=None,
            ))
            outcomes.append({**base, "status": "confirmed", "trigger_date": entry_session.date().isoformat(),
                             "trigger_type": None, "trigger_price": None})
            continue

        res = technical.find_trigger(
            pdf, from_date=actionable.date(),
            window_sessions=conf.confirmation_window_days, config=conf,
        )
        if not res.fired:
            expiry = price_mod.shift_sessions(pdf, actionable.date(),
                                              conf.confirmation_window_days - 1)
            outcomes.append({**base, "status": "expired",
                             "expiry_date": expiry.date().isoformat() if expiry is not None else None,
                             "block_reason": res.reason})
            continue

        # Fill on the session AFTER the trigger bar closes.
        entry_session = price_mod.shift_sessions(pdf, res.trigger_date.date(), 1)
        if entry_session is None:
            outcomes.append({**base, "status": "expired",
                             "block_reason": "trigger fired on the final available bar"})
            continue

        cands.append(_Candidate(
            entry_session=entry_session, ticker=ticker,
            priority=-float(row.score),
            signal_date=sig_date.isoformat(), score=float(row.score),
            cluster_count=base["cluster_count"], role_bucket=base["role_bucket_top"],
            earnings_flag=base["earnings_proximity_flag"], trigger_type=res.trigger_type,
        ))
        outcomes.append({**base, "status": "confirmed",
                         "trigger_date": res.trigger_date.date().isoformat(),
                         "trigger_type": res.trigger_type,
                         "trigger_price": res.trigger_price})

    return cands, outcomes


def _tech_candidates(
    price_data: dict[str, pd.DataFrame],
    conf: cfg.InsiderConfig,
    start: date,
    end: date,
) -> list[_Candidate]:
    """
    Every technical trigger in the universe, with no insider filter — the
    control arm.  Priority is a stable hash so the position cap samples the
    trigger population without a liquidity or alphabetical bias.
    """
    cands: list[_Candidate] = []
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)

    for ticker, pdf in price_data.items():
        if pdf is None or pdf.empty:
            continue
        try:
            fires = technical.trigger_series(pdf, conf)
        except Exception as exc:
            logger.debug("trigger_series failed for %s: %s", ticker, exc)
            continue
        if fires.empty:
            continue

        hit_dates = fires.index[fires["fired"].fillna(False).to_numpy()]
        idx = pd.DatetimeIndex(pdf.index)
        for ts in hit_dates:
            if ts < lo or ts > hi:
                continue
            pos = int(idx.searchsorted(ts, side="left")) + 1
            if pos >= len(idx):
                continue
            entry_session = idx[pos]
            cands.append(_Candidate(
                entry_session=entry_session, ticker=ticker,
                priority=_stable_rank(ticker, entry_session),
                signal_date=ts.date().isoformat(), score=None,
                trigger_type=str(fires.loc[ts, "trigger_type"] or ""),
            ))
    return cands


# ──────────────────────────────────────────────────────────────────────────────
#  Simulation
# ──────────────────────────────────────────────────────────────────────────────

def _simulate_arm(
    arm: str,
    candidates: list[_Candidate],
    price_data: dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
    membership: dict,
    conf: cfg.InsiderConfig,
    mcap_lookup: Optional[dict[str, str]] = None,
) -> ArmResult:
    """
    Walk the calendar once: exits first, then new entries.

    Exits are processed before entries on the same session so capital and a
    position slot freed by a stop-out are available the same day — the ordering
    a live system would actually have.
    """
    by_date: dict[pd.Timestamp, list[_Candidate]] = {}
    for c in candidates:
        by_date.setdefault(c.entry_session, []).append(c)

    book = risk.PositionBook(conf.max_concurrent_positions)
    open_meta: dict[str, dict] = {}
    trades: list[dict] = []
    equity_rows: list[dict] = []

    res = ArmResult(arm=arm, trades=pd.DataFrame(), equity=pd.DataFrame(),
                    candidates=len(candidates))

    realized = 0.0
    mcaps = mcap_lookup or {}

    def _finish(ticker: str, ev: risk.ExitEvent) -> None:
        nonlocal realized
        pos = book.remove(ticker)
        meta = open_meta.pop(ticker, {})
        if pos is None:
            return
        out = pos.realise(float(ev.exit_ref_price))
        realized += out["net_pnl"]
        trades.append({
            **meta,
            "exit_date": ev.exit_date.date().isoformat(),
            "exit_price": out["exit_price"],
            "exit_ref_price": out["exit_ref_price"],
            "exit_reason": ev.exit_reason,
            "gross_pnl": out["gross_pnl"],
            "slippage_cost": out["slippage_cost"],
            "net_pnl": out["net_pnl"],
            "return_pct": out["return_pct"],
            "r_multiple": out["r_multiple"],
            "holding_days": out["holding_days"],
            "status": "closed",
        })

    for session in calendar:
        d_str = session.date().isoformat()

        # ── 1. advance open positions ─────────────────────────────────────────
        for ticker in list(book.open.keys()):
            pdf = price_data.get(ticker)
            if pdf is None:
                continue
            if session not in pdf.index:
                # Series ended while we were holding → delisting/acquisition.
                last_idx = pdf.index[pdf.index <= session]
                if len(last_idx) and session > pdf.index[-1]:
                    pos = book.open[ticker]
                    _finish(ticker, pos.close_out(
                        float(pdf["Close"].iloc[-1]), pdf.index[-1], "delisted"))
                continue
            pos = book.open[ticker]
            ev = pos.update(pdf.loc[session], session)
            if ev.exited:
                _finish(ticker, ev)

        # ── 2. new entries ────────────────────────────────────────────────────
        for cand in sorted(by_date.get(session, []), key=lambda c: c.priority):
            ticker = cand.ticker
            pdf = price_data.get(ticker)
            if pdf is None or session not in pdf.index:
                res.skipped_no_price += 1
                continue
            if book.holds(ticker):
                res.skipped_already_open += 1
                continue
            if membership and not _is_member(membership, ticker, d_str):
                res.skipped_not_member += 1
                continue
            if not book.has_room():
                res.skipped_cap += 1
                book.skipped_for_cap += 1
                continue

            bar = pdf.loc[session]
            # Fill at this session's OPEN: the trigger/lag bar has already closed.
            ref = float(bar["Open"]) if not pd.isna(bar.get("Open")) else float(bar["Close"])
            prev_rows = pdf.loc[pdf.index < session]
            if prev_rows.empty:
                res.skipped_no_price += 1
                continue
            plan = risk.build_entry(prev_rows.iloc[-1], ref, conf)
            if not plan.ok:
                res.skipped_sizing += 1
                continue

            pos = risk.OpenPosition(plan, session, conf)
            if not book.add(ticker, pos):
                res.skipped_cap += 1
                continue

            res.filled += 1
            cluster = cand.cluster_count
            open_meta[ticker] = {
                "arm": arm,
                "ticker": ticker,
                "signal_date": cand.signal_date,
                "entry_date": d_str,
                "entry_price": plan.entry_price,
                "entry_ref_price": plan.entry_ref_price,
                "qty": plan.qty,
                "notional": plan.notional,
                "initial_stop": plan.initial_stop,
                "stop_basis": plan.stop_basis,
                "target_price": plan.target_price,
                "initial_risk": plan.risk_per_share,
                "atr_at_entry": plan.atr,
                "participation_rate": plan.participation_rate,
                "cluster_bucket": ("n/a" if cluster is None else
                                   ("1" if cluster == 1 else "2" if cluster == 2 else "3+")),
                "role_bucket": cand.role_bucket or "n/a",
                "mcap_bucket": mcaps.get(ticker, "unknown"),
                "earnings_flag": bool(cand.earnings_flag),
                # float('nan') rather than None: the tech_only arm has no score,
                # and an all-None column lands as object dtype and warns on concat.
                "score_at_entry": float("nan") if cand.score is None else float(cand.score),
                "trigger_type": cand.trigger_type,
            }

        # ── 3. mark to market ─────────────────────────────────────────────────
        unrealized = 0.0
        for ticker, pos in book.open.items():
            pdf = price_data.get(ticker)
            if pdf is None or session not in pdf.index:
                continue
            close = float(pdf.loc[session, "Close"])
            unrealized += (close - pos.plan.entry_ref_price) * pos.plan.qty

        equity_rows.append({
            "date": d_str,
            "realized": realized,
            "unrealized": unrealized,
            "equity": conf.account_size + realized + unrealized,
            "open_positions": len(book.open),
        })

    # ── close out anything still open at the end of the run ───────────────────
    final = calendar[-1] if len(calendar) else None
    for ticker in list(book.open.keys()):
        pdf = price_data.get(ticker)
        pos = book.open[ticker]
        if pdf is None or pdf.empty:
            book.remove(ticker)
            open_meta.pop(ticker, None)
            continue
        usable = pdf.index[pdf.index <= final] if final is not None else pdf.index
        when = usable[-1] if len(usable) else pdf.index[-1]
        meta = open_meta.get(ticker, {})
        out = pos.realise(float(pdf.loc[when, "Close"]))
        realized += out["net_pnl"]
        trades.append({
            **meta,
            "exit_date": when.date().isoformat(),
            "exit_price": out["exit_price"],
            "exit_ref_price": out["exit_ref_price"],
            "exit_reason": "open_at_end",
            "gross_pnl": out["gross_pnl"],
            "slippage_cost": out["slippage_cost"],
            "net_pnl": out["net_pnl"],
            "return_pct": out["return_pct"],
            "r_multiple": out["r_multiple"],
            "holding_days": out["holding_days"],
            "status": "open",
        })
        book.remove(ticker)
        open_meta.pop(ticker, None)

    res.trades = pd.DataFrame(trades)
    res.equity = pd.DataFrame(equity_rows)
    logger.info("Arm %-13s → %d trades  %s", arm, len(res.trades), res.diagnostics())
    return res


# ──────────────────────────────────────────────────────────────────────────────
#  Orchestration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    config: cfg.InsiderConfig
    label: str
    start: date
    end: date
    arms: dict[str, ArmResult] = field(default_factory=dict)
    scores: pd.DataFrame = field(default_factory=pd.DataFrame)
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    signal_outcomes: pd.DataFrame = field(default_factory=pd.DataFrame)
    coverage: dict = field(default_factory=dict)
    benchmark: pd.DataFrame = field(default_factory=pd.DataFrame)
    notes: list[str] = field(default_factory=list)

    def all_trades(self) -> pd.DataFrame:
        frames = [r.trades for r in self.arms.values() if not r.trades.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def run_backtest(
    config: Optional[cfg.InsiderConfig] = None,
    label: str = "insider_swing",
    start: Optional[date] = None,
    end: Optional[date] = None,
    arms: Iterable[str] = ALL_ARMS,
    tickers: Optional[Iterable[str]] = None,
    price_data: Optional[dict[str, pd.DataFrame]] = None,
    scores: Optional[pd.DataFrame] = None,
    use_earnings: bool = True,
) -> BacktestResult:
    """
    Run the full three-arm backtest.

    ``price_data`` and ``scores`` can be passed in to avoid re-downloading and
    re-scoring across a parameter sweep — the sweep varies only the parameters
    that change signal selection and trade management, so the price layer is
    shared.  ``scores`` is re-derived when the sweep changes the cluster window,
    since that alters the score itself.
    """
    conf = config or cfg.DEFAULT_CONFIG
    cfg.ensure_dirs()

    start = start or datetime.strptime(conf.backtest_start, "%Y-%m-%d").date()
    end = end or (datetime.strptime(conf.backtest_end, "%Y-%m-%d").date()
                  if conf.backtest_end else datetime.now(timezone.utc).date())

    result = BacktestResult(config=conf, label=label, start=start, end=end)

    # ── universe ──────────────────────────────────────────────────────────────
    members = univ.full_universe(start, end)
    if tickers is not None:
        want = {str(t).upper() for t in tickers}
        members = members[members["ticker"].isin(want)]
    universe_tickers = sorted(set(members["ticker"])) if not members.empty else sorted(
        {str(t).upper() for t in (tickers or [])}
    )
    if not universe_tickers:
        raise RuntimeError(
            "Universe is empty. Run the S&P 500 membership checkpoint first "
            "(python SP500/run_sp500_backtest.py --checkpoint membership), or "
            "supply an explicit ticker list / data/cache/insider_extra_universe.csv."
        )
    membership_idx = _membership_index(members)

    if not members.empty and (~members["point_in_time"].astype(bool)).any():
        n_extra = int((~members["point_in_time"].astype(bool)).sum())
        result.notes.append(
            f"{n_extra} tickers come from the hand-screened extra universe CSV, which has "
            "NO point-in-time membership history — those names carry survivorship bias."
        )

    # ── prices ────────────────────────────────────────────────────────────────
    if price_data is None:
        cov = price_mod.fetch_prices(
            universe_tickers,
            start=(start - timedelta(days=WARMUP_DAYS)).isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            config=conf,
        )
        price_data = cov.data
        result.coverage = cov.summary()
    else:
        result.coverage = {"with_price": len(price_data), "missing": 0, "coverage_pct": 100.0,
                           "missing_sample": [], "note": "price data supplied by caller"}

    if result.coverage.get("coverage_pct", 100.0) < 95.0:
        result.notes.append(
            f"Price data unavailable for {result.coverage.get('missing')} of "
            f"{result.coverage.get('with_price', 0) + result.coverage.get('missing', 0)} "
            "universe tickers (typically delisted names). Their trades cannot be "
            "simulated, so a residual survivorship bias remains in the price layer "
            "even though the universe itself is point-in-time correct."
        )

    if not price_data:
        raise RuntimeError("No price data could be loaded for any universe ticker.")

    # ── calendar ──────────────────────────────────────────────────────────────
    all_idx = pd.DatetimeIndex(sorted({ts for df in price_data.values() for ts in df.index}))
    calendar = all_idx[(all_idx >= pd.Timestamp(start)) & (all_idx <= pd.Timestamp(end))]
    if len(calendar) == 0:
        raise RuntimeError(f"No trading sessions between {start} and {end}.")

    # ── market-cap buckets (reporting only) ───────────────────────────────────
    mcap_lookup: dict[str, str] = {}

    # ── scores + signals ──────────────────────────────────────────────────────
    arms = tuple(arms)
    needs_scores = ARM_INSIDER in arms or ARM_COMBINED in arms
    if needs_scores:
        if scores is None:
            scores = scoring.compute_scores(
                start, end, config=conf, tickers=universe_tickers, use_earnings=use_earnings
            )
        result.scores = scores if scores is not None else pd.DataFrame()
        signals = scoring.qualifying_signals(result.scores, conf)
        result.signals = signals

        if result.scores.empty:
            result.notes.append(
                "No conviction scores were produced — the insider corpus is empty for this "
                "window. Run the ingest step before the backtest."
            )
    else:
        signals = pd.DataFrame()

    # ── run arms ──────────────────────────────────────────────────────────────
    outcomes_frames: list[pd.DataFrame] = []

    if ARM_INSIDER in arms:
        cands, outcomes = _insider_candidates(signals, price_data, conf, require_trigger=False) \
            if not signals.empty else ([], [])
        result.arms[ARM_INSIDER] = _simulate_arm(
            ARM_INSIDER, cands, price_data, calendar, membership_idx, conf, mcap_lookup
        )
        if outcomes:
            df = pd.DataFrame(outcomes); df["arm"] = ARM_INSIDER
            outcomes_frames.append(df)

    if ARM_COMBINED in arms:
        cands, outcomes = _insider_candidates(signals, price_data, conf, require_trigger=True) \
            if not signals.empty else ([], [])
        result.arms[ARM_COMBINED] = _simulate_arm(
            ARM_COMBINED, cands, price_data, calendar, membership_idx, conf, mcap_lookup
        )
        if outcomes:
            df = pd.DataFrame(outcomes); df["arm"] = ARM_COMBINED
            outcomes_frames.append(df)

    if ARM_TECH in arms:
        cands = _tech_candidates(price_data, conf, start, end)
        result.arms[ARM_TECH] = _simulate_arm(
            ARM_TECH, cands, price_data, calendar, membership_idx, conf, mcap_lookup
        )

    if outcomes_frames:
        result.signal_outcomes = pd.concat(outcomes_frames, ignore_index=True)

    # ── benchmark ─────────────────────────────────────────────────────────────
    result.benchmark = _load_benchmark(conf, start, end)

    return result


def _load_benchmark(conf: cfg.InsiderConfig, start: date, end: date) -> pd.DataFrame:
    """Buy-and-hold benchmark series, normalised to the same starting equity."""
    try:
        cov = price_mod.fetch_prices(
            [conf.benchmark_symbol],
            start=(start - timedelta(days=10)).isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            config=conf,
        )
        df = cov.data.get(conf.benchmark_symbol.upper())
        if df is None or df.empty:
            return pd.DataFrame()
        sub = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end)), ["Close"]]
        if sub.empty:
            return pd.DataFrame()
        out = sub.reset_index()
        out.columns = ["date", "close"]
        out["date"] = out["date"].dt.date.astype(str)
        out["equity"] = conf.account_size * out["close"] / out["close"].iloc[0]
        return out
    except Exception as exc:
        logger.warning("Benchmark %s unavailable: %s", conf.benchmark_symbol, exc)
        return pd.DataFrame()


# ──────────────────────────────────────────────────────────────────────────────
#  Persistence
# ──────────────────────────────────────────────────────────────────────────────

def save_run(result: BacktestResult, metrics: Optional[dict] = None,
             report_path: Optional[str] = None) -> int:
    """Persist the run header, its trades, and its signal outcomes.  Returns run_id."""
    from db import get_engine, session_scope
    from models import InsiderBacktestRun, InsiderSignal

    conf = result.config
    now = datetime.now(timezone.utc).isoformat()

    with session_scope() as sess:
        run = InsiderBacktestRun(
            label=result.label,
            param_key=conf.param_key(),
            params_json=conf.to_json(),
            start_date=result.start.isoformat(),
            end_date=result.end.isoformat(),
            started_at=now,
            finished_at=now,
            status="ok",
            universe_size=int(result.coverage.get("with_price", 0) + result.coverage.get("missing", 0)),
            universe_with_price=int(result.coverage.get("with_price", 0)),
            price_coverage_pct=float(result.coverage.get("coverage_pct", 0.0)),
            scores_computed=int(len(result.scores)),
            signals_generated=int(len(result.signals)),
            signals_expired=int((result.signal_outcomes["status"] == "expired").sum())
                            if not result.signal_outcomes.empty else 0,
            signals_blocked=int((result.signal_outcomes["status"] == "blocked").sum())
                            if not result.signal_outcomes.empty else 0,
            metrics_json=json.dumps(metrics or {}, default=str),
            report_path=report_path,
            notes="\n".join(result.notes) if result.notes else None,
        )
        sess.add(run)
        sess.flush()
        run_id = run.id

    engine = get_engine()

    trades = result.all_trades()
    if not trades.empty:
        out = trades.copy()
        out["run_id"] = run_id
        out["param_key"] = conf.param_key()
        out["strategy_version"] = cfg.STRATEGY_VERSION
        out["created_at"] = now
        keep = {c.name for c in __import__("models").InsiderTrade.__table__.columns}
        out = out[[c for c in out.columns if c in keep]]
        with engine.begin() as conn:
            out.to_sql("ins_trades", conn, if_exists="append", index=False)

    if not result.signal_outcomes.empty:
        with session_scope() as sess:
            for row in result.signal_outcomes.itertuples(index=False):
                sess.add(InsiderSignal(
                    ticker=row.ticker,
                    signal_date=row.signal_date,
                    param_key=conf.param_key(),
                    run_id=run_id,
                    score=float(row.score),
                    cluster_count=int(getattr(row, "cluster_count", 0) or 0),
                    role_bucket_top=getattr(row, "role_bucket_top", None),
                    sell_pressure_flag=bool(getattr(row, "sell_pressure_flag", False)),
                    earnings_proximity_flag=bool(getattr(row, "earnings_proximity_flag", False)),
                    status=str(row.status),
                    block_reason=getattr(row, "block_reason", None),
                    expiry_date=getattr(row, "expiry_date", None),
                    trigger_date=getattr(row, "trigger_date", None),
                    trigger_type=getattr(row, "trigger_type", None),
                    trigger_price=getattr(row, "trigger_price", None),
                    source="backtest",
                ))

    logger.info("Saved backtest run #%d (%s)", run_id, result.label)
    return run_id
