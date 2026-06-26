"""
regime_analysis.py — Cross-tab analysis of regime-tagged trades.

Loads closed trades joined with trade_regime_tags and prints:
  Part 7: single-dimension breakdowns (each regime col independently)
  Part 8: weak-market × sector regime cross-tabs + top/bottom 5 combos

Only closed trades are analysed (return_pct is meaningful only on close).
Cells with fewer than MIN_COUNT trades are flagged with '*'.
"""

from __future__ import annotations

import itertools
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

MIN_COUNT = 30   # cells below this are flagged — too few for reliable inference

logger = logging.getLogger(__name__)


# ── Data loading ───────────────────────────────────────────────────────────────

def load_tagged_closed_trades(strategy_version: str = "52wh_v1_survivorship_10y") -> pd.DataFrame:
    """Load closed trades joined with regime tags for the given strategy version."""
    engine = get_engine()
    df = pd.read_sql(
        """
        SELECT t.id, t.ticker, t.entry_date, t.trade_year,
               t.return_pct, t.holding_days,
               r.market_vs_200dma,
               r.market_6m_quintile,
               r.market_dist_200dma_pct,
               r.market_6m_return_pct,
               r.official_sector,
               r.official_vs_200dma,
               r.official_6m_quintile,
               r.official_dist_200dma_pct,
               r.official_6m_return_pct,
               r.industry_group,
               r.synthetic_basket_size,
               r.synthetic_vs_200dma,
               r.synthetic_6m_quintile,
               r.synthetic_dist_200dma_pct,
               r.synthetic_6m_return_pct
        FROM trades t
        JOIN trade_regime_tags r ON t.id = r.trade_id
        WHERE t.strategy_version = :sv
          AND t.source = 'backtest'
          AND t.status = 'closed'
          AND t.return_pct IS NOT NULL
        ORDER BY t.entry_date
        """,
        engine,
        params={"sv": strategy_version},
    )
    return df


# ── Stats helpers ──────────────────────────────────────────────────────────────

