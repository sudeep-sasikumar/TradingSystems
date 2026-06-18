"""
CLI entry point for the survivorship-corrected historic backtest.

Usage:
    # Step 1 — build membership table (requires PDFs in data/reconstitution_pdfs/)
    python 52WeekHigh/run_historic_backtest.py --checkpoint membership

    # Step 2 — run the extended backtest
    python 52WeekHigh/run_historic_backtest.py --checkpoint backtest

    # Step 1 dry-run (parse PDFs, show what would be written, do NOT save)
    python 52WeekHigh/run_historic_backtest.py --checkpoint membership --dry-run

    # Force re-download of all price data
    python 52WeekHigh/run_historic_backtest.py --checkpoint backtest --force-refresh
"""

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent   # …/52WeekHigh
_ROOT = _HERE.parent                      # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def checkpoint_membership(dry_run: bool) -> None:
    logger.info("=" * 60)
    logger.info("CHECKPOINT: Build index_membership table")
    logger.info("=" * 60)
    from historic_universe.build_membership import main as build_main
    import sys as _sys
    # Pass --dry-run if requested
    old_argv = _sys.argv
    _sys.argv = ["build_membership.py"] + (["--dry-run"] if dry_run else [])
    try:
        build_main()
    finally:
        _sys.argv = old_argv


def checkpoint_backtest(force_refresh: bool) -> None:
    logger.info("=" * 60)
    logger.info("CHECKPOINT: Survivorship-corrected historic backtest")
    logger.info("=" * 60)
    from historic_universe.historic_engine import run_historic_backtest

    trades_df, combined, yearly_table, equity_curve, failed, open_summary = \
        run_historic_backtest(force_refresh=force_refresh)

    if trades_df.empty:
        logger.error("No trades — check membership table and price data.")
        sys.exit(1)

    closed = trades_df[trades_df["status"] == "closed"]
    logger.info("\n" + "=" * 60)
    logger.info("RESULTS SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Closed trades      : {combined.get('total_trades')}")
    logger.info(f"  Win rate           : {combined.get('win_rate_pct')} %")
    logger.info(f"  Avg return/trade   : {combined.get('avg_return_pct')} %")
    logger.info(f"  Median return      : {combined.get('median_return_pct')} %")
    logger.info(f"  Avg holding days   : {combined.get('avg_holding_days')}")
    logger.info(f"  Best trade         : {combined.get('best_trade_pct')} %")
    logger.info(f"  Worst trade        : {combined.get('worst_trade_pct')} %")
    logger.info(f"  Gross return sum   : {combined.get('gross_return_pct')} %")
    logger.info(f"  Open trades        : {combined.get('open_trades_count')}")
    if "avg_unrealized_pct" in combined:
        logger.info(f"  Avg unrealized     : {combined.get('avg_unrealized_pct')} %")
    logger.info("")
    if not yearly_table.empty:
        logger.info("Year-by-year (closed trades opened in that year):")
        logger.info(yearly_table.to_string(index=False))
    logger.info("")
    logger.info(
        "NOTE: Illustrative, equal-weight, no capital constraints — not a real portfolio simulation."
    )
    logger.info(f"Failed tickers: {len(failed)}")


def main():
    parser = argparse.ArgumentParser(
        description="Survivorship-corrected 52-week high historic backtest"
    )
    parser.add_argument(
        "--checkpoint",
        choices=["membership", "backtest"],
        required=True,
        help="membership: build index_membership table from baseline + PDFs. "
             "backtest: run the extended backtest using that table.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(membership checkpoint only) Parse PDFs without writing to DB",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="(backtest checkpoint only) Ignore price cache and re-download",
    )
    args = parser.parse_args()

    if args.checkpoint == "membership":
        checkpoint_membership(dry_run=args.dry_run)
    elif args.checkpoint == "backtest":
        checkpoint_backtest(force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
