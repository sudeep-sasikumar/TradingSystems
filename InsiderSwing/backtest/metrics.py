"""
InsiderSwing — performance metrics, segmentation, and statistical honesty checks.

Everything the spec asks for, plus the parts that stop a thin result from
looking like a real one.

STATISTICAL HONESTY
-------------------
Qualifying insider cluster events are rare.  A strategy can easily end up with
40 trades over a decade, and 40 trades cannot distinguish a 1.2 Sharpe from
zero.  So:

* Sharpe and CAGR are reported as bootstrap CONFIDENCE INTERVALS, not point
  estimates.  A point estimate on 40 trades is a number pretending to be a fact.
* The daily-return bootstrap is a MOVING-BLOCK bootstrap (20-day blocks).  An
  i.i.d. bootstrap would destroy the serial correlation in an equity curve and
  produce spuriously tight intervals.
* A two-sample permutation test compares the combined arm against the
  technical-only arm directly — that is the question that matters ("does the
  insider data add anything over the timing rule?"), and permutation makes no
  distributional assumption.
* ``thin_sample`` is set whenever a bucket has fewer than
  ``config.thin_sample_threshold`` trades, and the report prints the warning
  instead of the reader having to notice the small n.

If the numbers come out flat after costs, that is the finding.  Nothing here
searches for a better-looking parameter set.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_ROOT = _PKG.parent
for _p in (str(_ROOT), str(_PKG), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
_RNG = np.random.default_rng(20260728)   # fixed seed: a re-run must reproduce the CIs


# ──────────────────────────────────────────────────────────────────────────────
#  Trade-level statistics  (insensitive to the position cap)
# ──────────────────────────────────────────────────────────────────────────────

def trade_stats(trades: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> dict:
    conf = config or cfg.DEFAULT_CONFIG
    if trades is None or trades.empty:
        return {"trades": 0, "thin_sample": True}

    net = pd.to_numeric(trades["net_pnl"], errors="coerce").dropna()
    r = pd.to_numeric(trades.get("r_multiple"), errors="coerce").dropna()
    ret = pd.to_numeric(trades.get("return_pct"), errors="coerce").dropna()
    hold = pd.to_numeric(trades.get("holding_days"), errors="coerce").dropna()
    gross = pd.to_numeric(trades.get("gross_pnl"), errors="coerce").dropna()
    slip = pd.to_numeric(trades.get("slippage_cost"), errors="coerce").dropna()
    notional = pd.to_numeric(trades.get("notional"), errors="coerce").dropna()

    wins, losses = net[net > 0], net[net <= 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())

    # Slippage as a share of gross return: the number that tells you whether the
    # edge survives execution.  Denominated on |gross| so a near-zero gross
    # doesn't produce a meaningless ratio.
    abs_gross = float(gross.abs().sum())
    slip_share = (float(slip.sum()) / abs_gross * 100.0) if abs_gross > 0 else None

    return {
        "trades": int(len(trades)),
        "thin_sample": bool(len(trades) < conf.thin_sample_threshold),
        "win_rate_pct": round(100.0 * len(wins) / len(net), 2) if len(net) else None,
        "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "avg_win_loss_ratio": (
            round(float(wins.mean()) / abs(float(losses.mean())), 3)
            if len(wins) and len(losses) and losses.mean() != 0 else None
        ),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy_usd": round(float(net.mean()), 2) if len(net) else None,
        "expectancy_r": round(float(r.mean()), 3) if len(r) else None,
        "median_r": round(float(r.median()), 3) if len(r) else None,
        "avg_return_pct": round(100.0 * float(ret.mean()), 2) if len(ret) else None,
        "total_net_pnl": round(float(net.sum()), 2),
        "total_gross_pnl": round(float(gross.sum()), 2) if len(gross) else None,
        "total_costs": round(float(slip.sum()), 2) if len(slip) else None,
        "slippage_pct_of_gross": round(slip_share, 2) if slip_share is not None else None,
        "avg_holding_days": round(float(hold.mean()), 1) if len(hold) else None,
        "median_holding_days": round(float(hold.median()), 1) if len(hold) else None,
        "avg_notional": round(float(notional.mean()), 2) if len(notional) else None,
        "total_turnover": round(float(notional.sum()) * 2, 2) if len(notional) else None,
        "exit_reason_mix": (
            trades["exit_reason"].value_counts(normalize=True).round(3).to_dict()
            if "exit_reason" in trades.columns else {}
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Equity-curve statistics  (SENSITIVE to the position cap)
# ──────────────────────────────────────────────────────────────────────────────

def _daily_returns(equity: pd.DataFrame) -> pd.Series:
    if equity is None or equity.empty or "equity" not in equity.columns:
        return pd.Series(dtype=float)
    eq = pd.to_numeric(equity["equity"], errors="coerce").ffill().dropna()
    return eq.pct_change().dropna()


def max_drawdown(equity_series: pd.Series) -> tuple[float, Optional[str]]:
    """(max drawdown as a negative fraction, date of the trough)."""
    if equity_series is None or equity_series.empty:
        return 0.0, None
    running_max = equity_series.cummax()
    dd = equity_series / running_max - 1.0
    return float(dd.min()), (str(dd.idxmin()) if len(dd) else None)


def equity_stats(equity: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> dict:
    conf = config or cfg.DEFAULT_CONFIG
    if equity is None or equity.empty:
        return {"sessions": 0}

    eq = equity.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    eq = eq.sort_values("date").set_index("date")
    series = pd.to_numeric(eq["equity"], errors="coerce").ffill().dropna()
    if series.empty or len(series) < 2:
        return {"sessions": int(len(series))}

    rets = series.pct_change().dropna()
    years = max((series.index[-1] - series.index[0]).days / 365.25, 1e-9)
    total_return = float(series.iloc[-1] / series.iloc[0] - 1.0)
    cagr = (series.iloc[-1] / series.iloc[0]) ** (1.0 / years) - 1.0 if series.iloc[0] > 0 else None

    sd = float(rets.std(ddof=1))
    sharpe = (float(rets.mean()) / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else None

    downside = rets[rets < 0]
    dsd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = (float(rets.mean()) / dsd * np.sqrt(TRADING_DAYS)) if dsd > 0 else None

    mdd, mdd_date = max_drawdown(series)
    calmar = (cagr / abs(mdd)) if (cagr is not None and mdd < 0) else None

    return {
        "sessions": int(len(series)),
        "years": round(years, 2),
        "start_equity": round(float(series.iloc[0]), 2),
        "end_equity": round(float(series.iloc[-1]), 2),
        "total_return_pct": round(100.0 * total_return, 2),
        "cagr_pct": round(100.0 * cagr, 2) if cagr is not None else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "max_drawdown_pct": round(100.0 * mdd, 2),
        "max_drawdown_date": mdd_date,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "avg_open_positions": (
            round(float(pd.to_numeric(eq["open_positions"], errors="coerce").mean()), 2)
            if "open_positions" in eq.columns else None
        ),
        "pct_days_invested": (
            round(100.0 * float((pd.to_numeric(eq["open_positions"], errors="coerce") > 0).mean()), 1)
            if "open_positions" in eq.columns else None
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Bootstrap + permutation
# ──────────────────────────────────────────────────────────────────────────────

def moving_block_bootstrap(
    returns: Sequence[float],
    iterations: int = 2000,
    block: int = 20,
    rng: Optional[np.random.Generator] = None,
) -> np.ndarray:
    """
    Resample a return series in overlapping blocks.

    Blocks preserve the serial correlation that an equity curve has and an
    i.i.d. bootstrap destroys — using i.i.d. here would understate the width of
    the Sharpe interval, which is the exact error this section exists to avoid.
    """
    rng = rng or _RNG
    x = np.asarray([r for r in returns if np.isfinite(r)], dtype=float)
    n = len(x)
    if n < 2:
        return np.empty((0, 0))
    block = max(1, min(block, n))
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(iterations, n_blocks))
    out = np.empty((iterations, n), dtype=float)
    for i in range(iterations):
        sample = np.concatenate([x[s:s + block] for s in starts[i]])
        out[i] = sample[:n]
    return out


def bootstrap_equity_ci(
    equity: pd.DataFrame,
    config: Optional[cfg.InsiderConfig] = None,
    alpha: float = 0.05,
) -> dict:
    """Bootstrap CIs on annualised Sharpe and CAGR from the daily equity curve."""
    conf = config or cfg.DEFAULT_CONFIG
    rets = _daily_returns(equity)
    if len(rets) < 30:
        return {"available": False,
                "reason": f"only {len(rets)} daily observations — too few to bootstrap"}

    samples = moving_block_bootstrap(rets.values, conf.bootstrap_iterations)
    if samples.size == 0:
        return {"available": False, "reason": "bootstrap produced no samples"}

    mean = samples.mean(axis=1)
    sd = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpes = np.where(sd > 0, mean / sd * np.sqrt(TRADING_DAYS), np.nan)
    cagrs = (1.0 + mean) ** TRADING_DAYS - 1.0

    lo, hi = 100 * alpha / 2, 100 * (1 - alpha / 2)
    sharpes = sharpes[np.isfinite(sharpes)]
    cagrs = cagrs[np.isfinite(cagrs)]

    return {
        "available": True,
        "iterations": conf.bootstrap_iterations,
        "confidence": round(100 * (1 - alpha)),
        "sharpe_ci": [round(float(np.percentile(sharpes, lo)), 3),
                      round(float(np.percentile(sharpes, hi)), 3)] if len(sharpes) else None,
        "sharpe_median": round(float(np.median(sharpes)), 3) if len(sharpes) else None,
        "cagr_ci_pct": [round(100 * float(np.percentile(cagrs, lo)), 2),
                        round(100 * float(np.percentile(cagrs, hi)), 2)] if len(cagrs) else None,
        "sharpe_ci_includes_zero": (
            bool(np.percentile(sharpes, lo) <= 0 <= np.percentile(sharpes, hi))
            if len(sharpes) else None
        ),
    }


def bootstrap_trade_ci(
    trades: pd.DataFrame,
    config: Optional[cfg.InsiderConfig] = None,
    alpha: float = 0.05,
) -> dict:
    """Bootstrap CI on per-trade expectancy (in R) — trades are i.i.d. enough for this."""
    conf = config or cfg.DEFAULT_CONFIG
    if trades is None or trades.empty:
        return {"available": False, "reason": "no trades"}

    r = pd.to_numeric(trades.get("r_multiple"), errors="coerce").dropna().values
    if len(r) < 5:
        return {"available": False, "reason": f"only {len(r)} trades with an R-multiple"}

    idx = _RNG.integers(0, len(r), size=(conf.bootstrap_iterations, len(r)))
    means = r[idx].mean(axis=1)
    lo, hi = 100 * alpha / 2, 100 * (1 - alpha / 2)

    return {
        "available": True,
        "n_trades": int(len(r)),
        "expectancy_r": round(float(r.mean()), 3),
        "expectancy_r_ci": [round(float(np.percentile(means, lo)), 3),
                            round(float(np.percentile(means, hi)), 3)],
        "ci_includes_zero": bool(np.percentile(means, lo) <= 0 <= np.percentile(means, hi)),
        "thin_sample": bool(len(r) < conf.thin_sample_threshold),
    }


def permutation_diff_test(
    a: Sequence[float],
    b: Sequence[float],
    iterations: int = 5000,
) -> dict:
    """
    Two-sample permutation test on the difference in means.

    Used to ask the question the spec cares about directly: are the combined
    arm's per-trade returns actually different from the technical-only arm's, or
    is the gap consistent with random relabelling?  No distributional
    assumptions, which matters for fat-tailed trade returns.
    """
    x = np.asarray([v for v in a if np.isfinite(v)], dtype=float)
    y = np.asarray([v for v in b if np.isfinite(v)], dtype=float)
    if len(x) < 5 or len(y) < 5:
        return {"available": False,
                "reason": f"need >=5 observations per arm (have {len(x)} and {len(y)})"}

    observed = float(x.mean() - y.mean())
    pooled = np.concatenate([x, y])
    n_x = len(x)

    count = 0
    for _ in range(iterations):
        _RNG.shuffle(pooled)
        if abs(float(pooled[:n_x].mean() - pooled[n_x:].mean())) >= abs(observed):
            count += 1
    # +1 correction: an exact zero p-value is not a thing a finite test can produce.
    p = (count + 1) / (iterations + 1)

    return {
        "available": True,
        "n_a": n_x, "n_b": len(y),
        "mean_a": round(float(x.mean()), 4),
        "mean_b": round(float(y.mean()), 4),
        "observed_diff": round(observed, 4),
        "iterations": iterations,
        "p_value": round(p, 4),
        "significant_at_5pct": bool(p < 0.05),
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Segmentation
# ──────────────────────────────────────────────────────────────────────────────

_SEGMENTS = {
    "cluster_size": "cluster_bucket",
    "role": "role_bucket",
    "market_cap": "mcap_bucket",
    "earnings_flag": "earnings_flag",
    "trigger_type": "trigger_type",
    "exit_reason": "exit_reason",
}


def segment_table(
    trades: pd.DataFrame,
    by: str,
    config: Optional[cfg.InsiderConfig] = None,
) -> pd.DataFrame:
    """
    Per-bucket edge.  Buckets below the thin-sample threshold are KEPT and
    flagged rather than hidden — a 4-trade bucket with a 3.0 expectancy is not a
    finding, and deleting it would be just as misleading as reporting it plainly.
    """
    conf = config or cfg.DEFAULT_CONFIG
    col = _SEGMENTS.get(by, by)
    if trades is None or trades.empty or col not in trades.columns:
        return pd.DataFrame()

    rows = []
    for value, grp in trades.groupby(trades[col].fillna("unknown"), sort=False):
        st = trade_stats(grp, conf)
        rows.append({
            "bucket": str(value),
            "trades": st["trades"],
            "win_rate_pct": st["win_rate_pct"],
            "expectancy_r": st["expectancy_r"],
            "avg_return_pct": st["avg_return_pct"],
            "profit_factor": st["profit_factor"],
            "total_net_pnl": st["total_net_pnl"],
            "avg_holding_days": st["avg_holding_days"],
            "thin_sample": st["thin_sample"],
        })
    out = pd.DataFrame(rows)
    return out.sort_values("trades", ascending=False).reset_index(drop=True) if not out.empty else out


def all_segments(trades: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> dict[str, pd.DataFrame]:
    return {name: segment_table(trades, name, config) for name in _SEGMENTS}


# ──────────────────────────────────────────────────────────────────────────────
#  Benchmarks
# ──────────────────────────────────────────────────────────────────────────────

def benchmark_stats(benchmark: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> dict:
    """Buy-and-hold stats on the same equity basis as the strategy arms."""
    if benchmark is None or benchmark.empty:
        return {"available": False, "reason": "benchmark data unavailable"}
    eq = benchmark[["date", "equity"]].copy()
    eq["open_positions"] = 1
    st = equity_stats(eq, config)
    st["available"] = True
    return st


# ──────────────────────────────────────────────────────────────────────────────
#  Top-level summary
# ──────────────────────────────────────────────────────────────────────────────

def summarize(result, config: Optional[cfg.InsiderConfig] = None) -> dict:
    """
    Everything Section 8 of the spec asks for, in one dict, ready for both the
    saved report and the Streamlit view.
    """
    conf = config or getattr(result, "config", None) or cfg.DEFAULT_CONFIG

    out: dict = {
        "label": result.label,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "params": conf.to_dict(),
        "param_key": conf.param_key(),
        "price_coverage": result.coverage,
        "notes": list(result.notes),
        "arms": {},
        "segments": {},
        "comparisons": {},
        "statistical_checks": {},
    }

    for arm_name, arm in result.arms.items():
        t_st = trade_stats(arm.trades, conf)
        e_st = equity_stats(arm.equity, conf)
        out["arms"][arm_name] = {
            "trade_stats": t_st,
            "equity_stats": e_st,
            "diagnostics": arm.diagnostics(),
            "bootstrap_equity": bootstrap_equity_ci(arm.equity, conf),
            "bootstrap_trades": bootstrap_trade_ci(arm.trades, conf),
        }

    # Segmentation on the primary arm (combined when present).
    primary = "combined" if "combined" in result.arms else next(iter(result.arms), None)
    if primary:
        out["primary_arm"] = primary
        segs = all_segments(result.arms[primary].trades, conf)
        out["segments"] = {k: (v.to_dict(orient="records") if not v.empty else [])
                           for k, v in segs.items()}

    # Benchmark
    out["benchmark"] = benchmark_stats(result.benchmark, conf)

    # ── Arm comparisons: the whole point of running three arms ────────────────
    def _r(arm: str) -> np.ndarray:
        a = result.arms.get(arm)
        if a is None or a.trades.empty:
            return np.array([])
        return pd.to_numeric(a.trades.get("r_multiple"), errors="coerce").dropna().values

    r_comb, r_ins, r_tech = _r("combined"), _r("insider_only"), _r("tech_only")

    if len(r_comb) and len(r_tech):
        out["comparisons"]["combined_vs_tech_only"] = permutation_diff_test(r_comb, r_tech)
        out["comparisons"]["combined_vs_tech_only"]["question"] = (
            "Does the insider filter add anything over the technical trigger alone?"
        )
    if len(r_comb) and len(r_ins):
        out["comparisons"]["combined_vs_insider_only"] = permutation_diff_test(r_comb, r_ins)
        out["comparisons"]["combined_vs_insider_only"]["question"] = (
            "Does requiring technical confirmation add anything over the raw insider signal?"
        )

    # ── Signal-funnel accounting (how much the timing overlay discards) ───────
    if result.signal_outcomes is not None and not result.signal_outcomes.empty:
        so = result.signal_outcomes
        comb = so[so["arm"] == "combined"] if "arm" in so.columns else so
        total = len(comb)
        out["signal_funnel"] = {
            "qualifying_signals": int(total),
            "confirmed": int((comb["status"] == "confirmed").sum()),
            "expired_no_trigger": int((comb["status"] == "expired").sum()),
            "blocked": int((comb["status"] == "blocked").sum()),
            "confirmation_rate_pct": round(
                100.0 * (comb["status"] == "confirmed").sum() / total, 1) if total else None,
        }

    # ── Honesty flags ─────────────────────────────────────────────────────────
    checks: dict = {}
    for arm_name, arm_out in out["arms"].items():
        n = arm_out["trade_stats"].get("trades", 0)
        bt = arm_out["bootstrap_trades"]
        eb = arm_out["bootstrap_equity"]
        verdict = []
        if n < conf.thin_sample_threshold:
            verdict.append(
                f"THIN SAMPLE: {n} trades is below the {conf.thin_sample_threshold}-trade "
                "threshold. No reliable conclusion can be drawn from this arm."
            )
        if bt.get("available") and bt.get("ci_includes_zero"):
            verdict.append(
                "Expectancy confidence interval includes zero — the per-trade edge is "
                "not statistically distinguishable from no edge."
            )
        if eb.get("available") and eb.get("sharpe_ci_includes_zero"):
            verdict.append(
                "Sharpe confidence interval includes zero — the risk-adjusted return is "
                "not statistically distinguishable from zero."
            )
        if not verdict:
            verdict.append("Sample size and confidence intervals support a non-zero edge.")
        checks[arm_name] = verdict
    out["statistical_checks"] = checks

    return out
