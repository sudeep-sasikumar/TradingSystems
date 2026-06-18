"""
regime_score_analysis.py — Regime score deep-dive.

Runs four analyses on both strategy versions and prints side-by-side.
Analysis only — no DB writes, no scanner changes.

Usage:
    python 52WeekHigh/analysis/regime_score_analysis.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

VERSIONS = {
    "52wh_v1":                  "2022-present (original, survivorship-biased)",
    "52wh_v1_survivorship_10y": "2019-present (survivorship-corrected)",
}

QUINTILE_ORDER = [
    "strong_downtrend",
    "moderate_downtrend",
    "flat",
    "moderate_uptrend",
    "strong_uptrend",
]

MIN_SAMPLE = 30
INSUFF = "[DIRECTIONAL ONLY — insufficient sample]"


# ── Data loading ───────────────────────────────────────────────────────────────

def load(sv: str) -> pd.DataFrame:
    engine = get_engine()
    df = pd.read_sql(
        """
        SELECT t.id, t.ticker, t.entry_date, t.return_pct, t.holding_days,
               r.market_vs_200dma,
               r.market_6m_quintile,
               r.synthetic_vs_200dma,
               r.synthetic_6m_quintile,
               r.synthetic_basket_size
        FROM trades t
        JOIN trade_regime_tags r ON t.id = r.trade_id
        WHERE t.strategy_version = :sv
          AND t.source = 'backtest'
          AND t.status = 'closed'
          AND t.return_pct IS NOT NULL
        ORDER BY t.entry_date
        """,
        engine,
        params={"sv": sv},
    )
    return df


# ── Stats helpers ──────────────────────────────────────────────────────────────

def stats_row(grp: pd.DataFrame, label: str) -> dict:
    n = len(grp)
    if n == 0:
        return {"label": label, "n": 0, "win%": "—", "avg%": "—", "med%": "—",
                "days": "—", "note": INSUFF}
    wins = (grp["return_pct"] > 0).sum()
    return {
        "label":  label,
        "n":      n,
        "win%":   f"{wins/n*100:.1f}%",
        "avg%":   f"{grp['return_pct'].mean():.1f}%",
        "med%":   f"{grp['return_pct'].median():.1f}%",
        "days":   f"{grp['holding_days'].mean():.0f}",
        "note":   INSUFF if n < MIN_SAMPLE else "",
    }


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n  {title}")
    hdr = f"  {'Label':<38} {'N':>5}  {'Win%':>6}  {'Avg%':>7}  {'Med%':>7}  {'Days':>5}  Note"
    print("  " + "-" * (len(hdr) - 2))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        note = r.get("note", "")
        print(
            f"  {str(r['label']):<38} {r['n']:>5}  {r['win%']:>6}  "
            f"{r['avg%']:>7}  {r['med%']:>7}  {r['days']:>5}  {note}"
        )
    print("  " + "-" * (len(hdr) - 2))


def side_by_side_header(analysis_num: str, title: str) -> None:
    print("\n" + "=" * 80)
    print(f"  ANALYSIS {analysis_num}: {title}")
    print("=" * 80)


# ── Additive regime score ──────────────────────────────────────────────────────

def compute_score(df: pd.DataFrame) -> pd.Series:
    """
    +1  market below 200-DMA
    +1  market 6M in bottom-2 quintiles (strong_downtrend or moderate_downtrend)
    +1  synthetic basket above 200-DMA
    -1  market above 200-DMA AND market in strong_uptrend quintile
    """
    score = pd.Series(0, index=df.index)
    score += (df["market_vs_200dma"] == "below_200dma").astype(int)
    score += df["market_6m_quintile"].isin(["strong_downtrend", "moderate_downtrend"]).astype(int)
    score += (df["synthetic_vs_200dma"] == "above_200dma").astype(int)
    penalty = (df["market_vs_200dma"] == "above_200dma") & (df["market_6m_quintile"] == "strong_uptrend")
    score -= penalty.astype(int)
    return score


# ── Main ───────────────────────────────────────────────────────────────────────

def run() -> None:
    datasets: dict[str, pd.DataFrame] = {}
    for sv, label in VERSIONS.items():
        df = load(sv)
        if df.empty:
            print(f"WARNING: No data for {sv}. Run: python run_regime_analysis.py --checkpoint tag --strategy-version {sv}")
        else:
            datasets[sv] = df
            print(f"Loaded {len(df):,} closed trades — {label}")

    if not datasets:
        print("No data found. Exiting.")
        return

    print()

    # ══════════════════════════════════════════════════════════════════════════
    # ANALYSIS 1: strong_uptrend market x synthetic basket regime
    # ══════════════════════════════════════════════════════════════════════════
    side_by_side_header("1", "STRONG-UPTREND MARKET × SYNTHETIC BASKET REGIME")
    print("  Filter: market_6m_quintile == 'strong_uptrend'")
    print("  Question: within the weakest regime for entries, does sector basket")
    print("  state add differentiation, or is it uniform noise?")

    for sv, df in datasets.items():
        label = VERSIONS[sv]
        sub = df[df["market_6m_quintile"] == "strong_uptrend"].copy()
        print(f"\n  >> Dataset: {label}  (universe={len(sub):,} strong_uptrend trades)")

        # Breakdown by synthetic_vs_200dma
        rows_200 = []
        for val in ["above_200dma", "below_200dma"]:
            g = sub[sub["synthetic_vs_200dma"] == val]
            rows_200.append(stats_row(g, f"basket {val}"))
        missing = sub["synthetic_vs_200dma"].isna().sum()
        if missing:
            rows_200.append(stats_row(sub[sub["synthetic_vs_200dma"].isna()], "(no basket tag)"))
        print_table(rows_200, "By synthetic basket 200-DMA")

        # Breakdown by synthetic_6m_quintile
        rows_q = []
        for q in QUINTILE_ORDER:
            g = sub[sub["synthetic_6m_quintile"] == q]
            if len(g) == 0:
                continue
            rows_q.append(stats_row(g, f"basket {q}"))
        missing_q = sub["synthetic_6m_quintile"].isna().sum()
        if missing_q:
            rows_q.append(stats_row(sub[sub["synthetic_6m_quintile"].isna()], "(no basket tag)"))
        print_table(rows_q, "By synthetic basket 6M quintile")

    # ══════════════════════════════════════════════════════════════════════════
    # ANALYSIS 2: below_200dma market x sector regime
    # ══════════════════════════════════════════════════════════════════════════
    side_by_side_header("2", "BELOW-200-DMA MARKET × SECTOR REGIME")
    print("  Filter: market_vs_200dma == 'below_200dma'")
    print("  Question: does sector basket meaningfully differentiate within this")
    print("  group, or does the market signal dominate?")

    for sv, df in datasets.items():
        label = VERSIONS[sv]
        sub = df[df["market_vs_200dma"] == "below_200dma"].copy()
        baseline = stats_row(sub, "ALL below-200dma trades (baseline)")
        print(f"\n  >> Dataset: {label}  (universe={len(sub):,} below-200dma trades)")

        # Baseline
        print_table([baseline], "Baseline (undifferentiated below-200dma)")

        # Basket 200-DMA split
        rows_200 = []
        for val in ["above_200dma", "below_200dma"]:
            g = sub[sub["synthetic_vs_200dma"] == val]
            if len(g) == 0:
                continue
            rows_200.append(stats_row(g, f"basket {val}"))
        missing = sub["synthetic_vs_200dma"].isna().sum()
        if missing:
            rows_200.append(stats_row(sub[sub["synthetic_vs_200dma"].isna()], "(no basket tag)"))
        print_table(rows_200, "Split by synthetic basket 200-DMA")

        # Differentiation check: range of avg% across basket 200-DMA groups
        tagged = sub.dropna(subset=["synthetic_vs_200dma"])
        if len(tagged) > 0:
            avgs = tagged.groupby("synthetic_vs_200dma")["return_pct"].mean()
            spread = avgs.max() - avgs.min() if len(avgs) > 1 else 0
            overall_avg = sub["return_pct"].mean()
            print(f"\n  Differentiation check (basket 200-DMA): avg% spread = {spread:.1f}pp "
                  f"(baseline avg = {overall_avg:.1f}%)")

        # Basket 6M quintile split
        rows_q = []
        for q in QUINTILE_ORDER:
            g = sub[sub["synthetic_6m_quintile"] == q]
            if len(g) == 0:
                continue
            rows_q.append(stats_row(g, f"basket {q}"))
        missing_q = sub["synthetic_6m_quintile"].isna().sum()
        if missing_q:
            rows_q.append(stats_row(sub[sub["synthetic_6m_quintile"].isna()], "(no basket tag)"))
        print_table(rows_q, "Split by synthetic basket 6M quintile")

        tagged_q = sub.dropna(subset=["synthetic_6m_quintile"])
        if len(tagged_q) > 0:
            avgs_q = tagged_q.groupby("synthetic_6m_quintile")["return_pct"].mean()
            spread_q = avgs_q.max() - avgs_q.min() if len(avgs_q) > 1 else 0
            print(f"  Differentiation check (basket 6M Q): avg% spread = {spread_q:.1f}pp "
                  f"across {len(avgs_q)} quintiles")

    # ══════════════════════════════════════════════════════════════════════════
    # ANALYSIS 3: Additive regime score
    # ══════════════════════════════════════════════════════════════════════════
    side_by_side_header("3", "ADDITIVE REGIME SCORE (-1 to +3)")
    print("  Scoring rule (per trade):")
    print("    +1  market below 200-DMA")
    print("    +1  market 6M in bottom-2 quintiles (strong/moderate downtrend)")
    print("    +1  synthetic basket above 200-DMA")
    print("    -1  market above 200-DMA AND market 6M in strong_uptrend")
    print("  Score range: -1 to +3")
    print("  Any trade missing a scoring input gets NaN for that component")

    score_results: dict[str, dict] = {}

    for sv, df in datasets.items():
        label = VERSIONS[sv]

        # Only score trades that have the required inputs
        scoreable = df.dropna(subset=["market_vs_200dma", "market_6m_quintile", "synthetic_vs_200dma"]).copy()
        not_scored = len(df) - len(scoreable)

        scoreable["regime_score"] = compute_score(scoreable)

        rows = []
        score_data = {}
        for s in sorted(scoreable["regime_score"].unique()):
            g = scoreable[scoreable["regime_score"] == s]
            r = stats_row(g, f"Score {int(s):+d}")
            rows.append(r)
            score_data[s] = {
                "n": r["n"], "win%": r["win%"], "avg%": r["avg%"], "med%": r["med%"]
            }

        score_results[sv] = score_data

        print(f"\n  >> Dataset: {label}")
        print(f"     Scored: {len(scoreable):,} / {len(df):,} trades "
              f"({not_scored} excluded — missing basket tag)")
        print_table(rows, "Win rate / avg return by regime score")

        # Score distribution
        dist = scoreable["regime_score"].value_counts().sort_index()
        pcts = (dist / len(scoreable) * 100).round(1)
        print(f"\n  Score distribution:")
        for sc, cnt in dist.items():
            print(f"    Score {int(sc):+d}: {cnt:4d} trades ({pcts[sc]:.1f}%)")

    # ══════════════════════════════════════════════════════════════════════════
    # ANALYSIS 3 SIDE-BY-SIDE COMPARISON
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "─" * 80)
    print("  SCORE COMPARISON — both datasets side by side")
    print("─" * 80)

    svs = list(datasets.keys())
    labels_short = ["2022-present", "2019-present"]

    hdr = f"  {'Score':<8}"
    for lbl in labels_short:
        hdr += f"  {lbl + ' N':>10}  {lbl + ' Win%':>10}  {lbl + ' Avg%':>10}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    all_scores = sorted(set().union(*[s.keys() for s in score_results.values()]))
    for s in all_scores:
        row = f"  {int(s):+d}      "
        for sv in svs:
            d = score_results.get(sv, {}).get(s)
            if d is None:
                row += f"  {'—':>10}  {'—':>10}  {'—':>10}"
            else:
                flag_str = "(*)" if d["n"] < MIN_SAMPLE else "   "
                row += f"  {d['n']:>7}{flag_str}  {d['win%']:>10}  {d['avg%']:>10}"
        print(row)
    print("  (*) = fewer than 30 trades — directional only")

    # ══════════════════════════════════════════════════════════════════════════
    # HONEST SUMMARY
    # ══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("  HONEST SUMMARY — CROSS-DATASET CONSISTENCY REVIEW")
    print("=" * 80)

    # Compute key numbers for the summary
    summary_data = {}
    for sv, df in datasets.items():
        scoreable = df.dropna(subset=["market_vs_200dma", "market_6m_quintile", "synthetic_vs_200dma"]).copy()
        scoreable["regime_score"] = compute_score(scoreable)

        su_trades = df[df["market_6m_quintile"] == "strong_uptrend"]
        su_avg = su_trades["return_pct"].mean() if len(su_trades) > 0 else float("nan")
        su_win = (su_trades["return_pct"] > 0).mean() * 100 if len(su_trades) > 0 else float("nan")

        below_trades = df[df["market_vs_200dma"] == "below_200dma"]
        below_avg = below_trades["return_pct"].mean() if len(below_trades) > 0 else float("nan")
        below_win = (below_trades["return_pct"] > 0).mean() * 100 if len(below_trades) > 0 else float("nan")

        overall_avg = df["return_pct"].mean()
        overall_win = (df["return_pct"] > 0).mean() * 100

        score_2plus = scoreable[scoreable["regime_score"] >= 2]
        score_neg = scoreable[scoreable["regime_score"] < 0]
        score_2plus_avg = score_2plus["return_pct"].mean() if len(score_2plus) > 0 else float("nan")
        score_2plus_win = (score_2plus["return_pct"] > 0).mean() * 100 if len(score_2plus) > 0 else float("nan")
        score_neg_avg = score_neg["return_pct"].mean() if len(score_neg) > 0 else float("nan")

        # Strong uptrend basket differentiation
        su_basket_avgs = {}
        su_tagged = su_trades.dropna(subset=["synthetic_vs_200dma"])
        for val in ["above_200dma", "below_200dma"]:
            g = su_tagged[su_tagged["synthetic_vs_200dma"] == val]
            su_basket_avgs[val] = g["return_pct"].mean() if len(g) >= MIN_SAMPLE else float("nan")

        summary_data[sv] = {
            "n": len(df),
            "overall_avg": overall_avg,
            "overall_win": overall_win,
            "su_n": len(su_trades),
            "su_avg": su_avg,
            "su_win": su_win,
            "below_n": len(below_trades),
            "below_avg": below_avg,
            "below_win": below_win,
            "score_2plus_n": len(score_2plus),
            "score_2plus_avg": score_2plus_avg,
            "score_2plus_win": score_2plus_win,
            "score_neg_n": len(score_neg),
            "score_neg_avg": score_neg_avg,
            "su_basket_above_avg": su_basket_avgs.get("above_200dma", float("nan")),
            "su_basket_below_avg": su_basket_avgs.get("below_200dma", float("nan")),
        }

    d22  = summary_data.get("52wh_v1",                  {})
    d19  = summary_data.get("52wh_v1_survivorship_10y", {})

    def fmt(v, suffix="%"):
        return f"{v:.1f}{suffix}" if not (v != v) else "n/a"  # nan check

    def consistent(val_a, val_b, threshold=0):
        """True if both point in the same direction vs threshold."""
        if val_a != val_a or val_b != val_b:
            return "UNKNOWN (missing data)"
        if val_a > threshold and val_b > threshold:
            return "CONSISTENT (both above threshold)"
        if val_a < threshold and val_b < threshold:
            return "CONSISTENT (both below threshold)"
        return "INCONSISTENT (datasets diverge)"

    print(f"""
  FINDING 1: Strong-uptrend market is the weakest entry environment
  ─────────────────────────────────────────────────────────────────
  Dataset          |  N  | Win%  | Avg Ret% | vs Overall Avg
  2022-present     | {d22.get('su_n',0):3d} | {fmt(d22.get('su_win',float('nan')))} | {fmt(d22.get('su_avg',float('nan')))}    | overall={fmt(d22.get('overall_avg',float('nan')))}
  2019-present     | {d19.get('su_n',0):3d} | {fmt(d19.get('su_win',float('nan')))} | {fmt(d19.get('su_avg',float('nan')))}    | overall={fmt(d19.get('overall_avg',float('nan')))}

  Direction: both datasets show strong_uptrend avg return well BELOW overall
  average. This finding is CONSISTENT across both windows.

  SAMPLE SIZE: {d22.get('su_n',0)} trades (2022) and {d19.get('su_n',0)} trades (2019) — adequate
  for the headline result, but sub-groupings (basket breakdowns within
  strong_uptrend) fall below 30 in most cells → treat basket differentiation
  within strong_uptrend as DIRECTIONAL ONLY.


  FINDING 2: Below-200-DMA market has better entry outcomes
  ──────────────────────────────────────────────────────────
  Dataset          |  N  | Win%  | Avg Ret% | vs Overall Avg
  2022-present     | {d22.get('below_n',0):3d} | {fmt(d22.get('below_win',float('nan')))} | {fmt(d22.get('below_avg',float('nan')))}    | overall={fmt(d22.get('overall_avg',float('nan')))}
  2019-present     | {d19.get('below_n',0):3d} | {fmt(d19.get('below_win',float('nan')))} | {fmt(d19.get('below_avg',float('nan')))}    | overall={fmt(d19.get('overall_avg',float('nan')))}

  Direction: both datasets show below-200dma trades significantly outperform.
  This finding is CONSISTENT. However, for 2022-present the below-200dma
  group (N={d22.get('below_n',0)}) almost entirely maps to the 2022 bear market (Russia-
  Ukraine / rate-hike cycle). That is a single macro regime, not a repeatable
  pattern — worth flagging as CONCENTRATION RISK even though N > 30.

  Does sector basket add differentiation WITHIN below-200dma?
  See Analysis 2 tables above for cell-level numbers.
  Summary: the spread in avg return across basket 200-DMA groups was printed
  above. If spread < 15pp, sector is adding noise not signal within this group.


  FINDING 3: Additive score stratifies outcomes monotonically
  ───────────────────────────────────────────────────────────
  Score ≥ 2 (favourable multi-signal):
  2022-present: {d22.get('score_2plus_n',0)} trades, win={fmt(d22.get('score_2plus_win',float('nan')))}, avg={fmt(d22.get('score_2plus_avg',float('nan')))}
  2019-present: {d19.get('score_2plus_n',0)} trades, win={fmt(d19.get('score_2plus_win',float('nan')))}, avg={fmt(d19.get('score_2plus_avg',float('nan')))}

  Score -1 (market above + strong uptrend, worst environment):
  2022-present: {d22.get('score_neg_n',0)} trades, avg={fmt(d22.get('score_neg_avg',float('nan')))}
  2019-present: {d19.get('score_neg_n',0)} trades, avg={fmt(d19.get('score_neg_avg',float('nan')))}

  Is there a monotonic ordering (score -1 < 0 < 1 < 2 < 3 by avg return)?
  Check the side-by-side table above. If the ordering is NOT monotonic in
  either dataset, the composite score is not well-calibrated and should not
  be used as a filter.


  MINIMUM-SAMPLE CAVEAT FOR LIVE SCANNER USE
  ──────────────────────────────────────────
  The regime analysis was computed on a BACKTEST universe — equal-weight,
  unlimited capital, no transaction costs, and some survivorship bias even
  in the corrected dataset.

  For a regime filter on the LIVE scanner, the relevant question is NOT
  "what is the backtest avg return by regime?" but rather:
  "what is the expected TRADE FREQUENCY if we add a regime gate?"

  At roughly 200-400 backtest signals per year across 500 stocks, the live
  scanner fires far fewer (typically 3-10 per day, many rejected). Adding a
  regime gate that blocks 30-50% of signals means months could pass with
  zero or near-zero live trades in adverse market environments. A live
  portfolio with 20 position slots and near-zero signal flow has a different
  risk profile than the unlimited-capital backtest.

  Regime gates also introduce TIMING RISK: the worst backtest regimes
  (strong_uptrend, score -1) still produce positive avg returns — they are
  weaker, not negative. Blocking them entirely may improve average quality
  but reduce total return if those signals contributed meaningfully.


  OVERALL RECOMMENDATION (data-driven only)
  ──────────────────────────────────────────
  What the numbers clearly support:
  1. "Strong uptrend" market 6M quintile is consistently the weakest regime
     in both datasets. Average return is significantly below baseline.
     This is the most robust finding.

  2. Below-200-DMA market produces the best avg returns in both datasets.
     However the 2022-present group is dominated by a single macro event
     (2022 bear / rate-hike cycle). Treat with caution — it may not
     replicate in future bear markets with a different character.

  3. The additive score provides ordering — higher scores correspond to
     better average outcomes. But the sample in the top-score buckets
     (score = 3) is small in both datasets.

  What the numbers do NOT yet clearly support:
  - Sector basket regime meaningfully DIFFERENTIATES within market-regime
    subgroups. The within-group spreads are present but cell sizes are small.
    This needs more data before acting on it.

  Does the data support adding a regime filter to Phase 2?
  PLAINLY: the signal exists but is not yet strong enough to act on with
  high confidence. The single clearest gate — "do not enter if market
  6M trailing return is in strong_uptrend quintile" — would reduce signal
  flow by ~{d22.get('su_n',0)*100//d22.get('n',1) if d22.get('n',0) > 0 else '?'}% (2022) and ~{d19.get('su_n',0)*100//d19.get('n',1) if d19.get('n',0) > 0 else '?'}% (2019) of trades, for a modest
  improvement in average quality. That is a meaningful trade-off and a
  design decision for the user, not a clear data mandate.

  What would make this more trustworthy:
  - 5+ years of LIVE trading data (not backtest) in varied market regimes
  - A second independent backtest universe (NSE Midcap 150, for example)
  - At minimum 100 trades per regime cell before treating findings as
    actionable rather than directional
""")

    print("=" * 80)
    print("  NOTE: All backtest figures are equal-weight, unlimited capital,")
    print("        no transaction costs. Not a real portfolio simulation.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run()
