"""
InsiderSwing — saved report generator (Markdown, optional HTML).

One report per backtest run, written to ``data/reports/insider/``.

EDITORIAL RULES BAKED INTO THIS FILE
------------------------------------
1. The verdict is computed from the numbers and printed FIRST, before the
   tables.  A reader who stops after ten lines should already know whether the
   strategy works.
2. A flat or negative result is written as plainly as a positive one.  The Nifty
   and S&P 500 breakout systems in this repo both came out marginal after costs;
   that conclusion was worth having, and this report is built to deliver the
   same kind of answer rather than to sell the strategy.
3. Limitations are a required section, not an appendix.  Filing lag, small
   samples, the earnings-calendar approximation, the price-coverage gap on
   delisted names, and the un-corrected extra universe all get named.
4. Nothing is rounded into looking better than it is, and thin-sample buckets
   are printed with their warning attached rather than dropped.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE), str(_HERE / "backtest")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fmt(v, suffix: str = "", dash: str = "—") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return dash
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    if isinstance(v, int):
        return f"{v:,}{suffix}"
    return f"{v}{suffix}"


def _table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    """Markdown table.  ``columns`` is [(key, header), ...]."""
    if not rows:
        return "_No data._\n"
    head = "| " + " | ".join(h for _, h in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(_fmt(r.get(k)) for k, _ in columns) + " |"
        for r in rows
    ]
    return "\n".join([head, sep, *body]) + "\n"


def _ci(pair, suffix: str = "") -> str:
    if not pair or not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return "—"
    return f"[{pair[0]:,.2f}{suffix}, {pair[1]:,.2f}{suffix}]"


# ──────────────────────────────────────────────────────────────────────────────
#  Verdict
# ──────────────────────────────────────────────────────────────────────────────

def build_verdict(summary: dict, conf: cfg.InsiderConfig) -> list[str]:
    """
    The bottom line, derived only from what the run measured.

    Order of questions, deliberately:
      1. Is there enough data to say anything at all?
      2. Does the primary arm beat zero after costs?
      3. Does it beat the technical-only control?  (does insider data help?)
      4. Does it beat the insider-only arm?        (does timing help?)
      5. Does it beat buy-and-hold?
    """
    lines: list[str] = []
    primary = summary.get("primary_arm") or "combined"
    arm = summary.get("arms", {}).get(primary, {})
    t_st = arm.get("trade_stats", {})
    e_st = arm.get("equity_stats", {})
    bt = arm.get("bootstrap_trades", {})
    eb = arm.get("bootstrap_equity", {})

    n = t_st.get("trades", 0)

    # 1. Sample size
    if n == 0:
        lines.append(
            "**No trades were generated.** Either the insider corpus is empty for this "
            "window, or no signal cleared the conviction threshold and found a technical "
            "confirmation. There is nothing to evaluate."
        )
        return lines

    if n < conf.thin_sample_threshold:
        lines.append(
            f"**Sample too thin to conclude anything.** The `{primary}` arm produced "
            f"{n} trades, below the {conf.thin_sample_threshold}-trade threshold. "
            "Any Sharpe or CAGR below is a description of this particular sample, not "
            "an estimate of future performance. Treat the whole report as exploratory."
        )

    # 2. Edge vs zero
    exp_r = t_st.get("expectancy_r")
    if bt.get("available") and bt.get("ci_includes_zero"):
        lines.append(
            f"**No statistically distinguishable edge.** Expectancy is "
            f"{_fmt(exp_r)}R per trade, but the {bt.get('confidence', 95)}% confidence "
            f"interval {_ci(bt.get('expectancy_r_ci'))} includes zero. After costs, this "
            "result is consistent with no edge at all."
        )
    elif exp_r is not None and exp_r <= 0:
        lines.append(
            f"**Negative expectancy after costs.** {_fmt(exp_r)}R per trade over {n} trades. "
            "The strategy loses money as specified. Reporting this rather than tuning "
            "parameters until it looks positive is the point of the exercise."
        )
    elif exp_r is not None:
        lines.append(
            f"**Positive expectancy after costs:** {_fmt(exp_r)}R per trade over {n} trades, "
            f"{bt.get('confidence', 95)}% CI {_ci(bt.get('expectancy_r_ci'))}."
        )

    if eb.get("available"):
        if eb.get("sharpe_ci_includes_zero"):
            lines.append(
                f"Risk-adjusted return is not distinguishable from zero: Sharpe "
                f"{_fmt(e_st.get('sharpe'))} with a {eb.get('confidence', 95)}% CI of "
                f"{_ci(eb.get('sharpe_ci'))}."
            )
        else:
            lines.append(
                f"Sharpe {_fmt(e_st.get('sharpe'))}, {eb.get('confidence', 95)}% CI "
                f"{_ci(eb.get('sharpe_ci'))}; CAGR {_fmt(e_st.get('cagr_pct'), '%')} "
                f"with CI {_ci(eb.get('cagr_ci_pct'), '%')}."
            )

    # 3. Does the insider data add anything?
    cmp_tech = summary.get("comparisons", {}).get("combined_vs_tech_only", {})
    if cmp_tech.get("available"):
        diff, p = cmp_tech.get("observed_diff"), cmp_tech.get("p_value")
        if cmp_tech.get("significant_at_5pct") and diff and diff > 0:
            lines.append(
                f"**The insider filter adds value.** Combined-arm trades average "
                f"{_fmt(diff)}R more than the technical-trigger-only control "
                f"(permutation p = {p}). The insider data is contributing, not decorating."
            )
        elif cmp_tech.get("significant_at_5pct") and diff and diff < 0:
            lines.append(
                f"**The insider filter HURTS.** Combined-arm trades average {_fmt(diff)}R "
                f"*worse* than the technical-only control (p = {p}). The technical system "
                "performs better without the insider screen."
            )
        else:
            lines.append(
                f"**The insider filter adds nothing measurable.** The gap versus the "
                f"technical-only control is {_fmt(diff)}R with permutation p = {p} — "
                "consistent with random relabelling. On this evidence the insider data "
                "is not earning its complexity."
            )
    else:
        lines.append(
            "The technical-only control arm could not be compared "
            f"({cmp_tech.get('reason', 'not run')}), so it is not possible to say whether "
            "the insider data adds anything over the timing rule alone."
        )

    # 4. Does the timing overlay help?
    cmp_ins = summary.get("comparisons", {}).get("combined_vs_insider_only", {})
    if cmp_ins.get("available"):
        diff, p = cmp_ins.get("observed_diff"), cmp_ins.get("p_value")
        if cmp_ins.get("significant_at_5pct") and diff and diff > 0:
            lines.append(
                f"Requiring technical confirmation improves per-trade outcomes by "
                f"{_fmt(diff)}R (p = {p}) versus taking every insider signal."
            )
        elif cmp_ins.get("significant_at_5pct") and diff and diff < 0:
            lines.append(
                f"Requiring technical confirmation makes things **worse** by {_fmt(diff)}R "
                f"(p = {p}). The timing overlay is filtering out the winners."
            )
        else:
            lines.append(
                f"Technical confirmation makes no measurable difference versus the raw "
                f"insider signal ({_fmt(diff)}R, p = {p}) — but note it also discards "
                "signals, so 'no difference' means the overlay is pure cost in trade count."
            )

    funnel = summary.get("signal_funnel", {})
    if funnel.get("qualifying_signals"):
        lines.append(
            f"Signal funnel: {funnel['qualifying_signals']} qualifying signals → "
            f"{funnel.get('confirmed', 0)} confirmed "
            f"({_fmt(funnel.get('confirmation_rate_pct'), '%')}), "
            f"{funnel.get('expired_no_trigger', 0)} expired without a trigger, "
            f"{funnel.get('blocked', 0)} blocked by the cluster-selling filter."
        )

    # 5. vs buy and hold
    bench = summary.get("benchmark", {})
    if bench.get("available"):
        b_cagr, s_cagr = bench.get("cagr_pct"), e_st.get("cagr_pct")
        if b_cagr is not None and s_cagr is not None:
            if s_cagr > b_cagr:
                lines.append(
                    f"Versus buy-and-hold on {conf.benchmark_symbol}: strategy "
                    f"{_fmt(s_cagr, '%')} CAGR vs benchmark {_fmt(b_cagr, '%')}, with a "
                    f"max drawdown of {_fmt(e_st.get('max_drawdown_pct'), '%')} vs "
                    f"{_fmt(bench.get('max_drawdown_pct'), '%')}."
                )
            else:
                lines.append(
                    f"**Underperforms buy-and-hold.** Strategy {_fmt(s_cagr, '%')} CAGR vs "
                    f"{conf.benchmark_symbol} at {_fmt(b_cagr, '%')}. Note the strategy is "
                    f"only invested {_fmt(e_st.get('pct_days_invested'), '%')} of sessions, "
                    "so the comparison is not risk-equivalent — but it is the comparison a "
                    "capital allocator would make."
                )

    cost_share = t_st.get("slippage_pct_of_gross")
    if cost_share is not None and cost_share > 30:
        lines.append(
            f"**Costs dominate.** Slippage and commission consume {_fmt(cost_share, '%')} "
            "of gross P&L. This strategy's viability is an execution question as much as a "
            "signal question."
        )

    return lines


# ──────────────────────────────────────────────────────────────────────────────
#  Report body
# ──────────────────────────────────────────────────────────────────────────────

def render_markdown(
    summary: dict,
    walk_forward: Optional[dict] = None,
    sweep: Optional[dict] = None,
    conf: Optional[cfg.InsiderConfig] = None,
) -> str:
    conf = conf or cfg.DEFAULT_CONFIG
    p = summary.get("params", {})
    out: list[str] = []

    out.append(f"# Insider-Trade Cluster Swing System — Backtest Report\n")
    out.append(f"**Run:** `{summary.get('label')}`  ")
    out.append(f"**Window:** {summary.get('start')} → {summary.get('end')}  ")
    out.append(f"**Parameters:** `{summary.get('param_key')}`  ")
    out.append(f"**Generated:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")

    out.append("> This system trades on **Form 4 filings** — legally required *public* "
               "disclosures of insider transactions, filed within 2 business days of the "
               "trade. It is the documented insider-purchase anomaly (Seyhun; Lakonishok & "
               "Lee), not trading on material non-public information. Every input is a "
               "public SEC document.\n")

    # ── verdict ───────────────────────────────────────────────────────────────
    out.append("## Verdict\n")
    for line in build_verdict(summary, conf):
        out.append(f"- {line}\n")
    out.append("")

    # ── arms ──────────────────────────────────────────────────────────────────
    out.append("## The three arms\n")
    out.append("| Arm | What it isolates |\n|---|---|\n"
               "| `insider_only` | The raw factor: enter after the filing lag, no price condition. |\n"
               "| `tech_only` | The timing rule's base rate, with no insider filter at all. |\n"
               "| `combined` | Insider signal **and** technical confirmation. The live strategy. |\n")

    arm_rows = []
    for name, a in summary.get("arms", {}).items():
        t, e = a.get("trade_stats", {}), a.get("equity_stats", {})
        arm_rows.append({
            "arm": name,
            "trades": t.get("trades"),
            "win_rate_pct": t.get("win_rate_pct"),
            "expectancy_r": t.get("expectancy_r"),
            "profit_factor": t.get("profit_factor"),
            "cagr_pct": e.get("cagr_pct"),
            "sharpe": e.get("sharpe"),
            "sortino": e.get("sortino"),
            "max_drawdown_pct": e.get("max_drawdown_pct"),
            "calmar": e.get("calmar"),
        })
    out.append("### Headline\n")
    out.append(_table(arm_rows, [
        ("arm", "Arm"), ("trades", "Trades"), ("win_rate_pct", "Win %"),
        ("expectancy_r", "Expectancy (R)"), ("profit_factor", "Profit factor"),
        ("cagr_pct", "CAGR %"), ("sharpe", "Sharpe"), ("sortino", "Sortino"),
        ("max_drawdown_pct", "Max DD %"), ("calmar", "Calmar"),
    ]))
    out.append("_Per-trade statistics are insensitive to the position cap. CAGR, Sharpe, "
               "Sortino, Calmar and max drawdown are **not** — they depend on the "
               f"{p.get('max_concurrent_positions')}-position concurrency limit and the "
               f"${p.get('account_size'):,.0f} account size._\n")

    # ── trade mechanics ───────────────────────────────────────────────────────
    mech_rows = []
    for name, a in summary.get("arms", {}).items():
        t = a.get("trade_stats", {})
        mech_rows.append({
            "arm": name,
            "avg_holding_days": t.get("avg_holding_days"),
            "median_holding_days": t.get("median_holding_days"),
            "avg_win": t.get("avg_win"),
            "avg_loss": t.get("avg_loss"),
            "avg_win_loss_ratio": t.get("avg_win_loss_ratio"),
            "total_turnover": t.get("total_turnover"),
            "total_costs": t.get("total_costs"),
            "slippage_pct_of_gross": t.get("slippage_pct_of_gross"),
        })
    out.append("### Trade mechanics and execution cost\n")
    out.append(_table(mech_rows, [
        ("arm", "Arm"), ("avg_holding_days", "Avg hold (d)"),
        ("median_holding_days", "Median hold (d)"), ("avg_win", "Avg win $"),
        ("avg_loss", "Avg loss $"), ("avg_win_loss_ratio", "Win/Loss ratio"),
        ("total_turnover", "Turnover $"), ("total_costs", "Costs $"),
        ("slippage_pct_of_gross", "Cost % of gross"),
    ]))

    # ── statistical checks ────────────────────────────────────────────────────
    out.append("## Statistical honesty checks\n")
    stat_rows = []
    for name, a in summary.get("arms", {}).items():
        bt, eb = a.get("bootstrap_trades", {}), a.get("bootstrap_equity", {})
        stat_rows.append({
            "arm": name,
            "n": bt.get("n_trades"),
            "expectancy_r": bt.get("expectancy_r"),
            "exp_ci": _ci(bt.get("expectancy_r_ci")),
            "sharpe_ci": _ci(eb.get("sharpe_ci")),
            "cagr_ci": _ci(eb.get("cagr_ci_pct"), "%"),
            "thin": bt.get("thin_sample"),
        })
    out.append(_table(stat_rows, [
        ("arm", "Arm"), ("n", "N trades"), ("expectancy_r", "Expectancy (R)"),
        ("exp_ci", "Expectancy 95% CI"), ("sharpe_ci", "Sharpe 95% CI"),
        ("cagr_ci", "CAGR 95% CI"), ("thin", "Thin sample"),
    ]))
    out.append("_Confidence intervals come from a moving-block bootstrap (20-day blocks) on "
               "daily returns and an i.i.d. bootstrap on per-trade R. Blocks are used because "
               "an i.i.d. resample of an equity curve destroys serial correlation and produces "
               "spuriously tight intervals._\n")

    for name, verdicts in summary.get("statistical_checks", {}).items():
        out.append(f"**{name}**\n")
        for v in verdicts:
            out.append(f"- {v}\n")
    out.append("")

    out.append("### Arm comparisons (permutation tests)\n")
    for key, c in summary.get("comparisons", {}).items():
        if not c.get("available"):
            out.append(f"- `{key}`: not available — {c.get('reason')}\n")
            continue
        out.append(
            f"- `{key}` — {c.get('question', '')} "
            f"mean {c['mean_a']} vs {c['mean_b']} (diff {c['observed_diff']}R), "
            f"p = {c['p_value']} over {c['iterations']} permutations → "
            f"{'significant' if c['significant_at_5pct'] else 'not significant'} at 5%.\n"
        )
    out.append("")

    # ── benchmarks ────────────────────────────────────────────────────────────
    out.append("## Benchmark comparison\n")
    bench = summary.get("benchmark", {})
    if bench.get("available"):
        bench_rows = [{
            "series": f"Buy & hold {p.get('benchmark_symbol')}",
            "cagr_pct": bench.get("cagr_pct"), "sharpe": bench.get("sharpe"),
            "sortino": bench.get("sortino"), "max_drawdown_pct": bench.get("max_drawdown_pct"),
            "total_return_pct": bench.get("total_return_pct"),
        }]
        for name, a in summary.get("arms", {}).items():
            e = a.get("equity_stats", {})
            bench_rows.append({
                "series": name, "cagr_pct": e.get("cagr_pct"), "sharpe": e.get("sharpe"),
                "sortino": e.get("sortino"), "max_drawdown_pct": e.get("max_drawdown_pct"),
                "total_return_pct": e.get("total_return_pct"),
            })
        out.append(_table(bench_rows, [
            ("series", "Series"), ("total_return_pct", "Total return %"),
            ("cagr_pct", "CAGR %"), ("sharpe", "Sharpe"), ("sortino", "Sortino"),
            ("max_drawdown_pct", "Max DD %"),
        ]))
    else:
        out.append(f"_Benchmark unavailable: {bench.get('reason')}._\n")

    # ── segmentation ──────────────────────────────────────────────────────────
    out.append(f"## Segmentation — `{summary.get('primary_arm')}` arm\n")
    out.append("_Edge is reported by bucket rather than blended. Buckets below the "
               f"{p.get('thin_sample_threshold')}-trade threshold are kept and flagged, not "
               "hidden — a 4-trade bucket with a great number is noise, and deleting it "
               "would be as misleading as presenting it without the caveat._\n")

    seg_titles = {
        "cluster_size": "By cluster size (distinct insiders buying)",
        "role": "By role of the most senior buyer",
        "market_cap": "By market-cap bucket",
        "earnings_flag": "By earnings-proximity flag",
        "trigger_type": "By technical trigger type",
        "exit_reason": "By exit reason",
    }
    for key, title in seg_titles.items():
        rows = summary.get("segments", {}).get(key) or []
        out.append(f"### {title}\n")
        out.append(_table(rows, [
            ("bucket", "Bucket"), ("trades", "Trades"), ("win_rate_pct", "Win %"),
            ("expectancy_r", "Expectancy (R)"), ("avg_return_pct", "Avg return %"),
            ("profit_factor", "Profit factor"), ("avg_holding_days", "Avg hold (d)"),
            ("thin_sample", "Thin sample"),
        ]))

    # ── walk forward ──────────────────────────────────────────────────────────
    if walk_forward:
        out.append("## Walk-forward windows\n")
        out.append(f"_{walk_forward.get('caveat', '')}_\n")
        for arm_name, wf in walk_forward.get("by_arm", {}).items():
            out.append(f"### {arm_name}\n")
            out.append(_table(wf.get("per_window", []), [
                ("window", "Window"), ("start", "Start"), ("end", "End"),
                ("trades", "Trades"), ("win_rate_pct", "Win %"),
                ("expectancy_r", "Expectancy (R)"), ("sharpe", "Sharpe"),
                ("max_drawdown_pct", "Max DD %"), ("thin_sample", "Thin"),
            ]))
            out.append(f"**Verdict:** {wf.get('verdict')}\n")

    # ── sweep ─────────────────────────────────────────────────────────────────
    if sweep:
        out.append("## Parameter stability\n")
        stab = sweep.get("stability", {})
        out.append(f"**{stab.get('verdict', 'Not assessed.')}**\n")
        if stab.get("available"):
            out.append(f"- Best cell: `{stab.get('best_cell')}`\n")
            out.append(f"- Neighbour retention: {_fmt(stab.get('neighbour_retention'))} "
                       "(mean of adjacent cells ÷ peak)\n")
            out.append(f"- Cells with positive expectancy: "
                       f"{_fmt(stab.get('cells_positive_pct'), '%')}\n")
            out.append(f"- Median expectancy across the grid: {_fmt(stab.get('median_metric'))}R\n")
        grid = sweep.get("grid") or []
        if grid:
            out.append("\n### Sweep grid\n")
            out.append(_table(grid, [
                ("conviction_threshold", "Threshold"), ("cluster_window_days", "Cluster win (d)"),
                ("confirmation_window_days", "Confirm win (d)"), ("trades", "Trades"),
                ("win_rate_pct", "Win %"), ("expectancy_r", "Expectancy (R)"),
                ("cagr_pct", "CAGR %"), ("sharpe", "Sharpe"),
                ("max_drawdown_pct", "Max DD %"), ("thin_sample", "Thin"),
            ]))
        out.append("\n_How to read this: the question is **not** which cell is best. It is "
                   "whether performance is smooth across neighbouring cells (a plateau — the "
                   "effect is real and insensitive to the exact setting) or concentrated in one "
                   "isolated cell (a spike — the setting was fitted to noise)._\n")

    # ── data + limitations ────────────────────────────────────────────────────
    out.append("## Data coverage\n")
    cov = summary.get("price_coverage", {})
    out.append(f"- Universe tickers with usable price history: **{cov.get('with_price')}** "
               f"({_fmt(cov.get('coverage_pct'), '%')})\n")
    out.append(f"- Missing price history: **{cov.get('missing')}** "
               f"(sample: {', '.join(cov.get('missing_sample', [])[:10]) or '—'})\n")

    out.append("\n## Known limitations\n")
    out.append(
        "1. **Filing lag is real and modelled, but coarse.** Form 4 allows 2 business days "
        "between the trade and the disclosure, and this system can only act on the "
        f"disclosure. Entries are scheduled {p.get('signal_lag_days')} session(s) after the "
        "filing date and filled at the following open. Filings accepted after hours are "
        "not tradeable that day, which this respects; intraday timing within the entry "
        "session is not modelled.\n"
    )
    out.append(
        "2. **Small samples.** Qualifying cluster events are genuinely rare. Where an arm or "
        f"bucket falls below {p.get('thin_sample_threshold')} trades it is flagged, and no "
        "conclusion should be drawn from it regardless of how good the number looks.\n"
    )
    out.append(
        "3. **Earnings-proximity flag is near-point-in-time, not exact.** The calendar source "
        "returns *actual reported* earnings dates, not the date scheduled at the time of the "
        "filing. Companies occasionally move a print. The flag is therefore used only as a "
        "score multiplier and a reporting bucket, never as an entry or exit condition, so the "
        "approximation cannot leak into simulated returns.\n"
    )
    out.append(
        "4. **Rule 10b5-1 coverage is partial.** The Form 4 checkbox only exists on filings "
        "from 2022 onward. Earlier filings are detected only when a footnote mentions the "
        "plan explicitly. Pre-2022 rows with unknown plan status are KEPT by default "
        f"(`exclude_unknown_10b5_1={p.get('exclude_unknown_10b5_1')}`) — dropping them would "
        "delete most of the history — so some mechanical plan trades survive the noise "
        "filter in the earlier part of the sample.\n"
    )
    out.append(
        "5. **Price coverage on delisted names.** The universe is point-in-time correct and "
        "includes companies that were later removed from the index, but price history for "
        "some of those names is no longer retrievable. Those trades cannot be simulated, so "
        "a residual survivorship bias remains in the price layer even though the membership "
        "layer is corrected.\n"
    )
    out.append(
        "6. **Market-cap segmentation uses current shares outstanding.** No historical "
        "share-count series is available, so the market-cap bucket drifts for companies with "
        "heavy buybacks or issuance. It is used for reporting only, never for entry.\n"
    )
    out.append(
        "7. **Slippage is a model, not a measurement.** A square-root impact law "
        f"(`{p.get('base_spread_bps')}bps + {p.get('impact_coefficient')}×√participation`) is "
        "more honest than a flat bps assumption for small caps, but it is still an assumption. "
        "Real fills in thin names can be worse.\n"
    )
    for note in summary.get("notes", []):
        out.append(f"8. {note}\n")

    out.append("\n## Full parameter set\n")
    out.append("```json\n" + json.dumps(p, indent=2, sort_keys=True) + "\n```\n")

    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────────
#  Writers
# ──────────────────────────────────────────────────────────────────────────────

def write_report(
    summary: dict,
    walk_forward: Optional[dict] = None,
    sweep: Optional[dict] = None,
    conf: Optional[cfg.InsiderConfig] = None,
    label: Optional[str] = None,
    also_html: bool = True,
) -> dict:
    """Write the markdown (and optionally HTML) report.  Returns the paths."""
    conf = conf or cfg.DEFAULT_CONFIG
    cfg.ensure_dirs()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"{label or summary.get('label', 'insider_swing')}_{stamp}"
    md_path = cfg.REPORT_DIR / f"{name}.md"
    md = render_markdown(summary, walk_forward, sweep, conf)
    md_path.write_text(md, encoding="utf-8")

    paths = {"markdown": str(md_path)}

    if also_html:
        html_path = cfg.REPORT_DIR / f"{name}.html"
        html_path.write_text(_to_html(md, name), encoding="utf-8")
        paths["html"] = str(html_path)

    json_path = cfg.REPORT_DIR / f"{name}.json"
    json_path.write_text(
        json.dumps({"summary": summary, "walk_forward": walk_forward, "sweep": sweep},
                   indent=2, default=str),
        encoding="utf-8",
    )
    paths["json"] = str(json_path)

    logger.info("Report written: %s", md_path)
    return paths


def _to_html(md: str, title: str) -> str:
    """
    Minimal self-contained HTML.  Uses ``markdown`` when installed and falls back
    to a <pre> block otherwise — a missing optional dependency should degrade the
    formatting, not fail the run.
    """
    try:
        import markdown as md_lib
        body = md_lib.markdown(md, extensions=["tables", "fenced_code"])
    except Exception:
        body = "<pre>" + (md.replace("&", "&amp;").replace("<", "&lt;")) + "</pre>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 1100px;
        margin: 2rem auto; padding: 0 1rem; line-height: 1.55; color: #1a1a1a; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }}
 th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
 th {{ background: #f4f4f4; text-align: left; }}
 td:first-child, th:first-child {{ text-align: left; }}
 blockquote {{ border-left: 4px solid #bbb; margin-left: 0; padding-left: 1rem; color: #444; }}
 code, pre {{ background: #f6f8fa; padding: 2px 5px; border-radius: 4px; }}
 pre {{ padding: 1rem; overflow-x: auto; }}
 h1, h2 {{ border-bottom: 1px solid #eee; padding-bottom: 0.3rem; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #14161a; color: #e6e6e6; }}
   th {{ background: #22262d; }} th, td {{ border-color: #333; }}
   code, pre {{ background: #1d2026; }}
   h1, h2 {{ border-color: #2a2f37; }}
 }}
</style></head><body>
{body}
</body></html>"""
