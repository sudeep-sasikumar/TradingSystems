#!/usr/bin/env python3
"""
52-Week High — Hourly Intraday Scanner (Checkpoint 4)

ENTRY SIGNAL ASYMMETRY (confirmed design decision — do not change without user):
    BACKTEST  uses close-based 252-day benchmark (daily CLOSE > max close, prior 252 days).
    LIVE SCAN uses intraday-high-based 252-day benchmark:
        Provisional alert fires when current intraday price >
        max(daily HIGH, prior 252 trading days).
    At EOD, close-confirmation pass runs:
        If stock also CLOSED above close-based 252-day level → "eod_confirmed"
        Otherwise → "provisional_unconfirmed" (follow-up note sent via Telegram at CP5)
    Intraday price triggers the ALERT; day's close triggers the CONFIRMATION.

TRAILING STOP (live positions only):
    Checked once per day at EOD, NOT intraday.
    Exit: daily CLOSE <= trailing_stop  (= max_close_since_entry × 0.80)
    Stop only moves up, never down.

RE-ENTRY SUPPRESSION:
    OPEN positions  → suppress new signals for same ticker
    PENDING signals → suppress duplicate alerts while pending
    REJECTED        → ticker eligible for fresh future signal
    EXPIRED         → ticker eligible for fresh future signal

POSITION CAP:
    When MAX_CONCURRENT_POSITIONS reached: signal STILL fires with [CAP REACHED] note.
    Never silently suppress a signal.

Runs via APScheduler:
    Hourly scan:  9:00–10:00 UTC Mon–Fri (market hours 09:15–15:30 IST guarded internally)
    EOD pass:     10:05 UTC Mon–Fri (= 15:35 IST)

Run standalone:
    venv\\Scripts\\python.exe 52WeekHigh\\scanner\\scanner.py           # start scheduler
    venv\\Scripts\\python.exe 52WeekHigh\\scanner\\scanner.py --run-now  # single test scan
    venv\\Scripts\\python.exe 52WeekHigh\\scanner\\scanner.py --eod-now  # single EOD pass
"""

import logging
import os
import sys
from contextlib import suppress
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from tenacity import (
    retry, stop_after_attempt, wait_exponential, before_sleep_log
)

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent          # .../52WeekHigh/scanner
_ROOT = _HERE.parent.parent                      # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(_ROOT / ".env")

from shared.db import session_scope, get_engine
from shared.models import Signal, Trade
from backtest.universe import fetch_nifty500
from analysis.conviction import get_signal_conviction

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scanner")

# ── Strategy constants ────────────────────────────────────────────────────────
ROLLING_WINDOW    = 252          # trading days ("52-week high")
TRAILING_STOP_PCT = 0.80         # stop = max_close_since_entry × this
CHUNK_SIZE        = 50           # tickers per yfinance batch
STRATEGY_VERSION  = "52wh_v1"
LOOKBACK_START    = "2021-01-01" # daily data start (252-day warm-up before 2022)
CACHE_TTL_H       = 23           # hours before daily cache considered stale

MAX_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "20"))

# ── Timezone / market hours ───────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")

_MARKET_OPEN  = time(9,  15)    # 09:15 IST
_MARKET_CLOSE = time(15, 30)    # 15:30 IST

# ── Cache directories ─────────────────────────────────────────────────────────
_HIGH_DIR  = _ROOT / "data" / "cache" / "highs"    # daily HIGH series per ticker
_CLOSE_DIR = _ROOT / "data" / "cache" / "prices"   # daily CLOSE series (shared with backtest)
_HIGH_DIR.mkdir(parents=True, exist_ok=True)
_CLOSE_DIR.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════════════════
#  MARKET HOURS
# ════════════════════════════════════════════════════════════════════════════

def _now_ist() -> datetime:
    return datetime.now(IST)


def is_market_hours() -> bool:
    """True during NSE trading hours (09:15–15:30 IST). Weekday check excluded here."""
    t = _now_ist().time()
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


def is_trading_day() -> bool:
    """True Mon–Fri (does not check NSE holiday calendar)."""
    return _now_ist().weekday() < 5


