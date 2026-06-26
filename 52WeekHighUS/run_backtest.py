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
    print(
        "\nBacktest engine not yet implemented (Session 2).\n"
        "Run --checkpoint setup to verify the data layer first."
    )
    sys.exit(0)


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
