"""
S&P 500 52-Week High Strategy — Backtest Engine

BACKTEST RULES (identical to Nifty 52wh_v1, applied to S&P 500):
    Signal:        daily CLOSE > max(daily CLOSE, prior 252 trading days)
                   — "prior" means shift by 1 (today excluded from rolling window)
    Entry price:   CLOSE on signal day
    Initial stop:  entry_price x 0.80
    Trailing stop: max(close since entry) x 0.80  [only moves up]
    Exit:          daily CLOSE <= trailing_stop
    Re-entry:      once in a trade, no new signals until stopped out
    Sizing:        equal-weight, UNLIMITED capital

KEY DIFFERENCES vs. NIFTY ENGINE:
    Universe:      time-varying S&P 500 membership from sp500_membership table
                   Entry signals only fire when ticker is an active member
    Delisting:     when price data ends >45 calendar days before today while in
                   a trade, exit at last available price with exit_reason='delisted'
    Artifacts:     single-day absolute move >25% during a holding period is flagged
                   (earnings gaps of 10-20% are common for US stocks; 25% threshold
                    is set to catch data errors, not legitimate moves)
    Ticker format: no .NS suffix (US tickers)
    strategy_version: 'sp500_52wh_v1'

BACKTEST PERIOD:
    Lookback data:   2005-01-01 (extra year for 252-day warm-up)
    Actual period:   2006-01-01 to today

Illustrative, equal-weight, no capital constraints — not a real portfolio simulation.
"""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from tenacity import (
    retry, stop_after_attempt, wait_exponential, before_sleep_log,
)

_HERE = Path(__file__).resolve().parent   # SP500/backtest
_ROOT = _HERE.parent.parent               # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import session_scope, get_engine
from shared.models import Trade, Base

logger = logging.getLogger(__name__)

# ── Strategy constants ────────────────────────────────────────────────────────
LOOKBACK_START    = "2005-01-01"
BACKTEST_START    = pd.Timestamp("2006-01-01")
TRAILING_STOP_PCT = 0.80
ROLLING_WINDOW    = 252
STRATEGY_VERSION  = "sp500_52wh_v1"
CHUNK_SIZE        = 50
ARTIFACT_THRESHOLD = 0.25   # >25% single-day abs move → possible data error
DELISTING_GAP_DAYS = 45     # if last price is >=45 calendar days old, treat as delisted

# ── Price cache ───────────────────────────────────────────────────────────────
_PRICE_CACHE_DIR = _ROOT / "data" / "cache" / "prices_sp500"
_CACHE_MAX_AGE_H = 23


# ════════════════════════════════════════════════════════════════════════════
#  PRICE CACHE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _cache_path(ticker: str) -> Path:
    safe = ticker.replace(".", "_").replace("/", "_")
    return _PRICE_CACHE_DIR / f"{safe}.parquet"


def _cache_is_fresh(ticker: str) -> bool:
    p = _cache_path(ticker)
    if not p.exists():
        return False
    age_h = (
        datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    ).total_seconds() / 3600
    return age_h < _CACHE_MAX_AGE_H


def _save_cache(ticker: str, series: pd.Series) -> None:
    with suppress(Exception):
        p = _cache_path(ticker)
        p.parent.mkdir(parents=True, exist_ok=True)
        series.rename("Close").to_frame().to_parquet(p)


def _load_cache(ticker: str) -> Optional[pd.Series]:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    with suppress(Exception):
        df = pd.read_parquet(p)
        s  = df["Close"].copy()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s
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
def _yf_download_chunk(chunk: list, start: str, end: str) -> pd.DataFrame:
    return yf.download(
        tickers=chunk,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        threads=True,
    )


