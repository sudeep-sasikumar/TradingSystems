#!/usr/bin/env python3
"""
InsiderSwing — CLI entry point.

Build order matters.  The data layer must exist before anything can be scored,
and scores must exist before a backtest means anything:

    1. universe     inspect the point-in-time universe and CIK coverage
    2. ingest       pull Form 4 filings from SEC EDGAR (the long one)
    3. reclassify   re-run the noise filter over the stored corpus
    4. score        compute conviction scores
    5. backtest     three-arm simulation + saved report
    6. sweep        parameter stability grid
    7. scan         live daily scan (what the Docker service runs)

Examples
--------
    python InsiderSwing/run_insider.py --checkpoint universe
    python InsiderSwing/run_insider.py --checkpoint ingest --start 2016-01-01
    python InsiderSwing/run_insider.py --checkpoint score
    python InsiderSwing/run_insider.py --checkpoint backtest --label baseline
    python InsiderSwing/run_insider.py --checkpoint sweep
    python InsiderSwing/run_insider.py --checkpoint scan --once
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # InsiderSwing/
_ROOT = _HERE.parent                             # project root
for _p in (str(_ROOT), str(_HERE), str(_HERE / "sources"), str(_HERE / "backtest")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402


def _setup_logging(level: str = "INFO") -> None:
    # Windows consoles default to cp1252, which cannot encode the arrows and
    # currency symbols used in log lines and report previews. Force UTF-8 on
    # both streams rather than stripping the characters from every message.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)-18s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    return datetime.strptime(s, "%Y-%m-%d").date()


# ──────────────────────────────────────────────────────────────────────────────
#  Checkpoints
# ──────────────────────────────────────────────────────────────────────────────

def cp_universe(args) -> None:
    import universe as univ

    start = _parse_date(args.start, date(2016, 1, 1))
    end = _parse_date(args.end, date.today())

    members = univ.full_universe(start, end)
    print(f"\nUniverse over {start} → {end}")
    print(f"  tickers (any time in window): {members['ticker'].nunique()}")
    if not members.empty:
        pit = members[members["point_in_time"].astype(bool)]
        extra = members[~members["point_in_time"].astype(bool)]
        print(f"  point-in-time (sp500_membership): {pit['ticker'].nunique()}")
        print(f"  extra CSV (NOT survivorship corrected): {extra['ticker'].nunique()}")

    today_set = univ.universe_on_date(end)
    print(f"  in universe on {end}: {len(today_set)}")

    tickers = sorted(set(members["ticker"])) if not members.empty else []
    cik_map = univ.ticker_cik_map(set(tickers))
    print(f"  SEC CIK resolved: {len(cik_map)}/{len(tickers)} "
          f"({100.0 * len(cik_map) / max(len(tickers), 1):.1f}%)")
    print("  (unresolved names are usually delisted — EDGAR per-issuer discovery "
          "cannot reach them; the daily-index path can.)\n")


def cp_ingest(args) -> None:
    import ingest
    import universe as univ

    conf = cfg.DEFAULT_CONFIG
    start = _parse_date(args.start, datetime.strptime(conf.backtest_start, "%Y-%m-%d").date())
    end = _parse_date(args.end, date.today())

    members = univ.full_universe(start, end)
    tickers = sorted(set(members["ticker"])) if not members.empty else []
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    if not tickers:
        raise SystemExit(
            "Universe is empty. Run the S&P 500 membership checkpoint first:\n"
            "  python SP500/run_sp500_backtest.py --checkpoint membership\n"
            "or pass --tickers AAPL,MSFT"
        )

    cik_map = univ.ticker_cik_map(set(tickers))
    ciks = set(cik_map.values())

    print(f"\nIngesting Form 4 filings, {start} → {end}")
    print(f"  universe: {len(tickers)} tickers | {len(ciks)} resolvable CIKs")
    print(f"  source: {args.source or conf.preferred_source}")
    print("  NOTE: a multi-year first run fetches hundreds of thousands of documents "
          "from SEC EDGAR at ~8 req/s. Everything is cached to disk, so a re-run is free.\n")

    def _progress(phase: str, n: int, total: int) -> None:
        print(f"    {phase}: {n}/{total}", end="\r", flush=True)

    res = ingest.ingest_range(
        start=start, end=end,
        tickers=set(tickers) if args.filter_tickers else None,
        ciks=ciks or None,
        source_name=args.source,
        mode="backfill",
        progress_cb=_progress,
    )
    print("\n" + json.dumps(res, indent=2))
    print("\nCorpus summary:\n" + json.dumps(ingest.ingest_summary(), indent=2))


def cp_reclassify(args) -> None:
    import filters

    counts = filters.reclassify_all(cfg.DEFAULT_CONFIG.exclude_unknown_10b5_1)
    if not counts:
        print("No transactions stored yet — run --checkpoint ingest first.")
        return
    total = sum(counts.values())
    print(f"\nRe-classified {total:,} transaction lines:\n")
    for key, n in counts.items():
        print(f"  {key:<45} {n:>9,}  ({100.0 * n / total:5.1f}%)")
    kept = counts.get("open_market_buy", 0) + counts.get("open_market_sale", 0)
    print(f"\n  kept as open-market: {kept:,} ({100.0 * kept / total:.1f}%) — "
          "the rest are awards, vesting, exercises, gifts and plan trades, which carry "
          "no conviction information.\n")


def cp_score(args) -> None:
    import scoring
    import universe as univ

    conf = cfg.DEFAULT_CONFIG
    start = _parse_date(args.start, datetime.strptime(conf.backtest_start, "%Y-%m-%d").date())
    end = _parse_date(args.end, date.today())

    tickers = None
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    scores = scoring.compute_scores(start, end, config=conf, tickers=tickers,
                                    use_earnings=not args.no_earnings)
    if scores.empty:
        print("No scores produced. Has the ingest step run?")
        return

    n = scoring.save_scores(scores, conf)
    qual = scoring.qualifying_signals(scores, conf)

    print(f"\nScored {len(scores):,} (ticker, filing-date) events; saved {n:,} rows.")
    print(f"  threshold {conf.conviction_threshold:g} → {len(qual):,} qualifying signals")
    if not qual.empty:
        print(f"  blocked by cluster-selling filter: {int((qual['status'] == 'blocked').sum()):,}")
        print("\nTop 15 by score:\n")
        cols = ["as_of_date", "ticker", "score", "cluster_count", "role_bucket_top",
                "size_ratio_max", "novelty_count", "earnings_proximity_flag", "sell_pressure_flag"]
        print(qual.sort_values("score", ascending=False)[cols].head(15).to_string(index=False))
    print()


def cp_backtest(args) -> None:
    import engine
    import metrics
    import report
    import walkforward

    conf = cfg.DEFAULT_CONFIG
    start = _parse_date(args.start, datetime.strptime(conf.backtest_start, "%Y-%m-%d").date())
    end = _parse_date(args.end, date.today())
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    arms = tuple(a.strip() for a in args.arms.split(",")) if args.arms else engine.ALL_ARMS

    print(f"\nBacktest {start} → {end} | arms: {', '.join(arms)}\n")

    result = engine.run_backtest(
        config=conf, label=args.label, start=start, end=end,
        arms=arms, tickers=tickers, use_earnings=not args.no_earnings,
    )

    summary = metrics.summarize(result, conf)
    wf = walkforward.walk_forward(result, conf)
    paths = report.write_report(summary, walk_forward=wf, conf=conf, label=args.label)
    run_id = engine.save_run(result, metrics=summary, report_path=paths["markdown"])

    print("\n" + "=" * 78)
    for line in report.build_verdict(summary, conf):
        print(f"  • {line}")
    print("=" * 78)
    print(f"\nRun #{run_id} saved.  Report: {paths['markdown']}\n")


def cp_sweep(args) -> None:
    import metrics
    import report
    import walkforward

    conf = cfg.DEFAULT_CONFIG
    start = _parse_date(args.start, datetime.strptime(conf.backtest_start, "%Y-%m-%d").date())
    end = _parse_date(args.end, date.today())
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    print("\nParameter stability sweep — this runs one full simulation per cell.\n")
    sweep = walkforward.parameter_sweep(
        base_config=conf, start=start, end=end, tickers=tickers,
        use_earnings=not args.no_earnings,
    )

    print("\n" + sweep.grid.to_string(index=False))
    print("\n" + "=" * 78)
    print(sweep.stability.get("verdict", "not assessed"))
    print("=" * 78 + "\n")

    paths = report.write_report(
        {"label": f"{args.label}_sweep", "start": start.isoformat(), "end": end.isoformat(),
         "params": conf.to_dict(), "param_key": conf.param_key(), "price_coverage": {},
         "notes": [], "arms": {}, "segments": {}, "comparisons": {},
         "statistical_checks": {}},
        sweep={"grid": sweep.grid.to_dict(orient="records"), "stability": sweep.stability},
        conf=conf, label=f"{args.label}_sweep",
    )
    print(f"Sweep report: {paths['markdown']}\n")


def cp_scan(args) -> None:
    import scanner

    if args.once:
        res = scanner.run_scan(trigger_type="manual")
        print(json.dumps(res, indent=2, default=str))
    else:
        scanner.main()


# ──────────────────────────────────────────────────────────────────────────────

_CHECKPOINTS = {
    "universe": cp_universe,
    "ingest": cp_ingest,
    "reclassify": cp_reclassify,
    "score": cp_score,
    "backtest": cp_backtest,
    "sweep": cp_sweep,
    "scan": cp_scan,
}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="InsiderSwing — insider-trade cluster swing system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--checkpoint", required=True, choices=sorted(_CHECKPOINTS))
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--tickers", help="comma-separated ticker override")
    ap.add_argument("--filter-tickers", action="store_true",
                    help="ingest: keep only filings whose ticker is in the universe")
    ap.add_argument("--source", choices=["edgar", "fmp", "auto"],
                    help="ingest: data source (default from INSIDER_SOURCE)")
    ap.add_argument("--arms", help="backtest: comma-separated arms to run")
    ap.add_argument("--label", default="insider_swing", help="run label")
    ap.add_argument("--no-earnings", action="store_true",
                    help="skip the earnings-calendar lookup (faster; disables the confound flag)")
    ap.add_argument("--once", action="store_true", help="scan: run a single pass and exit")
    ap.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = ap.parse_args()

    _setup_logging(args.log_level)
    cfg.ensure_dirs()
    _CHECKPOINTS[args.checkpoint](args)


if __name__ == "__main__":
    main()
