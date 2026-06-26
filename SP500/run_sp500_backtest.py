#!/usr/bin/env python3
"""
S&P 500 52-Week High Strategy — Backtest Entry Point

Usage (run from E:\\Trading Systems):
    venv\\Scripts\\python.exe SP500\\run_sp500_backtest.py --checkpoint membership
    venv\\Scripts\\python.exe SP500\\run_sp500_backtest.py --checkpoint backtest
    venv\\Scripts\\python.exe SP500\\run_sp500_backtest.py --checkpoint backtest --force-refresh
    venv\\Scripts\\python.exe SP500\\run_sp500_backtest.py --checkpoint regime

Checkpoints:
    membership  -- CP-S2: download fja05680 constituent CSV, parse intervals,
                   populate sp500_membership table, print coverage report
    backtest    -- CP-S3: download prices for all historical S&P 500 tickers
                   (from 2005-01-01), simulate strategy, print year-by-year stats,
                   save to trading.db as strategy_version='sp500_52wh_v1'
    regime      -- CP-S4: download ^GSPC + ^VIX daily closes, compute 200-DMA regime
                   and VIX tier signals, populate sp500_market_regime table
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent   # SP500/
_ROOT = _HERE.parent                      # project root
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from backtest.universe import (
    build_sp500_membership,
    get_backtest_tickers,
    load_all_membership_periods,
    COVERAGE_START,
)
from backtest.engine import (
    STRATEGY_VERSION, LOOKBACK_START,
    run_full_backtest,
)
from backtest.regime import build_regime_table

# freshness_tagger lives in 52WeekHigh/analysis/ — add 52WeekHigh to path
_52WH = _HERE.parent / "52WeekHigh"
if str(_52WH) not in sys.path:
    sys.path.insert(0, str(_52WH))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_DIV  = "=" * 72
_DIV2 = "-" * 72


# ════════════════════════════════════════════════════════════════════════════
#  CP-S2 — MEMBERSHIP
# ════════════════════════════════════════════════════════════════════════════

def checkpoint_membership(force_refresh: bool = False) -> None:
    logger.info(_DIV)
    logger.info("CP-S2 -- S&P 500 Historical Membership Ingestion")
    logger.info(_DIV)

    intervals_df = build_sp500_membership(force_refresh=force_refresh)

    today_str = str(date.today())

    print(f"\n{_DIV}")
    print(f"  CP-S2 COMPLETE -- S&P 500 Membership Ingested")
    print(_DIV)
    print(f"  Total unique tickers in history:  {len(intervals_df)}")
    print(f"  Coverage start:                   {COVERAGE_START}")
    print(f"  Source: fja05680/sp500 (GitHub, Wikipedia-sourced change dates)")
    print()
    print(f"  Baseline tickers (in 1996 snapshot, exact add-date unknown):")
    baseline = intervals_df[intervals_df["date_quality"] == "baseline"]
    confirmed = intervals_df[intervals_df["date_quality"] == "confirmed"]
    print(f"    {len(baseline)} baseline  |  {len(confirmed)} confirmed add-date")
    print()
    print(f"  Still current members (removed_date IS NULL):  "
          f"{intervals_df['removed_date'].isna().sum()}")
    print(f"  Tickers with removal date:                     "
          f"{intervals_df['removed_date'].notna().sum()}")
    print(_DIV)
    print(f"\n  [!] Proceed to CP-S3 (backtest) once you have confirmed the")
    print(f"      coverage report above looks reasonable.")
    print(f"      Run: python SP500\\run_sp500_backtest.py --checkpoint backtest\n")


# ════════════════════════════════════════════════════════════════════════════
#  CP-S3 — BACKTEST
# ════════════════════════════════════════════════════════════════════════════

def checkpoint_backtest(force_refresh: bool = False) -> None:
    logger.info(_DIV)
    logger.info("CP-S3 -- S&P 500 52-Week High Backtest")
    logger.info(_DIV)

    # ── 1. Universe from DB (auto-populate if empty) ───────────────────────
    tickers = get_backtest_tickers()
    if not tickers:
        logger.info(
            "sp500_membership table is empty — running membership ingestion first ..."
        )
        checkpoint_membership(force_refresh=force_refresh)
        tickers = get_backtest_tickers()
    if not tickers:
        logger.error(
            "sp500_membership is still empty after ingestion attempt. "
            "Check network connectivity to GitHub and retry."
        )
        sys.exit(1)

    membership_map = load_all_membership_periods()
    logger.info(
        "Universe: %d unique tickers to download (ever in S&P 500 from %s to today).",
        len(tickers), COVERAGE_START,
    )

    # ── 2. Run backtest ────────────────────────────────────────────────────
    trades_df, combined, yearly_table, equity_curve, failed, open_summary = \
        run_full_backtest(tickers, membership_map, force_refresh=force_refresh)

    if trades_df.empty:
        logger.error("No trades produced. Check logs above for download errors.")
        sys.exit(1)

    closed   = trades_df[trades_df["status"] == "closed"]
    delisted = closed[closed["exit_reason"] == "delisted"]
    artifacts = trades_df[trades_df.get("artifact_flag", False) == True] \
        if "artifact_flag" in trades_df.columns else trades_df.iloc[0:0]

    today_str = date.today().strftime("%Y-%m-%d")

    # ── 3. Print results ───────────────────────────────────────────────────
    print(f"\n{_DIV}")
    print(f"  BACKTEST RESULTS -- S&P 500 52-Week High Strategy  ({STRATEGY_VERSION})")
    print(_DIV)
    print(f"  Period:      {COVERAGE_START} to {today_str}")
    print(f"  Universe:    {len(tickers)} unique tickers (all S&P 500 members from 2006-present)")
    print(f"  Membership:  Time-varying (entry only when ticker was an active S&P 500 member)")
    print(f"  Prices:      yfinance adjusted close (auto_adjust=True, splits+dividends corrected)")
    print(f"  Lookback:    Price data from {LOOKBACK_START} (252-day warm-up year)")
    print(f"  Sizing:      Equal-weight, UNLIMITED capital -- position cap NOT applied")
    print(f"  Costs:       None modelled (no brokerage, commissions, slippage, taxes)")
    print(_DIV)
    print(f"  Illustrative, equal-weight, no capital constraints -- not a real portfolio simulation")
    print(_DIV)

    # ── Combined stats ─────────────────────────────────────────────────────
    print(f"\n{'COMBINED STATS (closed trades only)':^72}")
    print(_DIV2)
    s = combined
    print(f"  Total closed trades:          {s.get('total_trades', 'N/A'):>10}")
    print(f"  Win rate:                     {s.get('win_rate_pct', 0):>9.1f}%")
    print(f"  Avg return per trade:         {s.get('avg_return_pct', 0):>+10.2f}%")
    print(f"  Median return per trade:      {s.get('median_return_pct', 0):>+10.2f}%")
    print(f"  Avg holding period:           {s.get('avg_holding_days', 0):>9.1f} days")
    print(f"  Best single trade:            {s.get('best_trade_pct', 0):>+10.2f}%")
    print(f"  Worst single trade:           {s.get('worst_trade_pct', 0):>+10.2f}%")
    print(f"  Gross cumulative return:      {s.get('gross_return_pct', 0):>+10.2f}%")
    print(f"    [= sum of all trade returns, equal-weight per trade, NOT compounded]")
    print(f"  Trades still open (EOB):      {open_summary['count']:>10}")
    if combined.get("avg_unrealized_pct") is not None:
        print(f"  Avg unrealized (open trades): {combined['avg_unrealized_pct']:>+10.2f}%")
    print(f"  Delisted exits:               {len(delisted):>10}  (acquired/bankrupt mid-trade)")
    print(_DIV2)

    # ── Year-by-year ───────────────────────────────────────────────────────
    if not yearly_table.empty:
        print(f"\n{'YEAR-BY-YEAR BREAKDOWN (by year trade was opened)':^72}")
        print(_DIV2)
        print(
            f"  {'Year':<14} {'Trades':>6} {'Win%':>6} {'Avg Ret':>8} "
            f"{'Median':>8} {'AvgDays':>8} {'Best':>8} {'Worst':>8} {'Gross':>9}"
        )
        print(
            f"  {'-'*13} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*9}"
        )
        for _, row in yearly_table.iterrows():
            yr = str(row.get("year", "?"))
            print(
                f"  {yr:<14} "
                f"{_fmt_int(row, 'total_trades'):>6} "
                f"{_fmt_pct(row, 'win_rate_pct'):>6} "
                f"{_fmt_ret(row, 'avg_return_pct'):>8} "
                f"{_fmt_ret(row, 'median_return_pct'):>8} "
                f"{_fmt_days(row, 'avg_holding_days'):>8} "
                f"{_fmt_ret(row, 'best_trade_pct'):>8} "
                f"{_fmt_ret(row, 'worst_trade_pct'):>8} "
                f"{_fmt_ret(row, 'gross_return_pct'):>9}"
            )
        print(_DIV2)

    # ── Top/bottom trades ──────────────────────────────────────────────────
    if not closed.empty:
        print(f"\n{'TOP 5 WINNERS':^72}")
        print(_DIV2)
        top5 = closed.nlargest(5, "return_pct")[
            ["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
             "return_pct", "holding_days", "exit_reason"]
        ]
        print(top5.to_string(index=False))

        print(f"\n{'TOP 5 LOSERS':^72}")
        print(_DIV2)
        bot5 = closed.nsmallest(5, "return_pct")[
            ["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
             "return_pct", "holding_days", "exit_reason"]
        ]
        print(bot5.to_string(index=False))

        print(f"\n{'SAMPLE CLOSED TRADES (random 15 -- for manual spot-check)':^72}")
        print(_DIV2)
        sample = closed.sample(min(15, len(closed)), random_state=42)[
            ["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
             "return_pct", "holding_days", "exit_reason"]
        ].sort_values("entry_date")
        print(sample.to_string(index=False))

    # ── Delisted exits ─────────────────────────────────────────────────────
    if not delisted.empty:
        print(f"\n{'DELISTED / ACQUIRED EXITS ({} total)'.format(len(delisted)):^72}")
        print(_DIV2)
        print(f"  These trades exited at last available price (stock acquired/delisted).")
        delist_show = delisted[
            ["ticker", "entry_date", "exit_date", "entry_price", "exit_price", "return_pct"]
        ].sort_values("exit_date")
        print(delist_show.to_string(index=False))

    # ── Artifact warning ───────────────────────────────────────────────────
    if "artifact_flag" in trades_df.columns:
        flagged = trades_df[trades_df["artifact_flag"] == True]
        if not flagged.empty:
            print(f"\n{'[!] ARTIFACT FLAGS (>25% single-day move during trade)':^72}")
            print(_DIV2)
            print(f"  {len(flagged)} trades had a day with >25% absolute price move.")
            print(f"  These may contain data errors (splits not fully adjusted, etc.).")
            print(f"  Verify these manually before drawing conclusions:")
            art_show = flagged[
                ["ticker", "entry_date", "exit_date", "return_pct",
                 "max_daily_move_pct", "exit_reason"]
            ].sort_values("max_daily_move_pct", ascending=False)
            print(art_show.head(20).to_string(index=False))
            if len(flagged) > 20:
                print(f"  ... and {len(flagged) - 20} more (see trades_df for full list)")

    # ── Open trades ────────────────────────────────────────────────────────
    if open_summary["count"] > 0:
        open_t = trades_df[trades_df["status"] == "open"][
            ["ticker", "entry_date", "entry_price", "highest_price_reached",
             "trailing_stop", "unrealized_return_pct"]
        ].sort_values("entry_date")
        print(f"\n{'CURRENTLY OPEN TRADES':^72}")
        print(_DIV2)
        print(f"  {open_summary['count']} trades not stopped out as of {today_str}.")
        print(f"  Unrealized returns excluded from closed-trade stats above.")
        print(open_t.to_string(index=False))

    # ── Failed tickers ─────────────────────────────────────────────────────
    if failed:
        print(f"\n[!] {len(failed)} tickers had NO price data (excluded from backtest).")
        print(f"  Most of these are normal: delisted stocks with no Yahoo Finance data.")
        if len(failed) <= 30:
            print(f"  {', '.join(sorted(failed))}")
        else:
            print(f"  First 30: {', '.join(sorted(failed)[:30])} ...")

    print(f"\n{_DIV}")
    print(f"  Results saved to: {_ROOT / 'data' / 'trading.db'}")
    print(f"  strategy_version: {STRATEGY_VERSION}")
    print(_DIV)
    print(f"\n  [!] IMPORTANT CAVEATS:")
    print(f"  1. Time-varying membership used — entry only when ticker was in S&P 500.")
    print(f"  2. Adjusted prices (splits+dividends corrected via yfinance auto_adjust).")
    print(f"  3. No transaction costs, slippage, taxes, or SEC filing delays modelled.")
    print(f"  4. Equal-weight, unlimited capital — not a real portfolio.")
    print(f"  5. yfinance data quality for pre-2010 tickers may be imperfect.")
    print(f"  6. Delisted stocks exit at last available price (optimistic vs. halt/gap).")
    print(f"{_DIV}\n")


# ════════════════════════════════════════════════════════════════════════════
#  CP-S4 — REGIME ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

def checkpoint_regime() -> None:
    logger.info(_DIV)
    logger.info("CP-S4 -- S&P 500 Regime Analysis (^GSPC 200-DMA + ^VIX tiers)")
    logger.info(_DIV)

    regime_df = build_regime_table()

    bull_days = (regime_df["gspc_regime"] == "bull").sum()
    bear_days = (regime_df["gspc_regime"] == "bear").sum()
    total     = len(regime_df)
    calm_days     = (regime_df["vix_tier"] == "calm").sum()
    elevated_days = (regime_df["vix_tier"] == "elevated").sum()
    stressed_days = (regime_df["vix_tier"] == "stressed").sum()

    print(f"\n{_DIV}")
    print(f"  CP-S4 COMPLETE -- S&P 500 Market Regime Table")
    print(_DIV)
    print(f"  Trading days covered: {total:,}  "
          f"({regime_df.index[0].date()} to {regime_df.index[-1].date()})")
    print(f"")
    print(f"  ^GSPC 200-DMA Regime:")
    print(f"    Bull (close > 200-DMA):  {bull_days:4d} days  ({bull_days/total*100:.1f}%)")
    print(f"    Bear (close <= 200-DMA): {bear_days:4d} days  ({bear_days/total*100:.1f}%)")
    print(f"")
    print(f"  ^VIX Tier:")
    print(f"    Calm     (VIX < 20):    {calm_days:4d} days  ({calm_days/total*100:.1f}%)")
    print(f"    Elevated (20 <= VIX<25):{elevated_days:4d} days  ({elevated_days/total*100:.1f}%)")
    print(f"    Stressed (VIX >= 25):   {stressed_days:4d} days  ({stressed_days/total*100:.1f}%)")
    print(_DIV)
    print(f"\n  Regime rows saved to: sp500_market_regime table in trading.db")
    print(f"  Open the S&P 500 dashboard tab > Regime Analysis to see the breakdown.")
    print(f"{_DIV}\n")


# ════════════════════════════════════════════════════════════════════════════
#  FRESHNESS FACTOR
# ════════════════════════════════════════════════════════════════════════════

def checkpoint_freshness() -> None:
    logger.info(_DIV)
    logger.info("FRESHNESS -- S&P 500 52-Week High Freshness Factor")
    logger.info(_DIV)

    from analysis.freshness_tagger import tag_freshness_sp500
    summary = tag_freshness_sp500()
    logger.info("Done: %s", summary)

    total    = summary.get("total", 0)
    gap      = summary.get("gap_computed", 0)
    foh      = summary.get("first_observed_high", 0)
    insuf    = summary.get("insufficient_history", 0)

    print(f"\n{_DIV}")
    print(f"  FRESHNESS COMPLETE -- sp500_52wh_v1")
    print(_DIV)
    print(f"  Total trades processed:   {total:>8,}")
    print(f"  gap_computed:             {gap:>8,}  ({gap/total*100:.1f}%)" if total else "")
    print(f"  first_observed_high:      {foh:>8,}  ({foh/total*100:.1f}%)" if total else "")
    print(f"  insufficient_history:     {insuf:>8,}  ({insuf/total*100:.1f}%)" if total else "")
    print(f"")
    print(f"  Results written to: sp500_trade_freshness table in trading.db")
    print(f"  View in: S&P 500 dashboard → Freshness Factor tab")
    print(_DIV)
    print()

    if foh > 0:
        print(
            "  [!] first_observed_high caveat:\n"
            "      S&P 500 price cache starts 2005-01-01 (~20 years of lookback).\n"
            "      This category genuinely means no prior 52wk high was found\n"
            "      in the 20-year window — a reliable long-base breakout signal.\n"
        )


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_int(row, col):
    v = row.get(col)
    return str(int(v)) if v is not None else "--"

def _fmt_pct(row, col):
    v = row.get(col)
    return f"{v:.1f}%" if v is not None else "--"

def _fmt_ret(row, col):
    v = row.get(col)
    return f"{v:+.1f}%" if v is not None else "--"

def _fmt_days(row, col):
    v = row.get(col)
    return f"{v:.0f}d" if v is not None else "--"


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="S&P 500 52-Week High Backtest Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--checkpoint",
        choices=["membership", "backtest", "regime", "freshness"],
        default="membership",
        help=(
            "membership=CP-S2 (constituent history); backtest=CP-S3; "
            "regime=CP-S4 (^GSPC + ^VIX); freshness=compute freshness factor "
            "(run after backtest)"
        ),
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-download constituent CSV and/or price data (bypasses cache)",
    )
    args = parser.parse_args()

    if args.checkpoint == "membership":
        checkpoint_membership(force_refresh=args.force_refresh)
    elif args.checkpoint == "backtest":
        checkpoint_backtest(force_refresh=args.force_refresh)
    elif args.checkpoint == "regime":
        checkpoint_regime()
    elif args.checkpoint == "freshness":
        checkpoint_freshness()


if __name__ == "__main__":
    main()
