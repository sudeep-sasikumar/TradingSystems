"""
CLI entry point for Checkpoint 8 — Regime Tagging and Analysis.

Usage:
    # Step 1: Download index data, compute regime signals, tag all trades
    python 52WeekHigh/run_regime_analysis.py --checkpoint tag

    # Step 2: Run cross-tab analysis and print results
    python 52WeekHigh/run_regime_analysis.py --checkpoint analyze

    # Both steps in sequence
    python 52WeekHigh/run_regime_analysis.py --checkpoint all

    # Force re-download of all index data (clears regime cache)
    python 52WeekHigh/run_regime_analysis.py --checkpoint tag --force-refresh
"""

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # …/52WeekHigh
_ROOT = _HERE.parent                     # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def checkpoint_tag(force_refresh: bool, strategy_version: str) -> None:
    logger.info("=" * 60)
    logger.info(f"CHECKPOINT 8: Tag trades — strategy_version={strategy_version!r}")
    logger.info("=" * 60)
    from analysis.regime_tagger import tag_all_trades
    n = tag_all_trades(force_refresh=force_refresh, strategy_version=strategy_version)
    logger.info(f"Done — {n:,} regime tag records written.")


def checkpoint_analyze(strategy_version: str) -> None:
    logger.info("=" * 60)
    logger.info(f"CHECKPOINT 8: Regime cross-tab analysis — strategy_version={strategy_version!r}")
    logger.info("=" * 60)
    from analysis.regime_analysis import run_analysis
    run_analysis(strategy_version=strategy_version)


def main():
    parser = argparse.ArgumentParser(
        description="Checkpoint 8 — regime tagging and analysis"
    )
    parser.add_argument(
        "--checkpoint",
        choices=["tag", "analyze", "all"],
        required=True,
        help=(
            "tag: download index data + compute regime tags + save to DB. "
            "analyze: run cross-tab analysis on tagged trades. "
            "all: run both in sequence."
        ),
    )
    parser.add_argument(
        "--strategy-version",
        default="52wh_v1_survivorship_10y",
        help=(
            "Which strategy version to tag/analyse. "
            "52wh_v1 = original 2022-present backtest. "
            "52wh_v1_survivorship_10y = survivorship-corrected 2019-present (default)."
        ),
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-download index data and rebuild regime cache (tag step only).",
    )
    args = parser.parse_args()

    if args.checkpoint in ("tag", "all"):
        checkpoint_tag(force_refresh=args.force_refresh, strategy_version=args.strategy_version)
    if args.checkpoint in ("analyze", "all"):
        checkpoint_analyze(strategy_version=args.strategy_version)


if __name__ == "__main__":
    main()