def _extract_close(raw: pd.DataFrame, tickers: list) -> dict:
    """
    Extract {ticker: pd.Series(Close)} from a yfinance batch result.
    Handles flat columns (single ticker) and MultiIndex (multi-ticker),
    and both (Price, Ticker) / (Ticker, Price) MultiIndex orderings.
    """
    result = {}
    if raw is None or raw.empty:
        return result

    cols = raw.columns

    def _clean(s: pd.Series) -> Optional[pd.Series]:
        s = s.dropna()
        if s.empty:
            return None
        s = s.copy()
        s.index = pd.to_datetime(s.index)
        if hasattr(s.index, "tz") and s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s

    # ── Case A: flat columns (single ticker returned) ─────────────────────
    if not isinstance(cols, pd.MultiIndex):
        if len(tickers) == 1 and "Close" in cols:
            s = _clean(raw["Close"])
            if s is not None:
                result[tickers[0]] = s
        return result

    # ── Case B: MultiIndex ────────────────────────────────────────────────
    l0 = set(cols.get_level_values(0))
    l1 = set(cols.get_level_values(1))

    if "Close" in l0:
        # (Price, Ticker) — yfinance >= 0.2.x default
        close_block = raw["Close"]
        if isinstance(close_block, pd.Series):
            if len(tickers) == 1:
                s = _clean(close_block)
                if s is not None:
                    result[tickers[0]] = s
        else:
            for t in tickers:
                if t in close_block.columns:
                    s = _clean(close_block[t])
                    if s is not None:
                        result[t] = s
        return result

    if "Close" in l1:
        # (Ticker, Price) — group_by='ticker' style
        for t in tickers:
            try:
                s = _clean(raw[t]["Close"])
                if s is not None:
                    result[t] = s
            except (KeyError, TypeError):
                pass
        return result

    logger.warning(
        "Unrecognised yfinance column structure. "
        "Level-0 sample: %s  Level-1 sample: %s",
        list(l0)[:5], list(l1)[:5],
    )
    return result


def download_all_prices(
    tickers: list,
    force_refresh: bool = False,
) -> tuple[dict, list]:
    """
    Download adjusted daily Close for all tickers from LOOKBACK_START to today.
    Caches each ticker as a .parquet in data/cache/prices_sp500/.
    Returns ({ticker: Series}, [failed_tickers]).
    """
    end_date = date.today().strftime("%Y-%m-%d")

    to_download: list = []
    from_cache:  dict = {}

    for t in tickers:
        if not force_refresh and _cache_is_fresh(t):
            s = _load_cache(t)
            if s is not None:
                from_cache[t] = s
                continue
        to_download.append(t)

    if from_cache:
        logger.info("Cache hit: %d tickers loaded from disk.", len(from_cache))
    if not to_download:
        return from_cache, []

    chunks = [
        to_download[i: i + CHUNK_SIZE]
        for i in range(0, len(to_download), CHUNK_SIZE)
    ]
    logger.info(
        "Downloading %d tickers in %d batches from %s to %s ...",
        len(to_download), len(chunks), LOOKBACK_START, end_date,
    )

    downloaded: dict = {}
    failed:     list = []

    for i, chunk in enumerate(chunks, 1):
        logger.info("  Batch %2d/%d: %d tickers ...", i, len(chunks), len(chunk))
        try:
            raw        = _yf_download_chunk(chunk, LOOKBACK_START, end_date)
            chunk_data = _extract_close(raw, chunk)
            for t, s in chunk_data.items():
                _save_cache(t, s)
                downloaded[t] = s
            missing = [t for t in chunk if t not in chunk_data]
            if missing:
                logger.warning("    No data: %s", ", ".join(missing))
                failed.extend(missing)
        except Exception as exc:
            logger.error("  Batch %d failed after retries: %s", i, exc)
            for t in chunk:
                s = _load_cache(t)
                if s is not None:
                    logger.warning("    Stale cache fallback: %s", t)
                    downloaded[t] = s
                else:
                    failed.append(t)

    if failed:
        logger.warning(
            "[!] %d tickers produced NO data and are excluded from backtest:\n    %s",
            len(failed), ", ".join(failed),
        )

    result = {**from_cache, **downloaded}
    logger.info(
        "Price data ready: %d with data | %d failed | %d requested.",
        len(result), len(failed), len(tickers),
    )
    return result, failed


# ════════════════════════════════════════════════════════════════════════════
#  MEMBERSHIP CHECK (time-varying universe)
# ════════════════════════════════════════════════════════════════════════════

def _is_sp500_member(
    d: date,
    periods: list[tuple[date, date | None]],
) -> bool:
    """
    Return True if ticker was an S&P 500 member on date d.
    periods = [(added_date, removed_date_or_None), ...].
    A ticker may have multiple intervals (added, removed, re-added).
    """
    for added, removed in periods:
        if d >= added and (removed is None or d < removed):
            return True
    return False


