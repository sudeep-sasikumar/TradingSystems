#!/usr/bin/env python3
"""
52WeekHighUS — US S&P 500 Breakout System CLI

Checkpoints:
  universe   — fetch S&P 500 constituent list from Wikipedia, cache locally
  setup      — initialize DB and verify data layer (universe + indicator test)
  backtest   — run 3-version backtest comparison (Session 2 — not yet implemented)

Usage:
    venv\\Scripts\\python.exe 52WeekHighUS\\run_backtest.py --checkpoint universe
    venv\\Scripts\\python.exe 52WeekHighUS\\run_backtest.py --checkpoint setup
"""
import argparse
import logging
import sys
from pathlib import Path

# -- Path setup -----------------------------------------------------------------
_HERE = Path(__file__).resolve().parent   # 52WeekHighUS/
_ROOT = _HERE.parent                      # project root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("52whu")


def cmd_universe() -> None:
    from universe import fetch_universe
    df = fetch_universe(force_refresh=True)
    print(f"\n{'-'*60}")
    print(f"S&P 500 Universe: {len(df)} constituents")
    print(f"{'-'*60}")
    print(df[["ticker", "company_name", "gics_sector", "sector_etf"]].head(10).to_string(index=False))
    print(f"... ({len(df) - 10} more rows)")
    print(f"{'-'*60}")
    sectors = df["gics_sector"].value_counts()
    print("\nSector breakdown:")
    print(sectors.to_string())
    unmapped = df[df["sector_etf"].isna()]
    if not unmapped.empty:
        print(f"\nWARNING: {len(unmapped)} tickers with no sector ETF mapping:")
        print(unmapped[["ticker", "gics_sector"]].to_string(index=False))
    else:
        print("\nAll tickers mapped to sector ETFs. OK")


def cmd_setup() -> None:
    from universe import fetch_universe, get_tickers
    from db import get_engine
    from models import Base

    print("\n-- Step 1: Universe ------------------------------------------")
    df = fetch_universe()
    tickers = get_tickers()
    print(f"  {len(tickers)} S&P 500 tickers loaded")

    print("\n-- Step 2: DB init --------------------------------------------")
    engine = get_engine()
    table_names = [t for t in Base.metadata.tables.keys()]
    print(f"  Tables created: {table_names}")

    print("\n-- Step 3: Smoke-test data loader (first 3 tickers) ----------")
    from data_loader import fetch_all_tickers, compute_indicators
    sample = tickers[:3]
    result = fetch_all_tickers(sample, use_cache=False)
    for ticker, tdf in result.data.items():
        last = tdf.iloc[-1]
        print(f"  {ticker}: {len(tdf)} rows | last Close={last['Close']:.2f} | ATR14={last['ATR14']:.2f}")
    if result.failed:
        print(f"  WARNING: {len(result.failed)} failed: {result.failed}")
    print(f"  data_end_date: {result.data_end_date}")

    print("\n✓ Setup complete. Run --checkpoint backtest (Session 2) for full backtest.")


def cmd_backtest() -> None:
    """
    Run the three-version backtest comparison (Part I).

    Downloads/caches all S&P 500 + SPY + sector ETF price data, then runs
    Versions A (buggy baseline), B (corrected stop), and C (full spec) over
    the same historical period and prints a side-by-side comparison.
    """
    import os
    from datetime import date

    from data_loader import (
        fetch_all_tickers, fetch_index_data, compute_indicators,
        LOOKBACK_START,
    )
    from universe import fetch_universe, get_tickers, INDEX_TICKERS
    from signal_logic import SignalConfig
    from backtest.engine import run_backtest, print_report, trades_to_df, BACKTEST_START

    account_size       = float(os.getenv("SP500_US_ACCOUNT_SIZE", "100000"))
    risk_pct           = float(os.getenv("SP500_US_RISK_PERCENT", "1.0"))
    max_capital        = float(os.getenv("SP500_US_MAX_CAPITAL_PER_TRADE", "10000"))
    config = SignalConfig(
        account_size=account_size,
        risk_pct=risk_pct,
        max_capital_per_trade=max_capital,
    )

    print("\n-- Step 1: Universe -----------------------------------------------")
    universe_df = fetch_universe()
    tickers = get_tickers()
    print(f"  {len(tickers)} S&P 500 tickers")

    print("\n-- Step 2: Download S&P 500 price data ----------------------------")
    print(f"  Fetching from {LOOKBACK_START} (may take a few minutes the first time) ...")
    fetch_result = fetch_all_tickers(tickers, start=LOOKBACK_START, use_cache=True)
    ticker_data  = fetch_result.data
    data_end     = fetch_result.data_end_date
    if fetch_result.failed:
        print(f"  WARNING: {len(fetch_result.failed)} tickers failed: {fetch_result.failed[:10]}")
    print(f"  {len(ticker_data)} tickers downloaded. data_end_date = {data_end}")

    print("\n-- Step 3: Download SPY + sector ETF data -------------------------")
    index_result = fetch_index_data(INDEX_TICKERS, start=LOOKBACK_START, use_cache=True)
    spy_df = index_result.data.get("SPY")
    if spy_df is None or spy_df.empty:
        print("  ERROR: SPY data not available. Cannot run backtest.")
        sys.exit(1)
    sector_dfs = {
        etf: df for etf, df in index_result.data.items()
        if etf not in ("SPY", "QQQ") and not df.empty
    }
    print(f"  SPY: {len(spy_df)} bars | {len(sector_dfs)} sector ETFs loaded")

    print("\n-- Step 4: Running backtest (3 versions) --------------------------")
    print(f"  Backtest window: {BACKTEST_START} to {data_end}")
    print("  (This may take several minutes for 500 tickers × 3 years)")

    results = run_backtest(
        ticker_data=ticker_data,
        spy_df=spy_df,
        sector_dfs=sector_dfs,
        universe_df=universe_df,
        config=config,
        backtest_start=BACKTEST_START,
        backtest_end=data_end,
    )

    print_report(results)

    # Save trade logs to CSV
    output_dir = Path(__file__).resolve().parent.parent / "data"
    output_dir.mkdir(exist_ok=True)
    for version, res in results.items():
        if res.trades:
            df_out = trades_to_df(res.trades)
            out_path = output_dir / f"backtest_v{version}_trades.csv"
            df_out.to_csv(out_path, index=False)
            print(f"  Saved {len(res.trades)} trades for Version {version} → {out_path}")

    print("\nDone. Research estimate only — see survivorship bias note above.")


def main() -> None:
    parser = argparse.ArgumentParser(description="52WeekHighUS — US S&P 500 Breakout System")
    parser.add_argument(
        "--checkpoint",
        choices=["universe", "setup", "backtest"],
        required=True,
        help="Checkpoint to run",
    )
    args = parser.parse_args()

    if args.checkpoint == "universe":
        cmd_universe()
    elif args.checkpoint == "setup":
        cmd_setup()
    elif args.checkpoint == "backtest":
        cmd_backtest()


if __name__ == "__main__":
    main()
