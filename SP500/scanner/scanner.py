#!/usr/bin/env python3
"""
S&P 500 52-Week High — Daily EOD Scanner (CP-S5 + CP-S6)

ENTRY SIGNAL:
    Close-based only — no provisional phase.
    Signal fires when today's CLOSE strictly exceeds max(daily CLOSE, prior
    252 trading days).  Signal type is always 'eod_confirmed'.

TRAILING STOP:
    Checked at same EOD scan.
    Exit: daily CLOSE <= trailing_stop  (= max_close_since_entry × 0.80).
    Stop only moves up, never down.

RE-ENTRY SUPPRESSION:
    OPEN sp500_52wh_v1 positions  → suppress new signal for same ticker
    PENDING sp500_52wh_v1 signals → suppress duplicate while pending
    REJECTED / EXPIRED            → ticker eligible for fresh signal

POSITION CAP:
    SP500_MAX_CONCURRENT_POSITIONS in .env (default: 20).
    Cap-reached signals still sent with [CAP REACHED] note.

SCHEDULE (APScheduler):
    21:30 UTC Mon–Fri  (= 5:30 PM EDT | 4:30 PM EST — ~90 min after US close)

TELEGRAM:
    Signal records written to DB (strategy_version='sp500_52wh_v1').
    The Nifty bot (bot.py) picks them up and sends with [S&P500] / $ formatting.
    Accept/Reject callbacks handled by same bot.

CLI:
    python SP500/scanner/scanner.py            # start scheduler
    python SP500/scanner/scanner.py --run-now  # immediate scan (bypass schedule)
"""

import logging
import os
import sys
from contextlib import suppress
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

_HERE = Path(__file__).resolve().parent          # SP500/scanner/
_ROOT = _HERE.parent.parent                      # project root
sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from shared.db import get_engine, session_scope
from shared.models import Signal, Trade
from SP500.analysis.sp500_conviction import get_sp500_conviction

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sp500_scanner")

# ── Strategy constants ────────────────────────────────────────────────────────
STRATEGY_VERSION  = "sp500_52wh_v1"
ROLLING_WINDOW    = 252
TRAILING_STOP_PCT = 0.80
CHUNK_SIZE        = 50
LOOKBACK_START    = "2024-01-01"    # 620+ trading days to date — enough for 252-day window
CACHE_TTL_H       = 23

SP500_MAX_POSITIONS = int(os.getenv("SP500_MAX_CONCURRENT_POSITIONS",
                          os.getenv("MAX_CONCURRENT_POSITIONS", "20")))

# ── Cache directory (separate from backtest to avoid overwriting long history) ─
_CACHE_DIR = _ROOT / "data" / "cache" / "prices_sp500_live"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════════════════
#  UNIVERSE
# ════════════════════════════════════════════════════════════════════════════

def load_sp500_universe() -> pd.DataFrame:
    """
    Load current S&P 500 members from sp500_membership table
    (removed_date IS NULL = still in index).
    Returns DataFrame with columns [ticker, company_name].
    """
    engine = get_engine()
    df = pd.read_sql(
        """
        SELECT ticker,
               MAX(company_name) AS company_name
        FROM sp500_membership
        WHERE removed_date IS NULL
        GROUP BY ticker
        ORDER BY ticker
        """,
        engine,
    )
    if df.empty:
        logger.error(
            "sp500_membership has no current members. "
            "Run: python SP500/run_sp500_backtest.py --checkpoint membership"
        )
    else:
        logger.info("Universe: %d current S&P 500 members", len(df))
    return df


# ════════════════════════════════════════════════════════════════════════════
#  PRICE CACHE
# ════════════════════════════════════════════════════════════════════════════

def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{ticker.replace('.', '_')}.parquet"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_h = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600
    return age_h < CACHE_TTL_H


def _save_close(ticker: str, s: pd.Series) -> None:
    with suppress(Exception):
        s.rename("Close").to_frame().to_parquet(_cache_path(ticker))


def _load_close(ticker: str) -> Optional[pd.Series]:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    with suppress(Exception):
        df = pd.read_parquet(p)
        s  = df["Close"].copy()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.sort_index().dropna()
    return None