# ════════════════════════════════════════════════════════════════════════════
#  SIMULATION
# ════════════════════════════════════════════════════════════════════════════

def _simulate_ticker(
    ticker: str,
    close: pd.Series,
    company_name: str,
    membership_periods: list[tuple[date, date | None]],
) -> list:
    """
    Simulate the 52-week high strategy on a single S&P 500 ticker.

    Key differences vs. Nifty:
      - Entry only when ticker is an active S&P 500 member on that date.
      - Delisting detection: if the price series ends >= DELISTING_GAP_DAYS
        before today while a trade is open, exit at the last available price
        with exit_reason='delisted'.
      - Artifact flag: max single-day abs return >25% during a trade is
        stored in the trade dict as 'artifact_flag' / 'max_daily_move_pct'.
        These fields are NOT persisted to the trades table (no schema change);
        they are used for CLI reporting only.
    """
    # 252-day rolling max of PRIOR closes (shift 1 so today is excluded)
    rolling_max = close.shift(1).rolling(ROLLING_WINDOW).max()

    # Simulate from BACKTEST_START only
    mask           = close.index >= BACKTEST_START
    close_bt       = close[mask]
    rolling_max_bt = rolling_max[mask]

    if close_bt.empty:
        return []

    today          = date.today()
    last_data_date = close_bt.index[-1].date()
    days_stale     = (today - last_data_date).days

    # Pre-compute daily absolute returns for artifact detection
    daily_abs_ret = close_bt.pct_change().abs()

    trades:          list = []
    in_trade:        bool = False
    entry_date       = entry_price = peak_close = trailing_stop = None

    for dt in close_bt.index:
        c  = close_bt[dt]
        rm = rolling_max_bt[dt]

        if pd.isna(c):
            continue

        if in_trade:
            # Update trailing stop
            if c > peak_close:
                peak_close    = c
                trailing_stop = peak_close * TRAILING_STOP_PCT

            # Exit: close at or below trailing stop
            if c <= trailing_stop:
                ret_pct      = (c - entry_price) / entry_price * 100
                holding_days = (dt - entry_date).days
                trades.append(_trade_record(
                    ticker, company_name,
                    entry_date, entry_price, peak_close, trailing_stop,
                    exit_date=dt, exit_price=c,
                    holding_days=holding_days, return_pct=ret_pct,
                    status="closed", exit_reason="trailing_stop",
                    close_bt=close_bt, daily_abs_ret=daily_abs_ret,
                ))
                in_trade = entry_date = entry_price = peak_close = trailing_stop = None

        else:
            # Entry: new 252-day closing high — only while an active S&P 500 member
            dt_date = dt.date()
            if (
                (not pd.isna(rm))
                and (c > rm)
                and _is_sp500_member(dt_date, membership_periods)
            ):
                in_trade      = True
                entry_date    = dt
                entry_price   = c
                peak_close    = c
                trailing_stop = c * TRAILING_STOP_PCT

    # Handle trade still open at end of data
    if in_trade:
        last_close = close_bt.iloc[-1]
        last_dt    = close_bt.index[-1]
        unrealized = (last_close - entry_price) / entry_price * 100

        if days_stale >= DELISTING_GAP_DAYS:
            # Stock data ended well before today — treat as delisted/acquired
            ret_pct      = (last_close - entry_price) / entry_price * 100
            holding_days = (last_dt - entry_date).days
            trades.append(_trade_record(
                ticker, company_name,
                entry_date, entry_price, peak_close, trailing_stop,
                exit_date=last_dt, exit_price=last_close,
                holding_days=holding_days, return_pct=ret_pct,
                status="closed", exit_reason="delisted",
                close_bt=close_bt, daily_abs_ret=daily_abs_ret,
            ))
        else:
            # Still active
            trades.append(_trade_record(
                ticker, company_name,
                entry_date, entry_price, peak_close, trailing_stop,
                exit_date=None, exit_price=None,
                holding_days=(last_dt - entry_date).days,
                return_pct=None, status="open", exit_reason=None,
                close_bt=close_bt, daily_abs_ret=daily_abs_ret,
                extra={"unrealized_return_pct": round(unrealized, 4)},
            ))

    return trades