def _stats(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {"n": 0, "win_pct": None, "avg_ret": None, "median_ret": None,
                "avg_days": None, "flag": "*"}
    wins = (df["return_pct"] > 0).sum()
    return {
        "n":          n,
        "win_pct":    round(wins / n * 100, 1),
        "avg_ret":    round(df["return_pct"].mean(), 2),
        "median_ret": round(df["return_pct"].median(), 2),
        "avg_days":   round(df["holding_days"].mean(), 0),
        "flag":       "*" if n < MIN_COUNT else "",
    }


def breakdown_by(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Summary stats grouped by one regime column, sorted by avg_ret."""
    sub = df.dropna(subset=[col])
    rows = []
    for val in sorted(sub[col].unique()):
        s = _stats(sub[sub[col] == val])
        s[col] = val
        rows.append(s)
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).sort_values("avg_ret", ascending=False)
    return result[[col, "n", "win_pct", "avg_ret", "median_ret", "avg_days", "flag"]]


def cross_breakdown(df: pd.DataFrame, col1: str, col2: str) -> pd.DataFrame:
    """Cross-tab of two regime columns, sorted by avg_ret."""
    sub = df.dropna(subset=[col1, col2])
    rows = []
    for v1 in sorted(sub[col1].unique()):
        for v2 in sorted(sub[col2].unique()):
            g = sub[(sub[col1] == v1) & (sub[col2] == v2)]
            if len(g) == 0:
                continue
            s = _stats(g)
            s[col1] = v1
            s[col2] = v2
            rows.append(s)
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).sort_values("avg_ret", ascending=False)
    return result[[col1, col2, "n", "win_pct", "avg_ret", "median_ret", "avg_days", "flag"]]


def find_top_combinations(
    df: pd.DataFrame,
    regime_cols: list[str],
    top_n: int = 5,
    combo_size: int = 2,
    sort_ascending: bool = False,
) -> pd.DataFrame:
    """
    Find best (or worst) avg_ret regime combinations meeting MIN_COUNT.
    combo_size=2 means pairs, combo_size=3 means triples.
    """
    rows = []
    for cols in itertools.combinations(regime_cols, combo_size):
        sub = df.dropna(subset=list(cols))
        for key, group in sub.groupby(list(cols)):
            s = _stats(group)
            if s["n"] < MIN_COUNT:
                continue
            key_vals = key if isinstance(key, tuple) else (key,)
            for c, v in zip(cols, key_vals):
                s[c] = v
            rows.append(s)

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows).sort_values("avg_ret", ascending=sort_ascending)
    present = [c for c in regime_cols if c in result.columns]
    out_cols = present + ["n", "win_pct", "avg_ret", "median_ret", "avg_days"]
    out_cols = [c for c in out_cols if c in result.columns]
    return result[out_cols].head(top_n)


# ── Full analysis ──────────────────────────────────────────────────────────────

def run_analysis(strategy_version: str = "52wh_v1_survivorship_10y") -> None:
    df = load_tagged_closed_trades(strategy_version)
    if df.empty:
        logger.error(
            f"No tagged closed trades found for strategy_version={strategy_version!r}. "
            "Run: python run_regime_analysis.py --checkpoint tag --strategy-version <version>"
        )
        return

    overall = _stats(df)
    _sep = "=" * 80

    print("\n" + _sep)
    print("REGIME ANALYSIS — 52-Week High, Survivorship-Corrected Historic Backtest")
    print(_sep)
    print(f"Closed trades analysed : {len(df):,}")
    print(f"Date range             : {df['entry_date'].min()} to {df['entry_date'].max()}")
    print(f"Overall win rate       : {overall['win_pct']}%")
    print(f"Overall avg return     : {overall['avg_ret']}%")
    print(f"Overall median return  : {overall['median_ret']}%")
    print(f"  * = fewer than {MIN_COUNT} trades — interpret with caution")

    # ── Coverage report ────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("COVERAGE REPORT")
    print("─" * 60)
    n = len(df)
    mkt_ok  = df["market_vs_200dma"].notna().sum()
    sec_ok  = df["official_vs_200dma"].notna().sum()
    syn_ok  = df["synthetic_vs_200dma"].notna().sum()
    print(f"Market regime tagged             : {mkt_ok:>5,} / {n:,}  ({mkt_ok/n*100:.1f}%)")
    print(f"Official sector regime tagged    : {sec_ok:>5,} / {n:,}  ({sec_ok/n*100:.1f}%)")
    print(f"Synthetic basket regime tagged   : {syn_ok:>5,} / {n:,}  ({syn_ok/n*100:.1f}%)")

    print("\nOfficial sector distribution (of tagged trades):")
    for sec, cnt in df["official_sector"].value_counts(dropna=False).items():
        label = str(sec) if sec is not None else "(null — no matching index)"
        print(f"  {label:<35} {cnt:>5,} trades")

    print("\nSynthetic basket distribution (top 15 by trade count):")
    for ind, cnt in df["industry_group"].value_counts(dropna=False).head(15).items():
        bsize = 0
        if ind is not None and ind in df["industry_group"].values:
            bsize_vals = df.loc[df["industry_group"] == ind, "synthetic_basket_size"]
            if not bsize_vals.empty:
                bsize = int(bsize_vals.iloc[0])
        label = str(ind) if ind is not None else "(null)"
        print(f"  {label:<42} {cnt:>5,} trades  basket={bsize}")

    # ── Part 7: Single-dimension breakdowns ───────────────────────────────────
    print("\n" + "─" * 60)
    print("PART 7 — SINGLE-DIMENSION REGIME BREAKDOWNS")
    print("─" * 60)
    print("Columns: regime_value | Trades | Win% | Avg Ret% | Median Ret% | Avg Days | *flag")

    dims = [
        ("market_vs_200dma",      "Market 200-DMA (^CRSLDX)"),
        ("market_6m_quintile",    "Market 6-Month Return Quintile"),
        ("official_vs_200dma",    "Official Sector 200-DMA"),
        ("official_6m_quintile",  "Official Sector 6M Quintile"),
        ("synthetic_vs_200dma",   "Synthetic Basket 200-DMA"),
        ("synthetic_6m_quintile", "Synthetic Basket 6M Quintile"),
    ]

    for col, label in dims:
        tagged = df.dropna(subset=[col])
        print(f"\n{label}  (n={len(tagged):,} trades with valid tag)")
        result = breakdown_by(tagged, col)
        if result.empty:
            print("  (no data)")
            continue
        result = result.rename(columns={
            "n": "Trades", "win_pct": "Win%", "avg_ret": "Avg Ret%",
            "median_ret": "Median%", "avg_days": "Avg Days", "flag": "!"
        })
        print(result.to_string(index=False))

    # ── Part 8a: Weak market × sector regime ──────────────────────────────────
    print("\n" + "─" * 60)
    print("PART 8a — WEAK MARKET (below 200-DMA) × SECTOR REGIME")
    print("─" * 60)

    below = df[df["market_vs_200dma"] == "below_200dma"].copy()
    above = df[df["market_vs_200dma"] == "above_200dma"].copy()

    print(f"\nMarket ABOVE 200-DMA : {len(above):,} trades")
    print(f"Market BELOW 200-DMA : {len(below):,} trades")

    if len(below) > 0:
        for sec_col, sec_label in [
            ("official_vs_200dma",  "Official Sector 200-DMA"),
            ("official_6m_quintile","Official Sector 6M Quintile"),
            ("synthetic_vs_200dma", "Synthetic Basket 200-DMA"),
            ("synthetic_6m_quintile","Synthetic Basket 6M Quintile"),
        ]:
            sub = below.dropna(subset=[sec_col])
            if sub.empty:
                continue
            print(f"\n  [Below-market] × {sec_label}  (n={len(sub):,} tagged):")
            result = breakdown_by(sub, sec_col)
            if result.empty:
                print("    (no data)")
            else:
                result = result.rename(columns={
                    "n": "Trades", "win_pct": "Win%", "avg_ret": "Avg Ret%",
                    "median_ret": "Median%", "avg_days": "Avg Days", "flag": "!"
                })
                print(result.to_string(index=False))

    # ── Part 8b: Bottom-2-market-quintile × sector regime ─────────────────────
    print("\n" + "─" * 60)
    print("PART 8b — MARKET DOWNTREND QUINTILES × SECTOR REGIME")
    print("─" * 60)
    downtrend_quintiles = {"strong_downtrend", "moderate_downtrend"}
    weak_q = df[df["market_6m_quintile"].isin(downtrend_quintiles)].copy()
    print(f"Trades in bottom-2 market quintiles: {len(weak_q):,}")

    if len(weak_q) > 0:
        for sec_col, sec_label in [
            ("official_6m_quintile",  "Official Sector 6M Quintile"),
            ("synthetic_6m_quintile", "Synthetic Basket 6M Quintile"),
        ]:
            sub = weak_q.dropna(subset=[sec_col])
            if sub.empty:
                continue
            print(f"\n  [Bottom-2-market-Q] × {sec_label}  (n={len(sub):,}):")
            result = breakdown_by(sub, sec_col)
            if result.empty:
                print("    (no data)")
            else:
                result = result.rename(columns={
                    "n": "Trades", "win_pct": "Win%", "avg_ret": "Avg Ret%",
                    "median_ret": "Median%", "avg_days": "Avg Days", "flag": "!"
                })
                print(result.to_string(index=False))

    # ── Part 8c: Top/Bottom 5 combinations ────────────────────────────────────
    print("\n" + "─" * 60)
    print("PART 8c — TOP / BOTTOM 5 REGIME COMBINATIONS (min 30 trades)")
    print("─" * 60)
    print("Sorted by avg return (equal-weight, no capital constraints)")

    valid_regime_cols = [
        c for c in [
            "market_vs_200dma",
            "market_6m_quintile",
            "official_vs_200dma",
            "official_6m_quintile",
            "synthetic_vs_200dma",
            "synthetic_6m_quintile",
        ]
        if df[c].notna().sum() >= MIN_COUNT
    ]

    for combo_size in [2, 3]:
        top5 = find_top_combinations(df, valid_regime_cols, top_n=5,
                                     combo_size=combo_size, sort_ascending=False)
        bot5 = find_top_combinations(df, valid_regime_cols, top_n=5,
                                     combo_size=combo_size, sort_ascending=True)
        if top5.empty:
            continue
        label = "pairs" if combo_size == 2 else "triples"
        print(f"\n  TOP 5 {label.upper()} (highest avg return, ≥{MIN_COUNT} trades):")
        print(top5.to_string(index=False))
        if not bot5.empty:
            print(f"\n  BOTTOM 5 {label.upper()} (lowest avg return, ≥{MIN_COUNT} trades):")
            print(bot5.to_string(index=False))

    print("\n" + _sep)
    print("NOTE: Equal-weight, unlimited capital, no transaction costs.")
    print("      Not a real portfolio simulation.")
    print(_sep + "\n")
