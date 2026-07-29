"""
InsiderSwing — walk-forward validation and parameter stability sweep.

WALK-FORWARD
------------
A single blended equity curve over the whole history tells you nothing about
whether the result would have held up out of sample.  The window is split into
at least ``config.walk_forward_windows`` contiguous periods and stats are
reported PER WINDOW as well as blended.

Note carefully what this is and isn't: nothing in this system is *fitted*, so
these are out-of-sample in the sense of "different market regimes", not in the
sense of "trained on window 1, tested on window 2".  The parameters come from
the literature and from the spec, not from an optimiser.  That distinction is
stated in the report rather than letting "walk-forward" imply more rigour than
is actually present.

What the per-window table is genuinely for: if the edge lives entirely in one
window (say, the 2020–21 small-cap melt-up) and is absent or negative in the
others, the blended number is an artefact of one regime.  That is the single
most common way a factor backtest misleads, and it is visible only per window.

PARAMETER STABILITY
-------------------
Sweeps the three parameters that actually change signal selection:

    conviction_threshold      (score gate)
    cluster_window_days       (how far back a cluster can span)
    confirmation_window_days  (how long the technical trigger has to fire)

The output is a surface, and the question asked of it is NOT "which cell is
best". It is: **is performance smooth across neighbouring cells, or is there one
isolated spike?** A smooth plateau means the effect is robust to the exact
setting.  An isolated peak means the setting was fitted to noise, and
``stability_verdict`` says so in those words.

Cost note: each sweep cell is a full simulation.  Price data and (where the
cluster window is unchanged) the score matrix are computed once and reused, so
a 3×3×3 sweep costs far less than 27 independent runs.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_ROOT = _PKG.parent
for _p in (str(_ROOT), str(_PKG), str(_PKG / "sources"), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg      # noqa: E402
import engine             # noqa: E402
import metrics            # noqa: E402
import scoring            # noqa: E402

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Walk-forward
# ──────────────────────────────────────────────────────────────────────────────

def split_windows(start: date, end: date, n: int) -> list[tuple[str, date, date]]:
    """Contiguous, equal-length calendar windows labelled W1..Wn."""
    n = max(int(n), 1)
    total_days = (end - start).days
    if total_days <= 0:
        return [("W1", start, end)]
    step = total_days // n
    out: list[tuple[str, date, date]] = []
    cursor = start
    for i in range(n):
        w_end = end if i == n - 1 else cursor + timedelta(days=step)
        out.append((f"W{i + 1}", cursor, w_end))
        cursor = w_end + timedelta(days=1)
    return out


def walk_forward(
    result: "engine.BacktestResult",
    config: Optional[cfg.InsiderConfig] = None,
) -> dict:
    """
    Split an already-completed run's trades by ENTRY date into windows and
    report per-window stats.

    Slicing an existing run rather than re-simulating each window is deliberate:
    re-simulating would reset the position book at every boundary and create
    artificial capacity at the start of each window, which flatters the later
    windows.  Slicing keeps one continuous portfolio and just reports it in
    pieces.
    """
    conf = config or result.config
    windows = split_windows(result.start, result.end, conf.walk_forward_windows)

    out: dict = {
        "windows": [{"label": w, "start": s.isoformat(), "end": e.isoformat()}
                    for w, s, e in windows],
        "by_arm": {},
        "caveat": (
            "No parameters are fitted on any window — they come from the literature and "
            "the spec, not an optimiser. These windows test regime robustness, not "
            "in-sample/out-of-sample generalisation."
        ),
    }

    for arm_name, arm in result.arms.items():
        rows = []
        trades = arm.trades
        equity = arm.equity

        for label, w_start, w_end in windows:
            if trades.empty:
                rows.append({"window": label, "trades": 0, "thin_sample": True})
                continue

            entry = pd.to_datetime(trades["entry_date"], errors="coerce")
            mask = (entry >= pd.Timestamp(w_start)) & (entry <= pd.Timestamp(w_end))
            sub = trades[mask]

            st = metrics.trade_stats(sub, conf)
            row = {
                "window": label,
                "start": w_start.isoformat(),
                "end": w_end.isoformat(),
                "trades": st["trades"],
                "win_rate_pct": st.get("win_rate_pct"),
                "expectancy_r": st.get("expectancy_r"),
                "profit_factor": st.get("profit_factor"),
                "total_net_pnl": st.get("total_net_pnl"),
                "avg_holding_days": st.get("avg_holding_days"),
                "thin_sample": st.get("thin_sample"),
            }

            if not equity.empty:
                eq_dates = pd.to_datetime(equity["date"], errors="coerce")
                eq_sub = equity[(eq_dates >= pd.Timestamp(w_start)) & (eq_dates <= pd.Timestamp(w_end))]
                if len(eq_sub) > 2:
                    e_st = metrics.equity_stats(eq_sub, conf)
                    row["sharpe"] = e_st.get("sharpe")
                    row["max_drawdown_pct"] = e_st.get("max_drawdown_pct")
            rows.append(row)

        df = pd.DataFrame(rows)
        out["by_arm"][arm_name] = {
            "per_window": df.to_dict(orient="records"),
            "verdict": _window_verdict(df, conf),
        }

    return out


def _window_verdict(df: pd.DataFrame, conf: cfg.InsiderConfig) -> str:
    """Plain-language read on whether the edge is regime-dependent."""
    if df.empty or "expectancy_r" not in df.columns:
        return "No trades to evaluate."

    vals = pd.to_numeric(df["expectancy_r"], errors="coerce").dropna()
    counts = pd.to_numeric(df["trades"], errors="coerce").fillna(0)

    if vals.empty:
        return "No window produced a measurable expectancy."
    if (counts < conf.thin_sample_threshold).all():
        return (f"Every window is below the {conf.thin_sample_threshold}-trade threshold — "
                "the per-window numbers are noise and should not be interpreted.")

    positive = int((vals > 0).sum())
    total = int(len(vals))
    if positive == total:
        return f"Expectancy is positive in all {total} windows — the edge is not confined to one regime."
    if positive == 0:
        return f"Expectancy is negative in all {total} windows — no evidence of an edge in any regime."

    # Is the blended result carried by a single window?
    pnl = pd.to_numeric(df["total_net_pnl"], errors="coerce").fillna(0.0)
    total_pnl = float(pnl.sum())
    if total_pnl > 0 and float(pnl.max()) / total_pnl > 0.80:
        best = df.loc[pnl.idxmax(), "window"]
        return (f"Positive in {positive}/{total} windows, but over 80% of total P&L comes from "
                f"{best} alone. The blended result is a single-regime artefact, not a persistent edge.")

    return (f"Expectancy is positive in {positive}/{total} windows — mixed. The edge is "
            "regime-dependent rather than consistent.")


# ──────────────────────────────────────────────────────────────────────────────
#  Parameter stability sweep
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLDS = (50.0, 55.0, 60.0, 65.0, 70.0)
DEFAULT_CLUSTER_WINDOWS = (30, 45, 60, 90)
DEFAULT_CONFIRMATION_WINDOWS = (5, 10, 15, 20)


@dataclass
class SweepResult:
    grid: pd.DataFrame
    stability: dict

    def best_cell(self) -> Optional[dict]:
        if self.grid.empty:
            return None
        vals = pd.to_numeric(self.grid["expectancy_r"], errors="coerce")
        if vals.dropna().empty:
            return None
        return self.grid.loc[vals.idxmax()].to_dict()


def parameter_sweep(
    base_config: Optional[cfg.InsiderConfig] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    cluster_windows: Sequence[int] = DEFAULT_CLUSTER_WINDOWS,
    confirmation_windows: Sequence[int] = DEFAULT_CONFIRMATION_WINDOWS,
    arm: str = engine.ARM_COMBINED,
    tickers: Optional[Iterable[str]] = None,
    use_earnings: bool = True,
) -> SweepResult:
    """
    Run the grid and judge whether performance is a plateau or an isolated spike.

    Only the ``combined`` arm is swept by default — the sweep exists to test the
    robustness of the live strategy's settings, and running all three arms per
    cell would triple the cost for no additional information about stability.
    """
    conf = base_config or cfg.DEFAULT_CONFIG
    start = start or datetime.strptime(conf.backtest_start, "%Y-%m-%d").date()
    if end is None:
        end = (datetime.strptime(conf.backtest_end, "%Y-%m-%d").date()
               if conf.backtest_end else datetime.now(timezone.utc).date())

    # ── shared price layer ────────────────────────────────────────────────────
    import prices as price_mod
    import universe as univ

    members = univ.full_universe(start, end)
    if tickers is not None:
        want = {str(t).upper() for t in tickers}
        members = members[members["ticker"].isin(want)]
    universe_tickers = sorted(set(members["ticker"])) if not members.empty else sorted(
        {str(t).upper() for t in (tickers or [])}
    )
    if not universe_tickers:
        raise RuntimeError("Universe is empty — cannot sweep.")

    cov = price_mod.fetch_prices(
        universe_tickers,
        start=(start - timedelta(days=engine.WARMUP_DAYS)).isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        config=conf,
    )
    price_data = cov.data
    logger.info("Sweep price layer ready: %s", cov.summary())

    # ── score matrices, one per distinct cluster window ───────────────────────
    score_cache: dict[int, pd.DataFrame] = {}
    for cw in sorted(set(cluster_windows)):
        logger.info("Scoring for cluster_window_days=%d ...", cw)
        score_cache[cw] = scoring.compute_scores(
            start, end, config=conf.with_overrides(cluster_window_days=cw),
            tickers=universe_tickers, use_earnings=use_earnings,
        )

    # ── the grid ──────────────────────────────────────────────────────────────
    rows: list[dict] = []
    combos = list(product(sorted(set(thresholds)), sorted(set(cluster_windows)),
                          sorted(set(confirmation_windows))))
    logger.info("Parameter sweep: %d cells", len(combos))

    for n, (th, cw, conf_win) in enumerate(combos, 1):
        cell_conf = conf.with_overrides(
            conviction_threshold=float(th),
            cluster_window_days=int(cw),
            confirmation_window_days=int(conf_win),
        )
        try:
            res = engine.run_backtest(
                config=cell_conf, label=f"sweep_{cell_conf.param_key()}",
                start=start, end=end, arms=(arm,),
                tickers=universe_tickers, price_data=price_data,
                scores=score_cache.get(int(cw)), use_earnings=use_earnings,
            )
            arm_res = res.arms[arm]
            t_st = metrics.trade_stats(arm_res.trades, cell_conf)
            e_st = metrics.equity_stats(arm_res.equity, cell_conf)
            rows.append({
                "conviction_threshold": th,
                "cluster_window_days": cw,
                "confirmation_window_days": conf_win,
                "trades": t_st["trades"],
                "win_rate_pct": t_st.get("win_rate_pct"),
                "expectancy_r": t_st.get("expectancy_r"),
                "profit_factor": t_st.get("profit_factor"),
                "total_net_pnl": t_st.get("total_net_pnl"),
                "cagr_pct": e_st.get("cagr_pct"),
                "sharpe": e_st.get("sharpe"),
                "max_drawdown_pct": e_st.get("max_drawdown_pct"),
                "thin_sample": t_st.get("thin_sample"),
            })
        except Exception as exc:      # noqa: BLE001
            logger.warning("Sweep cell (%s, %s, %s) failed: %s", th, cw, conf_win, exc)
            rows.append({
                "conviction_threshold": th, "cluster_window_days": cw,
                "confirmation_window_days": conf_win, "trades": 0,
                "expectancy_r": None, "error": str(exc), "thin_sample": True,
            })

        if n % 5 == 0 or n == len(combos):
            logger.info("Sweep progress: %d/%d cells", n, len(combos))

    grid = pd.DataFrame(rows)
    return SweepResult(grid=grid, stability=assess_stability(grid))


def assess_stability(grid: pd.DataFrame, metric: str = "expectancy_r") -> dict:
    """
    Plateau or spike?

    Method: take the best cell, then look at its immediate neighbours along each
    swept axis.  If the neighbours retain most of the peak's performance, the
    setting sits on a plateau and is robust.  If performance collapses one step
    away in every direction, the peak is fitted to noise and the verdict says
    so explicitly.
    """
    if grid is None or grid.empty or metric not in grid.columns:
        return {"available": False, "reason": "empty sweep grid"}

    vals = pd.to_numeric(grid[metric], errors="coerce")
    usable = grid[vals.notna()].copy()
    usable[metric] = vals[vals.notna()]
    if usable.empty:
        return {"available": False, "reason": f"no cell produced a {metric}"}

    axes = ["conviction_threshold", "cluster_window_days", "confirmation_window_days"]
    best_idx = usable[metric].idxmax()
    best = usable.loc[best_idx]
    best_val = float(best[metric])

    levels = {ax: sorted(usable[ax].unique()) for ax in axes}
    neighbours: list[dict] = []
    for ax in axes:
        vs = levels[ax]
        try:
            pos = vs.index(best[ax])
        except ValueError:
            continue
        for step in (-1, 1):
            j = pos + step
            if not (0 <= j < len(vs)):
                continue
            mask = pd.Series(True, index=usable.index)
            for other in axes:
                mask &= (usable[other] == (vs[j] if other == ax else best[other]))
            cell = usable[mask]
            if not cell.empty:
                neighbours.append({
                    "axis": ax, "value": vs[j],
                    metric: round(float(cell.iloc[0][metric]), 4),
                    "trades": int(cell.iloc[0].get("trades", 0) or 0),
                })

    positive_share = float((usable[metric] > 0).mean())
    median_val = float(usable[metric].median())

    if not neighbours:
        retention = None
        verdict = "Only one cell in the grid — stability cannot be assessed."
    else:
        n_vals = [n[metric] for n in neighbours]
        retention = (float(np.mean(n_vals)) / best_val) if best_val != 0 else None
        if best_val <= 0:
            verdict = ("No parameter combination produces a positive expectancy. "
                       "There is nothing to stabilise — the strategy does not work "
                       "at any tested setting.")
        elif retention is not None and retention >= 0.6 and positive_share >= 0.6:
            verdict = ("STABLE: neighbouring parameter values retain "
                       f"{retention:.0%} of the peak and {positive_share:.0%} of all cells are "
                       "positive. The result sits on a plateau and is robust to the exact setting.")
        elif retention is not None and retention < 0.3:
            verdict = ("OVERFIT RISK: performance collapses to "
                       f"{retention:.0%} of the peak one step away in the parameter grid. "
                       "This is an isolated spike, not a plateau — treat the peak cell as noise "
                       "and do not adopt its settings.")
        else:
            verdict = ("MARGINAL: neighbouring cells retain "
                       f"{retention:.0%} of the peak with {positive_share:.0%} of cells positive. "
                       "Weak evidence of a plateau; the setting is somewhat sensitive.")

    thin = usable.get("thin_sample")
    if thin is not None and bool(thin.astype(bool).all()):
        # The thin-sample warning overrides the shape verdict rather than sitting
        # beside it — a plateau made of 3-trade cells is still noise.
        verdict = ("Every cell in the sweep is a thin sample. The surface is noise and no "
                   "stability conclusion can be drawn. (Shape of the surface, for "
                   f"reference only: {verdict})")

    return {
        "available": True,
        "metric": metric,
        "best_cell": {k: (float(best[k]) if isinstance(best[k], (int, float, np.floating)) else best[k])
                      for k in axes + [metric, "trades"] if k in best.index},
        "neighbours": neighbours,
        "neighbour_retention": round(retention, 3) if retention is not None else None,
        "cells_positive_pct": round(100 * positive_share, 1),
        "median_metric": round(median_val, 4),
        "verdict": verdict,
    }