def _trade_record(
    ticker, company_name,
    entry_date, entry_price, peak_close, trailing_stop,
    exit_date, exit_price, holding_days, return_pct,
    status, exit_reason,
    close_bt: pd.Series,
    daily_abs_ret: pd.Series,
    extra: dict | None = None,
) -> dict:
    # Artifact detection: max single-day abs move during holding period
    end_dt   = exit_date if exit_date is not None else close_bt.index[-1]
    in_range = daily_abs_ret[entry_date:end_dt]
    max_move = float(in_range.max()) if not in_range.empty else 0.0
    if pd.isna(max_move):
        max_move = 0.0

    rec = {
        "ticker":                ticker,
        "company_name":          company_name,
        "entry_date":            entry_date.strftime("%Y-%m-%d"),
        "entry_price":           round(float(entry_price), 4),
        "highest_price_reached": round(float(peak_close), 4),
        "trailing_stop":         round(float(trailing_stop), 4),
        "exit_date":             exit_date.strftime("%Y-%m-%d") if exit_date else None,
        "exit_price":            round(float(exit_price), 4) if exit_price else None,
        "holding_days":          int(holding_days),
        "return_pct":            round(float(return_pct), 4) if return_pct is not None else None,
        "trade_year":            int(entry_date.year),
        "status":                status,
        "exit_reason":           exit_reason,
        "source":                "backtest",
        "strategy_version":      STRATEGY_VERSION,
        # Artifact info — in-memory only, NOT saved to trades table
        "artifact_flag":         max_move > ARTIFACT_THRESHOLD,
        "max_daily_move_pct":    round(max_move * 100, 2),
    }
    if extra:
        rec.update(extra)
    return rec


def simulate_all(
    price_data: dict,
    membership_map: dict[str, list[tuple[date, date | None]]],
) -> pd.DataFrame:
    """
    Run the 52-week high simulation across all S&P 500 tickers.
    membership_map: {ticker: [(added, removed_or_None), ...]}
    Returns DataFrame of all trade records (closed + open + delisted).
    """
    all_trades: list = []
    n = len(price_data)

    for i, (ticker, close) in enumerate(price_data.items(), 1):
        if i % 100 == 0 or i == n:
            logger.info("  Simulating %3d/%d tickers ...", i, n)
        periods = membership_map.get(ticker, [])
        all_trades.extend(_simulate_ticker(ticker, close, ticker, periods))

    logger.info("Simulation done: %d total trade records.", len(all_trades))
    return pd.DataFrame(all_trades) if all_trades else pd.DataFrame()


# ════════════════════════════════════════════════════════════════════════════
#  STATS
# ════════════════════════════════════════════════════════════════════════════

def _group_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {k: None for k in [
            "total_trades", "win_rate_pct", "avg_return_pct", "median_return_pct",
            "avg_holding_days", "best_trade_pct", "worst_trade_pct", "gross_return_pct",
        ]}
    wins = df[df["return_pct"] > 0]
    return {
        "total_trades":      int(len(df)),
        "win_rate_pct":      round(len(wins) / len(df) * 100, 1),
        "avg_return_pct":    round(float(df["return_pct"].mean()), 2),
        "median_return_pct": round(float(df["return_pct"].median()), 2),
        "avg_holding_days":  round(float(df["holding_days"].mean()), 1),
        "best_trade_pct":    round(float(df["return_pct"].max()), 2),
        "worst_trade_pct":   round(float(df["return_pct"].min()), 2),
        "gross_return_pct":  round(float(df["return_pct"].sum()), 2),
    }


def compute_stats(trades_df: pd.DataFrame) -> tuple:
    """
    Returns:
        combined_stats — dict of full-period stats (closed trades only)
        yearly_table   — DataFrame, one row per year (trades opened that year)
        open_summary   — dict with count of still-open trades
    """
    closed = trades_df[trades_df["status"] == "closed"].copy()
    open_t = trades_df[trades_df["status"] == "open"].copy()

    combined = _group_stats(closed)
    combined["open_trades_count"] = len(open_t)
    if not open_t.empty and "unrealized_return_pct" in open_t.columns:
        combined["avg_unrealized_pct"] = round(
            float(open_t["unrealized_return_pct"].mean()), 2
        )

    current_year = date.today().year
    rows = []
    for year in sorted(closed["trade_year"].dropna().unique()):
        yr  = int(year)
        row = _group_stats(closed[closed["trade_year"] == yr])
        row["year"] = f"{yr} (YTD)" if yr == current_year else str(yr)
        rows.append(row)
    yearly_table = pd.DataFrame(rows) if rows else pd.DataFrame()

    open_summary = {
        "count":   len(open_t),
        "tickers": sorted(open_t["ticker"].tolist()) if not open_t.empty else [],
    }
    return combined, yearly_table, open_summary


