#!/usr/bin/env python3
"""
52-Week High Strategy — Backtest Entry Point

Usage (run from E:\\Trading Systems):
    venv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint universe
    venv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint backtest
    venv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint backtest --force-refresh

Checkpoints:
    universe  — Fetch and display Nifty 500 constituent list (Checkpoint 1)
    backtest  — Full backtest: download prices, simulate, stats, save to DB (Checkpoint 2)
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent          # <root>/52WeekHigh
_ROOT = _HERE.parent                             # project root
sys.path.insert(0, str(_ROOT))   # makes 'shared' importable
sys.path.insert(0, str(_HERE))   # makes 'backtest', 'scanner', 'bot' importable
# ─────────────────────────────────────────────────────────────────────────────

from backtest.universe import fetch_nifty500
from backtest.engine import (
    STRATEGY_VERSION, run_full_backtest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_DIV  = "=" * 72
_DIV2 = "-" * 72


def checkpoint_universe(force_refresh: bool = False) -> None:
    logger.info(_DIV)
    logger.info("CHECKPOINT 1 — Nifty 500 Universe Fetch")
    logger.info(_DIV)

    df = fetch_nifty500(force_refresh=force_refresh)

    print(f"\n{_DIV}")
    print(f"  Nifty 500 — {len(df)} stocks loaded")
    print(_DIV)
    print("\n  First 15 stocks:")
    print(df.head(15).to_string(index=False))
    print("\n  Last 5 stocks:")
    print(df.tail(5).to_string(index=False))
    print(f"\n  Sample yfinance tickers: {', '.join(df['ticker'].head(8).tolist())}")
    print(f"  Total unique tickers:    {df['ticker'].nunique()}")
    print(f"\n  [!] SURVIVORSHIP BIAS: This is the CURRENT Nifty 500 list.")
    print(f"      Stocks added/removed between 2022 and today are not reflected.")
    print(f"{_DIV}\n")


def checkpoint_backtest(force_refresh: bool = False) -> None:
    logger.info(_DIV)
    logger.info("CHECKPOINT 2 — Full Backtest")
    logger.info(_DIV)

    # ── 1. Universe ────────────────────────────────────────────────────────────
    universe_df = fetch_nifty500()
    logger.info(f"Universe: {len(universe_df)} tickers.")

    # ── 2. Run backtest ────────────────────────────────────────────────────────
    trades_df, combined, yearly_table, equity_curve, failed, open_summary = \
        run_full_backtest(universe_df, force_refresh=force_refresh)

    if trades_df.empty:
        logger.error("No trades produced. Check logs above for download errors.")
        sys.exit(1)

    closed = trades_df[trades_df["status"] == "closed"]

    # ── 3. Print results ───────────────────────────────────────────────────────
    today_str = date.today().strftime("%Y-%m-%d")

    print(f"\n{_DIV}")
    print(f"  BACKTEST RESULTS — 52-Week High Strategy  ({STRATEGY_VERSION})")
    print(_DIV)
    print(f"  Period:     2022-01-01 to {today_str}")
    print(f"  Universe:   {len(universe_df)} Nifty 500 stocks (current list — survivorship bias applies)")
    print(f"  Prices:     yfinance adjusted close (auto_adjust=True, splits+dividends corrected)")
    print(f"  Sizing:     Equal-weight, UNLIMITED capital — position cap NOT applied in backtest")
    print(f"  Costs:      None modelled (no brokerage, STT, slippage)")
    print(_DIV)

    # ── Combined stats ─────────────────────────────────────────────────────────
    print(f"\n{'COMBINED STATS (closed trades only)':^72}")
    print(_DIV2)
    s = combined
    print(f"  Total closed trades:       {s.get('total_trades', 'N/A'):>10}")
    print(f"  Win rate:                  {s.get('win_rate_pct', 0):>9.1f}%")
    print(f"  Avg return per trade:      {s.get('avg_return_pct', 0):>+10.2f}%")
    print(f"  Median return per trade:   {s.get('median_return_pct', 0):>+10.2f}%")
    print(f"  Avg holding period:        {s.get('avg_holding_days', 0):>9.1f} days")
    print(f"  Best single trade:         {s.get('best_trade_pct', 0):>+10.2f}%")
    print(f"  Worst single trade:        {s.get('worst_trade_pct', 0):>+10.2f}%")
    print(f"  Gross cumulative return:   {s.get('gross_return_pct', 0):>+10.2f}%")
    print(f"    [= sum of all trade returns, equal-weight per trade, NOT compounded]")
    print(f"  Trades still open (EOB):   {open_summary['count']:>10}")
    if combined.get("avg_unrealized_pct") is not None:
        print(f"  Avg unrealized return:     {combined['avg_unrealized_pct']:>+10.2f}%  (open trades, mark-to-market)")
    print(_DIV2)

    # ── Year-by-year ───────────────────────────────────────────────────────────
    if not yearly_table.empty:
        print(f"\n{'YEAR-BY-YEAR BREAKDOWN (by year trade was opened)':^72}")
        print(_DIV2)

        cols = ["year", "total_trades", "win_rate_pct", "avg_return_pct",
                "median_return_pct", "avg_holding_days", "best_trade_pct",
                "worst_trade_pct", "gross_return_pct"]
        headers = ["Year", "Trades", "Win%", "Avg Ret%", "Med Ret%",
                   "Avg Days", "Best%", "Worst%", "Gross%"]

        print(f"  {'Year':<14} {'Trades':>6} {'Win%':>6} {'Avg Ret':>8} "
              f"{'Median':>8} {'AvgDays':>8} {'Best':>8} {'Worst':>8} {'Gross':>9}")
        print(f"  {'-'*13} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*9}")

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

    # ── Best and worst trades ──────────────────────────────────────────────────
    if not closed.empty:
        print(f"\n{'TOP 5 WINNERS':^72}")
        print(_DIV2)
        top5 = closed.nlargest(5, "return_pct")[
            ["ticker", "company_name", "entry_date", "exit_date",
             "entry_price", "exit_price", "return_pct", "holding_days"]
        ]
        print(top5.to_string(index=False))

        print(f"\n{'TOP 5 LOSERS':^72}")
        print(_DIV2)
        bot5 = closed.nsmallest(5, "return_pct")[
            ["ticker", "company_name", "entry_date", "exit_date",
             "entry_price", "exit_price", "return_pct", "holding_days"]
        ]
        print(bot5.to_string(index=False))

        print(f"\n{'SAMPLE CLOSED TRADES (random 15 — for manual spot-check)':^72}")
        print(_DIV2)
        sample = closed.sample(min(15, len(closed)), random_state=42)[
            ["ticker", "company_name", "entry_date", "exit_date",
             "entry_price", "exit_price", "return_pct", "holding_days"]
        ].sort_values("entry_date")
        print(sample.to_string(index=False))

    # ── Open trades ────────────────────────────────────────────────────────────
    if open_summary["count"] > 0:
        open_t = trades_df[trades_df["status"] == "open"][
            ["ticker", "company_name", "entry_date", "entry_price",
             "highest_price_reached", "trailing_stop"]
        ].sort_values("entry_date")
        print(f"\n{'CURRENTLY OPEN TRADES (at end of backtest period)':^72}")
        print(_DIV2)
        print(f"  These {open_summary['count']} trades were not stopped out — still running as of {today_str}.")
        print(f"  Their returns are unrealized and excluded from closed-trade stats above.")
        print(open_t.to_string(index=False))

    # ── Failed tickers ─────────────────────────────────────────────────────────
    if failed:
        print(f"\n[!] {len(failed)} tickers had NO price data and are excluded:")
        print(f"    {', '.join(failed)}")

    print(f"\n{_DIV}")
    print(f"  Results saved to: {_ROOT / 'data' / 'trading.db'}")
    print(f"  strategy_version: {STRATEGY_VERSION}")
    print(_DIV)
    print(f"\n  ⚠  IMPORTANT CAVEATS:")
    print(f"  1. Survivorship bias: current Nifty 500 list only.")
    print(f"  2. Adjusted prices used (splits/dividends corrected).")
    print(f"  3. No transaction costs, slippage, or taxes modelled.")
    print(f"  4. Equal-weight, unlimited capital — not a real portfolio.")
    print(f"  5. Single historical window (2022-2026) — not predictive.")
    print(f"  These results show what the rules WOULD HAVE produced under")
    print(f"  these assumptions. They are not achievable real-world returns.")
    print(f"{_DIV}\n")


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_int(row, col):
    v = row.get(col)
    return str(int(v)) if v is not None else "—"

def _fmt_pct(row, col):
    v = row.get(col)
    return f"{v:.1f}%" if v is not None else "—"

def _fmt_ret(row, col):
    v = row.get(col)
    return f"{v:+.1f}%" if v is not None else "—"

def _fmt_days(row, col):
    v = row.get(col)
    return f"{v:.0f}d" if v is not None else "—"


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="52-Week High Backtest Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--checkpoint",
        choices=["universe", "backtest"],
        default="universe",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-download prices from yfinance (bypasses cache)",
    )
    args = parser.parse_args()

    if args.checkpoint == "universe":
        checkpoint_universe(force_refresh=args.force_refresh)
    elif args.checkpoint == "backtest":
        checkpoint_backtest(force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