# ════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD
# ════════════════════════════════════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _dl_chunk(chunk: list) -> pd.DataFrame:
    end = (date.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return yf.download(
        tickers=chunk, start=LOOKBACK_START, end=end,
        auto_adjust=True, progress=False, threads=True,
    )


def _extract_close(raw: pd.DataFrame, tickers: list) -> dict[str, pd.Series]:
    result: dict[str, pd.Series] = {}
    if raw is None or raw.empty:
        return result

    def _clean(s: pd.Series) -> Optional[pd.Series]:
        s = s.dropna()
        if s.empty:
            return None
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.sort_index()

    cols = raw.columns
    if not isinstance(cols, pd.MultiIndex):
        if len(tickers) == 1 and "Close" in cols:
            s = _clean(raw["Close"])
            if s is not None:
                result[tickers[0]] = s
        return result

    l0 = set(cols.get_level_values(0))
    l1 = set(cols.get_level_values(1))

    if "Close" in l0:
        block = raw["Close"]
        if isinstance(block, pd.Series):
            s = _clean(block)
            if s is not None and tickers:
                result[tickers[0]] = s
        else:
            for t in tickers:
                if t in block.columns:
                    s = _clean(block[t])
                    if s is not None:
                        result[t] = s
    elif "Close" in l1:
        for t in tickers:
            if (t, "Close") in cols:
                s = _clean(raw[(t, "Close")])
                if s is not None:
                    result[t] = s
    return result


def refresh_cache(tickers: list) -> tuple[int, int]:
    """Download and cache close prices for stale tickers. Returns (refreshed, failed)."""
    stale = [t for t in tickers if not _is_fresh(_cache_path(t))]
    if not stale:
        logger.info("Price cache: all %d tickers fresh (< %dh)", len(tickers), CACHE_TTL_H)
        return 0, 0

    logger.info("Price cache: refreshing %d/%d tickers from %s ...",
                len(stale), len(tickers), LOOKBACK_START)
    refreshed = failed = 0
    chunks = [stale[i : i + CHUNK_SIZE] for i in range(0, len(stale), CHUNK_SIZE)]

    for i, chunk in enumerate(chunks, 1):
        try:
            raw = _dl_chunk(chunk)
            closes = _extract_close(raw, chunk)
            for t in chunk:
                s = closes.get(t)
                if s is not None and not s.empty:
                    _save_close(t, s)
                    refreshed += 1
                else:
                    logger.warning("No close data for %s (chunk %d/%d)", t, i, len(chunks))
                    failed += 1
        except Exception as exc:
            logger.error("Download chunk %d/%d failed: %s", i, len(chunks), exc)
            failed += len(chunk)

    logger.info("Price cache refresh done: %d refreshed, %d failed", refreshed, failed)
    return refreshed, failed


# ════════════════════════════════════════════════════════════════════════════
#  SIGNAL COMPUTATION
# ════════════════════════════════════════════════════════════════════════════

def compute_signals(tickers: list) -> dict[str, dict]:
    """
    For each ticker, compute 252-day benchmark and check if today's close exceeds it.
    Returns {ticker: {today_close, benchmark_252d}} for tickers that fired a signal.
    """
    hits: dict[str, dict] = {}

    for t in tickers:
        s = _load_close(t)
        if s is None or len(s) < ROLLING_WINDOW + 2:
            continue
        try:
            bm_series = s.shift(1).rolling(ROLLING_WINDOW).max()
            bm_series = bm_series.dropna()
            if bm_series.empty:
                continue
            today_close = float(s.iloc[-1])
            benchmark   = float(bm_series.iloc[-1])
            if today_close > benchmark and not np.isnan(benchmark):
                hits[t] = {"today_close": today_close, "benchmark_252d": benchmark}
        except Exception:
            pass

    return hits


# ════════════════════════════════════════════════════════════════════════════
#  SUPPRESSION / POSITION STATE
# ════════════════════════════════════════════════════════════════════════════

def load_suppressed() -> set[str]:
    """
    Tickers suppressed from new S&P 500 signals:
      - Open sp500_52wh_v1 live positions
      - Pending sp500_52wh_v1 signals
    REJECTED and EXPIRED do NOT suppress.
    """
    engine = get_engine()
    suppressed: set[str] = set()
    with suppress(Exception):
        df = pd.read_sql(
            "SELECT ticker FROM trades "
            "WHERE source='live' AND status='open' AND strategy_version=:sv",
            engine, params={"sv": STRATEGY_VERSION},
        )
        suppressed.update(df["ticker"].tolist())
    with suppress(Exception):
        df = pd.read_sql(
            "SELECT ticker FROM signals WHERE status='pending' AND strategy_version=:sv",
            engine, params={"sv": STRATEGY_VERSION},
        )
        suppressed.update(df["ticker"].tolist())
    return suppressed


def count_open_live() -> int:
    engine = get_engine()
    with suppress(Exception):
        df = pd.read_sql(
            "SELECT COUNT(*) AS n FROM trades "
            "WHERE source='live' AND status='open' AND strategy_version=:sv",
            engine, params={"sv": STRATEGY_VERSION},
        )
        return int(df["n"].iloc[0])
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  TRAILING STOP CHECK
# ════════════════════════════════════════════════════════════════════════════

def run_trailing_stop_check(close_map: dict[str, float]) -> int:
    """
    Check open sp500_52wh_v1 live trades.
    Close trade if today_close <= trailing_stop.
    Update highest_price and trailing_stop if new high.
    Returns count of exits triggered.
    """
    engine = get_engine()
    today_str = date.today().isoformat()
    exits = 0

    try:
        open_trades = pd.read_sql(
            "SELECT * FROM trades "
            "WHERE source='live' AND status='open' AND strategy_version=:sv",
            engine, params={"sv": STRATEGY_VERSION},
        )
    except Exception as exc:
        logger.error("Trailing stop check: cannot load open trades: %s", exc)
        return 0

    for _, row in open_trades.iterrows():
        ticker   = row["ticker"]
        trade_id = int(row["id"])
        t_high   = float(row["highest_price_reached"])
        t_stop   = float(row["trailing_stop"])
        entry_p  = float(row["entry_price"])
        tc       = close_map.get(ticker)

        if tc is None:
            logger.warning("Trailing stop: no price for %s (trade %d)", ticker, trade_id)
            continue

        new_high = max(t_high, tc)
        new_stop = round(new_high * TRAILING_STOP_PCT, 2)

        if tc <= new_stop:
            ret_pct = round((tc - entry_p) / entry_p * 100, 4)
            hold_d  = (date.today() - pd.Timestamp(row["entry_date"]).date()).days
            try:
                with session_scope() as sess:
                    t = sess.query(Trade).filter_by(id=trade_id).first()
                    if t and t.status == "open":
                        t.status       = "closed"
                        t.exit_date    = today_str
                        t.exit_price   = tc
                        t.exit_reason  = "trailing_stop"
                        t.return_pct   = ret_pct
                        t.holding_days = hold_d
                        t.updated_at   = datetime.now(timezone.utc).isoformat()
                logger.info("[S&P500 EXIT] %s trade=%d | close=%.2f stop=%.2f ret=%.2f%%",
                            ticker, trade_id, tc, new_stop, ret_pct)
                exits += 1
            except Exception as exc:
                logger.error("Trailing stop: failed to close trade %d: %s", trade_id, exc)

        elif new_high > t_high:
            try:
                with session_scope() as sess:
                    t = sess.query(Trade).filter_by(id=trade_id).first()
                    if t and t.status == "open":
                        t.highest_price_reached = round(new_high, 2)
                        t.trailing_stop         = new_stop
                        t.updated_at            = datetime.now(timezone.utc).isoformat()
                logger.debug("[S&P500] %s stop %.2f -> %.2f (new high %.2f)",
                             ticker, t_stop, new_stop, new_high)
            except Exception as exc:
                logger.error("Trailing stop: failed to update trade %d: %s", trade_id, exc)

    return exits


# ════════════════════════════════════════════════════════════════════════════
#  CORE EOD SCAN
# ════════════════════════════════════════════════════════════════════════════

def run_eod_scan(universe_df: pd.DataFrame) -> None:
    """
    Daily S&P 500 EOD scan:
      1. Refresh price cache (download today's close for stale tickers)
      2. Compute 252-day close-based signals
      3. Write Signal records (type='eod_confirmed', strategy_version='sp500_52wh_v1')
         → Nifty bot picks up pending signals and sends Telegram alerts
      4. Check trailing stops on open S&P 500 live trades
    """
    tickers   = universe_df["ticker"].tolist()
    name_map  = dict(zip(universe_df["ticker"], universe_df["company_name"]))
    today_str = date.today().isoformat()
    scan_ts   = datetime.now(timezone.utc).isoformat()

    logger.info("=== S&P500 EOD SCAN %s UTC | %d tickers ===", scan_ts[:19], len(tickers))

    # ── 1. Refresh cache ───────────────────────────────────────────────────
    refresh_cache(tickers)

    # ── 2. Compute signals ─────────────────────────────────────────────────
    hits       = compute_signals(tickers)
    suppressed = load_suppressed()
    open_count = count_open_live()
    cap        = SP500_MAX_POSITIONS

    # CP-S6: conviction tier — same for all signals fired on the same day
    conviction    = get_sp500_conviction(today_str)
    conv_tier     = conviction["tier"]
    conv_score    = conviction["regime_score"]
    logger.info(
        "Market regime: gspc=%s vix=%s score=%d → conviction=%s",
        conviction["gspc_regime"], conviction["vix_tier"], conv_score, conv_tier,
    )

    logger.info("Tickers above 252d benchmark: %d | Suppressed: %d",
                len(hits), len(suppressed & set(hits)))

    # Build a close_map from cached data for trailing stop check
    close_map: dict[str, float] = {}
    for t in tickers:
        s = _load_close(t)
        if s is not None and not s.empty:
            close_map[t] = float(s.iloc[-1])

    # ── 3. Write signals ───────────────────────────────────────────────────
    n_written = n_suppressed = 0

    for ticker, data in hits.items():
        today_close = data["today_close"]
        benchmark   = data["benchmark_252d"]
        company     = name_map.get(ticker, "")

        if ticker in suppressed:
            n_suppressed += 1
            logger.debug("Suppressed: %s", ticker)
            continue

        try:
            with session_scope() as sess:
                sig = Signal(
                    ticker                  = ticker,
                    company_name            = company,
                    signal_price            = round(today_close, 2),
                    signal_date             = today_str,
                    scan_timestamp          = scan_ts,
                    signal_type             = "eod_confirmed",
                    benchmark_252d          = round(benchmark, 2),
                    status                  = "pending",
                    positions_open_at_signal= open_count,
                    cap_at_signal           = cap,
                    strategy_version        = STRATEGY_VERSION,
                    conviction_tier         = conv_tier,
                    regime_score            = conv_score,
                )
                sess.add(sig)
                sess.flush()
                sig_id = sig.id

            cap_note = f"  [CAP REACHED - {open_count}/{cap}]" if open_count >= cap else ""
            logger.info(
                "[S&P500 SIGNAL id=%d] %s (%s) | close=$%.2f | bm=$%.2f | +%.1f%%%s",
                sig_id, ticker, company, today_close, benchmark,
                (today_close / benchmark - 1) * 100, cap_note,
            )
            n_written += 1

        except Exception as exc:
            logger.error("Failed to write signal for %s: %s", ticker, exc)

    # ── 4. Trailing stop check ─────────────────────────────────────────────
    exits = run_trailing_stop_check(close_map)

    logger.info(
        "=== S&P500 EOD END | signals above bm: %d | suppressed: %d | new: %d | exits: %d ===",
        len(hits), n_suppressed, n_written, exits,
    )


# ════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

def is_us_trading_day() -> bool:
    """True Mon–Fri UTC (approximate — does not check NYSE holiday calendar)."""
    return datetime.now(timezone.utc).weekday() < 5


def _guarded_eod(universe_df: pd.DataFrame) -> None:
    if not is_us_trading_day():
        logger.info("S&P500 scan skipped — not a US trading day (weekday=%d)",
                    datetime.now(timezone.utc).weekday())
        return
    run_eod_scan(universe_df)


def start_scheduler() -> None:
    """
    Start the blocking APScheduler.
    Job: daily EOD scan at 21:30 UTC Mon–Fri
      = 5:30 PM EDT (summer, UTC-4) | 4:30 PM EST (winter, UTC-5)
    """
    universe_df = load_sp500_universe()
    if universe_df.empty:
        logger.error("Cannot start — empty universe. Run SP500 membership checkpoint first.")
        return

    scheduler = BlockingScheduler(timezone=pytz.utc)
    scheduler.add_job(
        _guarded_eod,
        trigger=CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone=pytz.utc),
        args=[universe_df],
        id="sp500_eod_scan",
        name="S&P 500 52WH Daily EOD Scanner",
        misfire_grace_time=1800,
        coalesce=True,
    )

    logger.info("S&P 500 Scanner started. Job: 21:30 UTC Mon–Fri.")
    logger.info("Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("S&P 500 scanner stopped.")
        scheduler.shutdown(wait=False)


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="S&P 500 52-Week High Daily EOD Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scanner.py            # start scheduler (runs continuously)\n"
            "  python scanner.py --run-now  # immediate scan, bypasses schedule\n"
        ),
    )
    parser.add_argument(
        "--run-now", action="store_true",
        help="Execute one EOD scan immediately, then exit.",
    )
    args = parser.parse_args()

    universe_df = load_sp500_universe()
    if universe_df.empty:
        raise SystemExit("Universe is empty — run membership checkpoint first.")

    if args.run_now:
        logger.info("--run-now: immediate S&P 500 EOD scan")
        run_eod_scan(universe_df)
    else:
        start_scheduler()