# ════════════════════════════════════════════════════════════════════════════
#  DAILY CACHE — HIGH and CLOSE series per ticker
# ════════════════════════════════════════════════════════════════════════════

def _high_path(ticker: str) -> Path:
    return _HIGH_DIR / f"{ticker.replace('.', '_')}.parquet"


def _close_path(ticker: str) -> Path:
    return _CLOSE_DIR / f"{ticker.replace('.', '_')}.parquet"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_h = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600
    return age_h < CACHE_TTL_H


def _save_series(path: Path, s: pd.Series, col: str) -> None:
    with suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        s.rename(col).to_frame().to_parquet(path)


def _load_series(path: Path, col: str) -> Optional[pd.Series]:
    if not path.exists():
        return None
    with suppress(Exception):
        df = pd.read_parquet(path)
        s = df[col].copy()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.sort_index()
    return None


# ════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD HELPERS
# ════════════════════════════════════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _dl_daily(chunk: list) -> pd.DataFrame:
    """Daily OHLCV from LOOKBACK_START to today (batch of ≤CHUNK_SIZE tickers)."""
    end = (date.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return yf.download(
        tickers=chunk, start=LOOKBACK_START, end=end,
        auto_adjust=True, progress=False, threads=True,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _dl_intraday(chunk: list) -> pd.DataFrame:
    """Today's 5-min bars (batch of ≤CHUNK_SIZE tickers)."""
    return yf.download(
        tickers=chunk, period="1d", interval="5m",
        auto_adjust=True, progress=False, threads=True,
    )


def _extract(raw: pd.DataFrame, field: str, tickers: list) -> dict[str, pd.Series]:
    """
    Extract {ticker: Series} from a yfinance batch download.
    Handles single-ticker flat columns and both MultiIndex orientations.
    """
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
        if len(tickers) == 1 and field in cols:
            s = _clean(raw[field])
            if s is not None:
                result[tickers[0]] = s
        return result

    l0 = set(cols.get_level_values(0))
    l1 = set(cols.get_level_values(1))

    if field in l0:
        block = raw[field]
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
    elif field in l1:
        for t in tickers:
            if (t, field) in cols:
                s = _clean(raw[(t, field)])
                if s is not None:
                    result[t] = s

    return result


# ════════════════════════════════════════════════════════════════════════════
#  DAILY CACHE REFRESH
# ════════════════════════════════════════════════════════════════════════════

def refresh_daily_cache(tickers: list, force: bool = False) -> tuple[int, int]:
    """
    Download and cache daily HIGH (and CLOSE) for stale tickers.

    Args:
        tickers: full list of universe tickers
        force:   bypass TTL check (used in EOD pass to pick up today's close)

    Returns:
        (refreshed_count, failed_count)
    """
    stale = [t for t in tickers if force or not _is_fresh(_high_path(t))]
    if not stale:
        logger.info("Daily cache: all %d tickers fresh", len(tickers))
        return 0, 0

    logger.info("Daily cache: refreshing %d/%d tickers", len(stale), len(tickers))
    refreshed = failed = 0
    chunks = [stale[i : i + CHUNK_SIZE] for i in range(0, len(stale), CHUNK_SIZE)]

    for i, chunk in enumerate(chunks, 1):
        try:
            raw    = _dl_daily(chunk)
            highs  = _extract(raw, "High",  chunk)
            closes = _extract(raw, "Close", chunk)

            for t in chunk:
                h = highs.get(t)
                c = closes.get(t)
                if h is not None and not h.empty:
                    _save_series(_high_path(t), h, "High")
                    if c is not None and not c.empty:
                        _save_series(_close_path(t), c, "Close")
                    refreshed += 1
                else:
                    logger.warning("Daily refresh: no data for %s (chunk %d)", t, i)
                    failed += 1
        except Exception as exc:
            logger.error("Daily refresh chunk %d/%d failed: %s", i, len(chunks), exc)
            failed += len(chunk)

    logger.info("Daily cache refresh done: %d refreshed, %d failed", refreshed, failed)
    return refreshed, failed


# ════════════════════════════════════════════════════════════════════════════
#  INTRADAY PRICE FETCH
# ════════════════════════════════════════════════════════════════════════════

def fetch_current_prices(tickers: list) -> dict[str, float]:
    """
    Get the latest market price for each ticker using 5-min bars.
    After market close this returns today's last traded price (≈ closing price).
    Returns {} for tickers where download failed.
    """
    prices: dict[str, float] = {}
    chunks = [tickers[i : i + CHUNK_SIZE] for i in range(0, len(tickers), CHUNK_SIZE)]

    for i, chunk in enumerate(chunks, 1):
        try:
            raw = _dl_intraday(chunk)
            for t, s in _extract(raw, "Close", chunk).items():
                if not s.empty:
                    prices[t] = float(s.iloc[-1])
        except Exception as exc:
            logger.warning("Intraday fetch chunk %d/%d failed: %s", i, len(chunks), exc)

    missing = len(tickers) - len(prices)
    if missing:
        logger.warning("Intraday: no price for %d tickers", missing)
    return prices


# ════════════════════════════════════════════════════════════════════════════
#  BENCHMARK COMPUTATION
# ════════════════════════════════════════════════════════════════════════════

def compute_benchmarks(tickers: list) -> dict[str, dict]:
    """
    Compute per-ticker 252-day benchmarks from daily cache.

    high_benchmark  = max(daily HIGH, prior 252 trading days)   ← used for intraday signal
    close_benchmark = max(daily CLOSE, prior 252 trading days)  ← used for EOD confirmation

    'Prior 252' = shift(1).rolling(252).max() — same convention as backtest.
    Returns empty dict entry for tickers with insufficient history.
    """
    result: dict[str, dict] = {}
    for t in tickers:
        h = _load_series(_high_path(t),  "High")
        c = _load_series(_close_path(t), "Close")
        if h is None or len(h) < ROLLING_WINDOW + 2:
            continue
        try:
            hb = h.shift(1).rolling(ROLLING_WINDOW).max().dropna()
            cb = c.shift(1).rolling(ROLLING_WINDOW).max().dropna() if c is not None and len(c) >= ROLLING_WINDOW + 2 else None
            if hb.empty:
                continue
            hb_val = float(hb.iloc[-1])
            cb_val = float(cb.iloc[-1]) if cb is not None and not cb.empty else None
            if not np.isnan(hb_val):
                result[t] = {"high_benchmark": hb_val, "close_benchmark": cb_val}
        except Exception:
            pass
    return result


# ════════════════════════════════════════════════════════════════════════════
#  SUPPRESSION / POSITION STATE
# ════════════════════════════════════════════════════════════════════════════

def load_suppressed() -> set[str]:
    """
    Tickers where new signals must be suppressed:
      - OPEN live positions (source='live', status='open')
      - PENDING signals (status='pending')
    REJECTED and EXPIRED do NOT suppress (per design spec).
    """
    engine = get_engine()
    suppressed: set[str] = set()
    with suppress(Exception):
        df = pd.read_sql(
            "SELECT ticker FROM trades WHERE source='live' AND status='open'", engine
        )
        suppressed.update(df["ticker"].tolist())
    with suppress(Exception):
        df = pd.read_sql(
            "SELECT ticker FROM signals WHERE status='pending'", engine
        )
        suppressed.update(df["ticker"].tolist())
    return suppressed


def count_open_live() -> int:
    engine = get_engine()
    with suppress(Exception):
        df = pd.read_sql(
            "SELECT COUNT(*) AS n FROM trades WHERE source='live' AND status='open'",
            engine,
        )
        return int(df["n"].iloc[0])
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  NOTIFICATION STUBS (replaced by Telegram at Checkpoint 5)
# ════════════════════════════════════════════════════════════════════════════

_TYPE_LABELS = {
    "intraday_provisional":  "provisional",
    "eod_confirmed":         "eod_confirmed",
    "provisional_unconfirmed": "provisional_unconfirmed",
}


def _notify_signal(
    ticker: str, company: str, signal_price: float, benchmark: float,
    open_count: int, cap: int, sig_type: str, sig_id: int,
    conviction_tier: str | None = None,
) -> None:
    """
    CP4 STUB — logs to console.
    CP5 replaces this with Telegram message via bot.py.

    Per design spec the Telegram message must include:
      'Signal price: Rs.XXX -- actual fill price may differ.'
    Cap warning included when open_count >= cap -- never silently suppressed.
    Conviction tier logged here; formatted for Telegram in bot.py._fmt_signal().
    """
    cap_note  = f"  [CAP REACHED -- {open_count}/{cap} positions open]" if open_count >= cap else ""
    tier_note = f"  [Conviction: {conviction_tier}]" if conviction_tier else ""
    type_disp = _TYPE_LABELS.get(sig_type, sig_type)
    logger.info(
        "[SIGNAL id=%d] %s (%s) | price=%.2f above 252d-high %.2f | type=%s%s%s",
        sig_id, ticker, company, signal_price, benchmark, type_disp, tier_note, cap_note,
    )


def _notify_eod_update(ticker: str, sig_id: int, confirmed: bool) -> None:
    """CP4 STUB — CP5 sends Telegram follow-up if provisional_unconfirmed."""
    status = "EOD_CONFIRMED" if confirmed else "PROVISIONAL_UNCONFIRMED"
    logger.info("[EOD-CONFIRM id=%d] %s → %s", sig_id, ticker, status)


def _notify_stop_exit(
    ticker: str, company: str, exit_price: float, return_pct: float
) -> None:
    """CP4 STUB — CP5 sends Telegram exit alert."""
    logger.info(
        "[EXIT] %s (%s) trailing stop hit | exit=%.2f | return=%.2f%%",
        ticker, company, exit_price, return_pct,
    )


# ════════════════════════════════════════════════════════════════════════════
#  CORE SCAN
# ════════════════════════════════════════════════════════════════════════════

def run_scan(universe_df: pd.DataFrame) -> None:
    """
    Single hourly scan:
      1. Refresh stale daily HIGH/CLOSE cache
      2. Fetch current intraday prices
      3. Detect new 52-week highs (current_price > high_252d_benchmark)
      4. Skip suppressed tickers (open positions + pending signals)
      5. Write Signal records to DB (status='pending', type='intraday_provisional')
      6. Notify (stub at CP4)
    """
    tickers   = universe_df["ticker"].tolist()
    scan_ts   = datetime.now(timezone.utc).isoformat()
    today_str = date.today().isoformat()
    name_map  = dict(zip(universe_df["ticker"], universe_df["company_name"]))

    logger.info("=== SCAN START %s UTC | %d tickers ===", scan_ts[:19], len(tickers))

    refresh_daily_cache(tickers)
    prices     = fetch_current_prices(tickers)
    benchmarks = compute_benchmarks(tickers)
    suppressed = load_suppressed()
    open_count = count_open_live()
    cap        = MAX_POSITIONS

    if not prices:
        logger.warning("No intraday prices — market may be closed or yfinance unreachable")
        return

    n_above = n_suppressed = n_written = 0

    for ticker, price in prices.items():
        bm = benchmarks.get(ticker)
        if bm is None:
            continue
        hb = bm["high_benchmark"]
        if price <= hb:
            continue

        n_above += 1

        if ticker in suppressed:
            n_suppressed += 1
            logger.debug("Suppressed: %s (open position or pending signal)", ticker)
            continue

        company = name_map.get(ticker, "")
        sig_id: Optional[int] = None

        # Conviction tier (advisory only -- never blocks the signal)
        conviction: dict = {}
        try:
            conviction = get_signal_conviction(ticker, today_str)
        except Exception as exc:
            logger.debug("Conviction lookup failed for %s: %s", ticker, exc)

        try:
            with session_scope() as sess:
                sig = Signal(
                    ticker=ticker,
                    company_name=company,
                    signal_price=round(price, 2),
                    signal_date=today_str,
                    scan_timestamp=scan_ts,
                    signal_type="intraday_provisional",
                    benchmark_252d=round(hb, 2),
                    status="pending",
                    positions_open_at_signal=open_count,
                    cap_at_signal=cap,
                    strategy_version=STRATEGY_VERSION,
                    conviction_tier=conviction.get("tier"),
                    regime_score=conviction.get("score"),
                )
                sess.add(sig)
                sess.flush()
                sig_id = sig.id
        except Exception as exc:
            logger.error("Failed to write signal for %s: %s", ticker, exc)
            continue

        _notify_signal(
            ticker, company, price, hb, open_count, cap,
            "intraday_provisional", sig_id,
            conviction_tier=conviction.get("tier"),
        )
        n_written += 1

    logger.info(
        "=== SCAN END | above benchmark: %d | suppressed: %d | new signals: %d ===",
        n_above, n_suppressed, n_written,
    )


# ════════════════════════════════════════════════════════════════════════════
#  EOD PASS — close confirmation + trailing stop check
# ════════════════════════════════════════════════════════════════════════════

def run_eod(universe_df: pd.DataFrame) -> None:
    """
    EOD pass (runs once after 15:30 IST):

    1. Fetch today's closing prices (via intraday download — last bar = closing price)
    2. For each provisional signal from today:
         today_close > close_252d_benchmark → "eod_confirmed"
         otherwise                          → "provisional_unconfirmed"
    3. For each open live position:
         today_close <= trailing_stop  → exit trade (mark closed)
         today_close > highest_price   → update highest_price + trailing_stop
    """
    today_str  = date.today().isoformat()
    tickers    = universe_df["ticker"].tolist()
    name_map   = dict(zip(universe_df["ticker"], universe_df["company_name"]))

    logger.info("=== EOD PASS START %s ===", today_str)

    # Today's closing prices via intraday download
    today_closes = fetch_current_prices(tickers)
    if not today_closes:
        logger.warning("EOD: no closing prices retrieved — aborting EOD pass")
        return

    # Benchmarks from cached daily data (prior 252 days — no refresh needed)
    benchmarks = compute_benchmarks(tickers)
    engine     = get_engine()

    # ── 1. Close confirmation ────────────────────────────────────────────
    try:
        pend = pd.read_sql(
            "SELECT * FROM signals "
            "WHERE status='pending' AND signal_date=:d AND signal_type='intraday_provisional'",
            engine, params={"d": today_str},
        )
    except Exception as exc:
        logger.error("EOD: cannot load pending signals: %s", exc)
        pend = pd.DataFrame()

    for _, row in pend.iterrows():
        ticker = row["ticker"]
        sig_id = int(row["id"])
        tc     = today_closes.get(ticker)
        cb     = benchmarks.get(ticker, {}).get("close_benchmark")
        if tc is None or cb is None:
            logger.warning("EOD: missing data for signal %d (%s)", sig_id, ticker)
            continue

        confirmed = tc > cb
        new_type  = "eod_confirmed" if confirmed else "provisional_unconfirmed"
        try:
            with session_scope() as sess:
                sig = sess.query(Signal).filter_by(id=sig_id).first()
                if sig:
                    sig.signal_type = new_type
                    sig.updated_at  = datetime.now(timezone.utc).isoformat()
            _notify_eod_update(ticker, sig_id, confirmed)
        except Exception as exc:
            logger.error("EOD: failed to update signal %d: %s", sig_id, exc)

    # ── 2. Trailing stop check ───────────────────────────────────────────
    try:
        open_live = pd.read_sql(
            "SELECT * FROM trades WHERE source='live' AND status='open'", engine
        )
    except Exception as exc:
        logger.error("EOD: cannot load open live trades: %s", exc)
        open_live = pd.DataFrame()

    for _, row in open_live.iterrows():
        ticker   = row["ticker"]
        company  = row.get("company_name", "")
        trade_id = int(row["id"])
        t_high   = float(row["highest_price_reached"])
        t_stop   = float(row["trailing_stop"])
        entry_p  = float(row["entry_price"])
        tc       = today_closes.get(ticker)

        if tc is None:
            logger.warning("EOD: no price for %s (trade %d), skipping", ticker, trade_id)
            continue

        new_high = max(t_high, tc)
        new_stop = round(new_high * TRAILING_STOP_PCT, 2)

        if tc <= new_stop:
            # Exit triggered
            ret_pct  = round((tc - entry_p) / entry_p * 100, 4)
            hold_d   = (date.today() - pd.Timestamp(row["entry_date"]).date()).days
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
                _notify_stop_exit(ticker, company, tc, ret_pct)
            except Exception as exc:
                logger.error("EOD: failed to close trade %d: %s", trade_id, exc)

        elif new_high > t_high:
            # New high — update trailing stop upward
            try:
                with session_scope() as sess:
                    t = sess.query(Trade).filter_by(id=trade_id).first()
                    if t and t.status == "open":
                        t.highest_price_reached = round(new_high, 2)
                        t.trailing_stop         = new_stop
                        t.updated_at            = datetime.utcnow().isoformat()
                logger.debug(
                    "EOD: %s stop %.2f → %.2f (new high %.2f)", ticker, t_stop, new_stop, new_high
                )
            except Exception as exc:
                logger.error("EOD: failed to update stop for trade %d: %s", trade_id, exc)

    logger.info("=== EOD PASS END ===")


# ════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

def _guarded_scan(universe_df: pd.DataFrame) -> None:
    if not is_trading_day():
        logger.info("Scan skipped — not a trading day (weekday=%d)", _now_ist().weekday())
        return
    if not is_market_hours():
        logger.info("Scan skipped — outside market hours (%s IST)", _now_ist().strftime("%H:%M"))
        return
    run_scan(universe_df)


def _guarded_eod(universe_df: pd.DataFrame) -> None:
    if not is_trading_day():
        logger.info("EOD skipped — not a trading day")
        return
    run_eod(universe_df)


def start_scheduler() -> None:
    """
    Start the blocking APScheduler.

    Jobs (all UTC, Mon–Fri):
      hourly_scan  — every hour 03:00–10:00 UTC (market-hours guard runs inside)
                     = 08:30–15:30 IST, guarded to 09:15–15:30 IST
      eod_pass     — 10:05 UTC = 15:35 IST (5 min after market close)
    """
    universe_df = fetch_nifty500()
    logger.info("Universe: %d tickers loaded", len(universe_df))

    scheduler = BlockingScheduler(timezone=pytz.utc)

    scheduler.add_job(
        _guarded_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour="3-10", minute=0, timezone=pytz.utc),
        args=[universe_df],
        id="hourly_scan",
        name="52WH Intraday Scanner",
        misfire_grace_time=300,
        coalesce=True,
    )

    scheduler.add_job(
        _guarded_eod,
        trigger=CronTrigger(day_of_week="mon-fri", hour=10, minute=5, timezone=pytz.utc),
        args=[universe_df],
        id="eod_pass",
        name="52WH EOD Confirmation",
        misfire_grace_time=600,
        coalesce=True,
    )

    logger.info(
        "Scheduler started. Hourly: 03–10:00 UTC Mon–Fri | EOD: 10:05 UTC Mon–Fri"
    )
    logger.info("Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scanner stopped.")
        scheduler.shutdown(wait=False)


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="52-Week High Intraday Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scanner.py              # start scheduler (runs continuously)\n"
            "  python scanner.py --run-now   # single scan, ignores market-hours guard\n"
            "  python scanner.py --eod-now   # single EOD pass\n"
        ),
    )
    parser.add_argument(
        "--run-now", action="store_true",
        help="Execute one scan immediately (bypasses market-hours check), then exit.",
    )
    parser.add_argument(
        "--eod-now", action="store_true",
        help="Execute EOD confirmation pass immediately, then exit.",
    )
    args = parser.parse_args()

    universe_df = fetch_nifty500()

    if args.run_now:
        logger.info("--run-now: single scan (market-hours guard bypassed)")
        run_scan(universe_df)
    elif args.eod_now:
        logger.info("--eod-now: EOD confirmation pass")
        run_eod(universe_df)
    else:
        start_scheduler()
