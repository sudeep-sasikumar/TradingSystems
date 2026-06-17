#!/usr/bin/env python3
"""
52-Week High Strategy — Backtest Entry Point

Usage (run from E:\\Trading Systems):
    venv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint universe
    venv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint backtest
    venv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint backtest --force-refresh

Checkpoints:
    universe  — Fetch and display Nifty 500 constituent list (Checkpoint 1)
    backtest  — Run full backtest engine (Checkpoint 2, not yet built)
"""
import argparse
import logging
import sys
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
# run_backtest.py lives at: <root>/52WeekHigh/run_backtest.py
_HERE = Path(__file__).resolve().parent          # <root>/52WeekHigh
_ROOT = _HERE.parent                             # <root>  (project root)
sys.path.insert(0, str(_ROOT))   # makes 'shared' importable
sys.path.insert(0, str(_HERE))   # makes 'backtest', 'scanner', 'bot' importable
# ─────────────────────────────────────────────────────────────────────────────

from backtest.universe import fetch_nifty500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_DIVIDER = "=" * 64


def checkpoint_universe(force_refresh: bool = False) -> None:
    logger.info(_DIVIDER)
    logger.info("CHECKPOINT 1 — Nifty 500 Universe Fetch")
    logger.info(_DIVIDER)

    df = fetch_nifty500(force_refresh=force_refresh)

    print(f"\n{_DIVIDER}")
    print(f"  Nifty 500 — {len(df)} stocks loaded")
    print(_DIVIDER)

    print("\n  First 15 stocks:")
    print(df.head(15).to_string(index=False))

    print("\n  Last 5 stocks:")
    print(df.tail(5).to_string(index=False))

    print(f"\n  Sample yfinance tickers: {', '.join(df['ticker'].head(8).tolist())}")
    print(f"  Total unique tickers:   {df['ticker'].nunique()}")

    print(f"\n  {'!'*3} SURVIVORSHIP BIAS NOTE {'!'*3}")
    print(f"  This is the CURRENT Nifty 500 constituent list.")
    print(f"  Stocks added or removed between 2022 and today are not")
    print(f"  perfectly reflected in historical data. Backtest results")
    print(f"  should be interpreted with this limitation in mind.")
    print(f"{_DIVIDER}\n")


def checkpoint_backtest(force_refresh: bool = False) -> None:
    logger.error("Backtest engine (Checkpoint 2) is not yet built.")
    logger.error("Complete Checkpoint 1 first, then implement 52WeekHigh/backtest/engine.py")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="52-Week High Backtest Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--checkpoint",
        choices=["universe", "backtest"],
        default="universe",
        help="Which checkpoint to run (default: universe)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force re-fetch of Nifty 500 list from NSE (bypasses cache)",
    )
    args = parser.parse_args()

    if args.checkpoint == "universe":
        checkpoint_universe(force_refresh=args.force_refresh)
    elif args.checkpoint == "backtest":
        checkpoint_backtest(force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
