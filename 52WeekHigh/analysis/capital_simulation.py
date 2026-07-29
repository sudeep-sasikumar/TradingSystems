"""
capital_simulation.py -- Rs.1,000 flat-allocation simulation.

Purely illustrative: equal Rs.1,000 per trade, no compounding, no
reinvestment, no position sizing, no transaction costs. Each trade is
an independent Rs.1,000 bet whose P&L is return_pct / 100 x 1,000.

Usage:
    python 52WeekHigh/analysis/capital_simulation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

TRADE_SIZE  = 1_000      # Rs. per trade
MIN_SAMPLE  = 30

VERSIONS = {
    "52wh_v1":                  "2022-present  (original, survivorship-biased)",
    "52wh_v1_survivorship_10y": "2019-present  (survivorship-corrected)",
}

QUINTILE_ORDER = [
    "strong_downtrend",
    "moderate_downtrend",
    "flat",
    "moderate_uptrend",
    "strong_uptrend",
]


# ---- Data helpers ------------------------------------------------------------

def load_trades(sv: str) -> pd.DataFrame:
    df = pd.read_sql(
        """
        SELECT t.id, t.ticker, t.entry_date, t.trade_year,
               t.return_pct, t.holding_days,
               r.market_vs_200dma, r.market_6m_quintile,
               r.synthetic_vs_200dma, r.synthetic_6m_quintile
        FROM trades t
        LEFT JOIN trade_regime_tags r ON t.id = r.trade_id
        WHERE t.strategy_version = :sv
          AND t.source = 'backtest'
          AND t.status = 'closed'
          AND t.return_pct IS NOT NULL
        ORDER BY t.entry_date
        """,
        get_engine(),
        params={"sv": sv},
    )
    df["pnl"] = (df["return_pct"] / 100) * TRADE_SIZE
    return df


def compute_score(df: pd.DataFrame) -> pd.Series:
    """
    Additive regime score (missing basket treated as 0, not NaN).
    +1 market below 200-DMA
    +1 market 6M in bottom-2 quintiles
    +1 synthetic basket above 200-DMA  (0 if tag missing)
    -1 market above 200-DMA AND strong_uptrend
    """
    score = pd.Series(0, index=df.index)
    score += (df["market_vs_200dma"] == "below_200dma").astype(int)
    score += df["market_6m_quintile"].isin(["strong_downtrend", "moderate_downtrend"]).astype(int)
    score += (df["synthetic_vs_200dma"].fillna("") == "above_200dma").astype(int)
    penalty = (
        (df["market_vs_200dma"] == "above_200dma")
        & (df["market_6m_quintile"] == "strong_uptrend")
    )
    score -= penalty.astype(int)
    return score


# ---- Formatting --------------------------------------------------------------

def inr(v: float) -> str:
    """Format as Indian Rupees with lakh shorthand above Rs.1,00,000."""
    sign = "-" if v < 0 else ""
    abs_v = abs(v)
    if abs_v >= 1_00_000:
        return f"{sign}Rs.{abs_v / 1_00_000:.2f}L"
    return f"{sign}Rs.{abs_v:,.0f}"


def flag(n: int) -> str:
    return "  [directional only - insufficient sample]" if n < MIN_SAMPLE else ""


def sep(char: str = "-", width: int = 78) -> None:
    print("  " + char * width)


def header(title: str) -> None:
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)


# ---- Core stats --------------------------------------------------------------

def sim_stats(grp: pd.DataFrame) -> dict:
    n = len(grp)
    if n == 0:
        return dict(n=0, deployed=0, pnl=0.0, final=0.0, ret=0.0,
                    wins=0, losses=0, win_pct=0.0, max_gain=0.0, max_loss=0.0)
    deployed = n * TRADE_SIZE
    pnl      = grp["pnl"].sum()
    wins     = int((grp["pnl"] > 0).sum())
    losses   = int((grp["pnl"] < 0).sum())
    return dict(
        n        = n,
        deployed = deployed,
        pnl      = pnl,
        final    = deployed + pnl,
        ret      = pnl / deployed * 100,
        wins     = wins,
        losses   = losses,
        win_pct  = wins / n * 100,
        max_gain = grp["pnl"].max(),
        max_loss = grp["pnl"].min(),
    )


def print_overall(s: dict, indent: str = "  ") -> None:
    print(f"{indent}  Trades                 : {s['n']:,}{flag(s['n'])}")
    print(f"{indent}  Capital deployed       : {inr(s['deployed'])}")
    print(f"{indent}  Total P&L              : {inr(s['pnl'])}")
    print(f"{indent}  Final portfolio value  : {inr(s['final'])}  (deployed + P&L)")
    print(f"{indent}  Return on deployed     : {s['ret']:.1f}%")
    print(f"{indent}  Winners / Losers       : {s['wins']:,} ({s['win_pct']:.1f}%) / {s['losses']:,}")
    print(f"{indent}  Largest single gain    : {inr(s['max_gain'])}")
    print(f"{indent}  Largest single loss    : {inr(s['max_loss'])}")


# ---- Year-by-year table ------------------------------------------------------

def print_yearly(df: pd.DataFrame) -> None:
    rows = []
    cumulative = 0.0
    for yr in sorted(df["trade_year"].dropna().unique()):
        g = df[df["trade_year"] == yr]
        s = sim_stats(g)
        cumulative += s["pnl"]
        rows.append({
            "Year":    int(yr),
            "Trades":  s["n"],
            "Deployed":inr(s["deployed"]),
            "P&L":     inr(s["pnl"]),
            "Ret%":    f"{s['ret']:.1f}%",
            "Win%":    f"{s['win_pct']:.1f}%",
            "Cum P&L": inr(cumulative),
            "Flag":    flag(s["n"]).strip(),
        })

    print(f"\n  {'Year':<6} {'Trades':>6}  {'Deployed':>12}  {'P&L':>12}  "
          f"{'Ret%':>6}  {'Win%':>6}  {'Cum P&L':>14}  Note")
    sep()
    for r in rows:
        print(f"  {r['Year']:<6} {r['Trades']:>6}  {r['Deployed']:>12}  {r['P&L']:>12}  "
              f"{r['Ret%']:>6}  {r['Win%']:>6}  {r['Cum P&L']:>14}  {r['Flag']}")
    sep()


# ---- Quintile table ----------------------------------------------------------

def print_quintile(df: pd.DataFrame) -> None:
    tagged   = df.dropna(subset=["market_6m_quintile"])
    total    = sim_stats(df)

    rows = []
    for q in QUINTILE_ORDER:
        g = tagged[tagged["market_6m_quintile"] == q]
        s = sim_stats(g)
        rows.append((q, s))

    print(f"\n  {'Quintile':<22} {'Trades':>6}  {'Deployed':>12}  {'P&L':>12}  "
          f"{'Ret%':>7}  {'Win%':>6}  Note")
    sep()
    for q, s in rows:
        print(f"  {q:<22} {s['n']:>6}  {inr(s['deployed']):>12}  {inr(s['pnl']):>12}  "
              f"{s['ret']:>6.1f}%  {s['win_pct']:>5.1f}%  {flag(s['n']).strip()}")
    sep()

    # Filter impact: what dropping strong_uptrend would mean
    su     = tagged[tagged["market_6m_quintile"] == "strong_uptrend"]
    non_su = tagged[tagged["market_6m_quintile"] != "strong_uptrend"]
    s_su    = sim_stats(su)
    s_nonsu = sim_stats(non_su)
    s_all   = sim_stats(tagged)

    print(f"\n  FILTER IMPACT: dropping 'strong_uptrend' trades")
    print(f"  All tagged trades    : {s_all['n']:>4,} trades  P&L={inr(s_all['pnl'])}  Ret={s_all['ret']:.1f}%")
    print(f"  Excl. strong_uptrend : {s_nonsu['n']:>4,} trades  P&L={inr(s_nonsu['pnl'])}  Ret={s_nonsu['ret']:.1f}%")
    print(f"  strong_uptrend only  : {s_su['n']:>4,} trades  P&L={inr(s_su['pnl'])}  Ret={s_su['ret']:.1f}%")
    print(f"  Filtering removes:     {inr(s_su['pnl'])} P&L on {inr(s_su['deployed'])} deployed  ({s_su['ret']:.1f}%)")
    print(f"  Avg P&L per skipped trade: {inr(s_su['pnl']/s_su['n']) if s_su['n'] else 'n/a'}")


# ---- Conviction tier table ---------------------------------------------------

def print_conviction(df: pd.DataFrame) -> None:
    df = df.copy()
    df["score"] = compute_score(df)

    avoid     = df[df["market_6m_quintile"] == "strong_uptrend"]
    not_avoid = df[df["market_6m_quintile"] != "strong_uptrend"]
    high_conv = not_avoid[not_avoid["score"] >= 2]
    standard  = not_avoid[not_avoid["score"] < 2]
    unscored_n = df["market_6m_quintile"].isna().sum()

    tiers = [
        ("HIGH CONVICTION  (score >= 2)",       high_conv),
        ("STANDARD         (score 0-1)",        standard),
        ("AVOID            (strong_uptrend)",   avoid),
    ]

    print(f"\n  Note: {unscored_n} trades missing market quintile tag; basket absent = 0 (conservative).\n")
    print(f"  {'Tier':<40} {'Trades':>6}  {'Deployed':>12}  {'P&L':>12}  "
          f"{'Ret%':>7}  {'Win%':>6}  Note")
    sep()
    for tier_label, grp in tiers:
        s = sim_stats(grp)
        print(f"  {tier_label:<40} {s['n']:>6}  {inr(s['deployed']):>12}  {inr(s['pnl']):>12}  "
              f"{s['ret']:>6.1f}%  {s['win_pct']:>5.1f}%  {flag(s['n']).strip()}")
    sep()

    # Hypothetical: if you only took HIGH CONVICTION
    s_hc  = sim_stats(high_conv)
    s_all = sim_stats(df)
    print(f"\n  'HIGH CONVICTION only' vs 'all trades':")
    print(f"  All trades     : {s_all['n']:,}  P&L={inr(s_all['pnl'])}  Ret={s_all['ret']:.1f}%")
    print(f"  HC trades only : {s_hc['n']:,}  P&L={inr(s_hc['pnl'])}  Ret={s_hc['ret']:.1f}%")
    print(f"  Trades skipped : {s_all['n'] - s_hc['n']:,}  P&L foregone = {inr(s_all['pnl'] - s_hc['pnl'])}")

    print(f"\n  Score distribution (non-AVOID trades only):")
    for sc in sorted(not_avoid["score"].unique()):
        g = not_avoid[not_avoid["score"] == sc]
        s = sim_stats(g)
        print(f"    Score {int(sc):+d}: {s['n']:>4,} trades  P&L={inr(s['pnl'])}  "
              f"Ret={s['ret']:.1f}%  Win={s['win_pct']:.1f}%{flag(s['n'])}")


# ---- Cross-dataset comparison ------------------------------------------------

def print_cross_comparison(results: dict) -> None:
    header("CROSS-DATASET COMPARISON (side by side)")

    sv_labels = {
        "52wh_v1":                  "2022-present",
        "52wh_v1_survivorship_10y": "2019-present",
    }

    keys   = ["n", "deployed", "pnl", "final", "ret", "win_pct"]
    labels = {
        "n":       "Closed trades",
        "deployed":"Capital deployed",
        "pnl":     "Total P&L",
        "final":   "Final portfolio value",
        "ret":     "Return on deployed %",
        "win_pct": "Win rate %",
    }

    print(f"\n  {'Metric':<26}", end="")
    for sv in results:
        print(f"  {sv_labels.get(sv, sv):<26}", end="")
    print()
    sep()

    for key in keys:
        print(f"  {labels[key]:<26}", end="")
        for sv, s in results.items():
            v = s[key]
            if key in ("deployed", "pnl", "final"):
                cell = f"{inr(v):<26}"
            elif key in ("ret", "win_pct"):
                cell = f"{v:.1f}%{'':21}"
            else:
                cell = f"{v:,}{'':22}"
            print(f"  {cell}", end="")
        print()
    sep()


# ---- Main -------------------------------------------------------------------

def run() -> None:
    print("\n" + "#" * 80)
    print("  Rs.1,000-PER-TRADE CAPITAL SIMULATION")
    print("  Equal weight  |  No compounding  |  Purely illustrative")
    print("#" * 80)
    print(f"\n  Trade size : Rs.{TRADE_SIZE:,} per trade (flat, not reinvested)")
    print(f"  P&L formula: return_pct / 100 x Rs.1,000\n")

    all_results: dict[str, dict] = {}

    for sv, version_label in VERSIONS.items():
        df = load_trades(sv)
        if df.empty:
            print(f"  WARNING: No data for {sv}")
            continue

        print(f"\n  Loaded {len(df):,} closed trades -- {version_label}")
        overall = sim_stats(df)
        all_results[sv] = overall

        # -- SECTION 1: Overall -----------------------------------------------
        header(f"SECTION 1: OVERALL SIMULATION -- {version_label}")
        print_overall(overall)

        # -- SECTION 2: Year-by-year ------------------------------------------
        header(f"SECTION 2: YEAR-BY-YEAR -- {version_label}")
        print_yearly(df)

        # -- SECTION 3: Regime quintile ---------------------------------------
        header(f"SECTION 3: MARKET 6M QUINTILE BREAKDOWN -- {version_label}")
        print(f"  (market_6m_quintile at trade entry | {df['market_6m_quintile'].notna().sum():,} trades tagged)")
        print_quintile(df)

        # -- SECTION 4: Conviction tier ---------------------------------------
        header(f"SECTION 4: CONVICTION TIER -- {version_label}")
        print("  Tiers:")
        print("    HIGH CONVICTION : regime score >= 2 AND NOT strong_uptrend market")
        print("    STANDARD        : regime score 0 or 1 AND NOT strong_uptrend market")
        print("    AVOID           : market 6M quintile = strong_uptrend")
        print_conviction(df)

    # -- SECTION 5: Cross-comparison ------------------------------------------
    if len(all_results) == 2:
        print_cross_comparison(all_results)

    # -- SECTION 6: Honest caveats --------------------------------------------
    header("SECTION 6: WHAT THIS SIMULATION DOES NOT TELL YOU")
    print("""
  1. NO COMPOUNDING. Each Rs.1,000 is a fresh independent bet. In a real
     portfolio, early profits (or losses) would change the capital base for
     later trades. With compounding, a run of winning trades would deploy
     more capital to subsequent trades -- amplifying both upside and downside
     compared to what this simulation shows.

  2. NO POSITION SIZING. Every trade gets exactly Rs.1,000 regardless of
     conviction, volatility, sector concentration, or portfolio weight. A real
     portfolio would size positions based on risk parameters, not a flat Rs.

  3. NO TRANSACTION COSTS. Each equity delivery trade in India incurs:
     - STT: 0.1% on buy + sell (Rs.2 on a Rs.1,000 round-trip)
     - Brokerage + SEBI fees: ~Rs.0.50-Rs.5 per trade
     - Slippage on entry/exit: typically 0.1-0.5% for liquid Nifty 500 stocks
     On a Rs.1,000 trade, these costs add up to roughly Rs.5-Rs.15 per trade
     (~0.5-1.5% friction). At 1,000+ trades, this erodes Rs.5,000-Rs.15,000
     from the total P&L shown above -- material but not catastrophic at this
     trade size.

  4. NO CAPITAL CONSTRAINT. This simulation assumes you have enough cash to
     fund all concurrent open positions simultaneously. In practice, trades
     overlap heavily -- some 52-week-high breakouts run for 200+ days. At any
     given point you could have 20-50 concurrent open positions, requiring
     Rs.20,000-Rs.50,000 of deployed capital, not the total Rs.10-17 lakh
     deployed across all trades over years.

  5. SURVIVORSHIP BIAS (52wh_v1 only). The 2022-present dataset uses the
     CURRENT Nifty 500 list projected backward. Stocks that were delisted,
     merged, or dropped from the index are excluded -- their worst-case trades
     are missing. The 2019-present dataset corrects for this partially but
     still has gaps in reconstitution data (Sep 2020, Mar/Sep 2021, Sep 2023,
     Sep 2024).

  6. WHAT THE Rs. FIGURES ARE GOOD FOR. The absolute Rs. amounts let you see
     the magnitude of regime differences in money terms, not just percentages.
     A 10% difference in return rates looks very different when you see it
     expressed as Rs.X vs Rs.Y across the same number of trades. That
     comparison is valid and useful -- just do not interpret the total portfolio
     values as what you would actually have earned.
""")


if __name__ == "__main__":
    run()