def compute_equity_curve(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Cumulative sum of per-trade returns sorted by exit date.
    Additive (not compounded). Illustrative only.
    """
    closed = trades_df[trades_df["status"] == "closed"].copy()
    if closed.empty:
        return pd.DataFrame(columns=["exit_date", "cumulative_return_pct"])
    closed["exit_date"] = pd.to_datetime(closed["exit_date"])
    daily = (
        closed.groupby("exit_date")["return_pct"]
        .sum()
        .sort_index()
        .cumsum()
        .reset_index()
    )
    daily.columns = ["exit_date", "cumulative_return_pct"]
    return daily


# ════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════════════════

# Columns present in the Trade model — artifact fields exist only in-memory
_TRADE_MODEL_COLS = {
    "ticker", "company_name", "entry_date", "entry_price", "source",
    "highest_price_reached", "trailing_stop", "exit_date", "exit_price",
    "exit_reason", "return_pct", "holding_days", "trade_year",
    "status", "strategy_version",
}


def save_to_db(trades_df: pd.DataFrame) -> None:
    """
    Persist backtest trades to SQLite.
    Clears previous sp500_52wh_v1 backtest records first.
    Artifact flag columns are dropped before insert (not in Trade schema).
    """
    from sqlalchemy import text

    engine = get_engine()
    Base.metadata.create_all(engine)

    with session_scope() as session:
        deleted = session.execute(
            text("DELETE FROM trades WHERE source='backtest' AND strategy_version=:sv"),
            {"sv": STRATEGY_VERSION},
        ).rowcount
        if deleted:
            logger.info(
                "Cleared %d previous backtest records (strategy_version=%s).",
                deleted, STRATEGY_VERSION,
            )

    # Drop in-memory-only columns before inserting
    db_cols = [c for c in trades_df.columns if c in _TRADE_MODEL_COLS]
    records = (
        trades_df[db_cols]
        .fillna(np.nan)
        .replace({np.nan: None})
        .to_dict("records")
    )

    with session_scope() as session:
        for r in records:
            session.add(Trade(
                signal_id             = None,
                ticker                = r["ticker"],
                company_name          = r.get("company_name"),
                entry_date            = r["entry_date"],
                entry_price           = r["entry_price"],
                source                = "backtest",
                highest_price_reached = r.get("highest_price_reached"),
                trailing_stop         = r.get("trailing_stop"),
                exit_date             = r.get("exit_date"),
                exit_price            = r.get("exit_price"),
                exit_reason           = r.get("exit_reason"),
                return_pct            = r.get("return_pct"),
                holding_days          = r.get("holding_days"),
                trade_year            = r.get("trade_year"),
                status                = r.get("status", "open"),
                strategy_version      = r.get("strategy_version", STRATEGY_VERSION),
            ))

    logger.info("Saved %d trade records to SQLite (%s).", len(records), STRATEGY_VERSION)


# ════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════

def run_full_backtest(
    tickers: list,
    membership_map: dict,
    force_refresh: bool = False,
) -> tuple:
    """
    End-to-end S&P 500 backtest.
      1. Download prices (with cache)
      2. Simulate strategy (time-varying membership + delisting handling)
      3. Compute stats
      4. Save to SQLite
    Returns (trades_df, combined_stats, yearly_table, equity_curve, failed_tickers, open_summary).
    """
    price_data, failed = download_all_prices(tickers, force_refresh=force_refresh)

    logger.info("Running strategy simulation ...")
    trades_df = simulate_all(price_data, membership_map)

    if trades_df.empty:
        logger.error("No trades produced — check price data.")
        return trades_df, {}, pd.DataFrame(), pd.DataFrame(), failed, {"count": 0, "tickers": []}

    combined, yearly_table, open_summary = compute_stats(trades_df)
    equity_curve = compute_equity_curve(trades_df)

    logger.info("Saving results to SQLite ...")
    save_to_db(trades_df)

    return trades_df, combined, yearly_table, equity_curve, failed, open_summary
