"""
52-Week High Strategy — Backtest Engine

BACKTEST RULES (close-based, EOD only):
    Signal:        daily CLOSE > max(daily CLOSE, prior 252 trading days)
                   — "prior" means not including today (shift by 1)
    Entry price:   CLOSE on signal day
    Initial stop:  entry_price × 0.80
    Trailing stop: max(close since entry) × 0.80  [only moves up]
    Exit:          daily CLOSE ≤ trailing_stop
    Re-entry:      once in a trade, no new signals until stopped out
    Sizing:        equal-weight, UNLIMITED capital — position cap NOT applied

NOTE ON ADJUSTED PRICES:
    yfinance is used with auto_adjust=True. All prices (entry, exit, stop)
    are split- and dividend-adjusted. When verifying trades manually, compare
    against adjusted price charts (e.g. TradingView with "Adj" toggled on).
    Raw unadjusted prices will differ for stocks that had splits or dividends.

BACKTEST PERIOD:
    Lookback data:   2021-01-01 (for 252-day warm-up before backtest start)
    Actual period:   2022-01-01 to today
"""

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
    retry, stop_after_attempt, wait_exponential, before_sleep_log
)

# ── Path setup — allow engine.py to be run standalone ────────────────────────
_HERE = Path(__file__).resolve().parent          # …/52WeekHigh/backtest
_ROOT = _HERE.parent.parent                      # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
# ─────────────────────────────────────────────────────────────────────────────

from shared.db import session_scope, get_engine
from shared.models import Trade, Base

logger = logging.getLogger(__name__)

# ── Strategy constants ────────────────────────────────────────────────────────
LOOKBACK_START   = "2021-01-01"
BACKTEST_START   = pd.Timestamp("2022-01-01")
TRAILING_STOP_PCT = 0.80   # stop = highest_close_since_entry × this
ROLLING_WINDOW   = 252     # trading days  ("52-week")
STRATEGY_VERSION = "52wh_v1"
CHUNK_SIZE       = 50      # tickers per yfinance batch request

# ── Cache ─────────────────────────────────────────────────────────────────────
_PRICE_CACHE_DIR = _ROOT / "data" / "cache" / "prices"
_CACHE_MAX_AGE_H = 23      # hours before a price cache file is considered stale


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
    age_h = (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 3600
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
        s = df["Close"].copy()
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
    Extract a {ticker: pd.Series(Close)} dict from a yfinance batch download.
    Handles both single-ticker (flat) and multi-ticker (MultiIndex) results,
    and both (Price, Ticker) and (Ticker, Price) MultiIndex orderings.
    """
    result = {}
    if raw is None or raw.empty:
        return result

    cols = raw.columns

    def _clean(s: pd.Series, ticker: str) -> Optional[pd.Series]:
        s = s.dropna()
        if s.empty:
            return None
        s = s.copy()
        s.index = pd.to_datetime(s.index)
        if hasattr(s.index, "tz") and s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s

    # ── Case A: flat columns (single ticker) ─────────────────────────────────
    if not isinstance(cols, pd.MultiIndex):
        if len(tickers) == 1 and "Close" in cols:
            s = _clean(raw["Close"], tickers[0])
            if s is not None:
                result[tickers[0]] = s
        return result

    # ── Case B: MultiIndex ────────────────────────────────────────────────────
    l0 = set(cols.get_level_values(0))
    l1 = set(cols.get_level_values(1))

    if "Close" in l0:
        # (Price, Ticker) — yfinance ≥ 0.2.x default / 1.x
        close_block = raw["Close"]
        if isinstance(close_block, pd.Series):
            # Only one ticker came back as a Series
            if len(tickers) == 1:
                s = _clean(close_block, tickers[0])
                if s is not None:
                    result[tickers[0]] = s
        else:
            for t in tickers:
                if t in close_block.columns:
                    s = _clean(close_block[t], t)
                    if s is not None:
                        result[t] = s
        return result

    if "Close" in l1:
        # (Ticker, Price) — group_by='ticker' style
        for t in tickers:
            try:
                s = _clean(raw[t]["Close"], t)
                if s is not None:
                    result[t] = s
            except (KeyError, TypeError):
                pass
        return result

    logger.warning(
        "Unrecognised yfinance column structure. "
        f"Level-0 sample: {list(l0)[:5]}  Level-1 sample: {list(l1)[:5]}"
    )
    return result


def download_all_prices(
    tickers: list,
    force_refresh: bool = False,
) -> dict:
    """
    Download adjusted daily Close for all tickers from LOOKBACK_START to today.
    Caches each ticker as a .parquet file in data/cache/prices/.
    Falls back to cache on download failure.
    Returns {ticker: pd.Series} for tickers with valid data.
    Failed tickers are logged but not raised.
    """
    end_date = date.today().strftime("%Y-%m-%d")

    to_download, from_cache = [], {}
    for t in tickers:
        if not force_refresh and _cache_is_fresh(t):
            s = _load_cache(t)
            if s is not None:
                from_cache[t] = s
                continue
        to_download.append(t)

    if from_cache:
        logger.info(f"Cache hit: {len(from_cache)} tickers loaded from disk.")
    if not to_download:
        return from_cache

    chunks = [to_download[i: i + CHUNK_SIZE] for i in range(0, len(to_download), CHUNK_SIZE)]
    logger.info(
        f"Downloading {len(to_download)} tickers in {len(chunks)} batches "
        f"(~{CHUNK_SIZE}/batch) from {LOOKBACK_START} to {end_date} ..."
    )

    downloaded, failed = {}, []
    for i, chunk in enumerate(chunks, 1):
        logger.info(f"  Batch {i:2d}/{len(chunks)}: {len(chunk)} tickers ...")
        try:
            raw = _yf_download_chunk(chunk, LOOKBACK_START, end_date)
            chunk_data = _extract_close(raw, chunk)
            for t, s in chunk_data.items():
                _save_cache(t, s)
                downloaded[t] = s
            missing = [t for t in chunk if t not in chunk_data]
            if missing:
                logger.warning(f"    No data returned for: {', '.join(missing)}")
                failed.extend(missing)
        except Exception as exc:
            logger.error(f"  Batch {i} failed after retries: {exc}")
            # Try per-ticker fallback using stale cache
            for t in chunk:
                s = _load_cache(t)
                if s is not None:
                    logger.warning(f"    Using stale cache for {t}")
                    downloaded[t] = s
                else:
                    failed.append(t)

    if failed:
        logger.warning(
            f"\n[!] {len(failed)} tickers produced NO data and are excluded from the backtest:\n"
            f"    {', '.join(failed)}"
        )

    result = {**from_cache, **downloaded}
    logger.info(
        f"Price data ready: {len(result)} tickers with data | "
        f"{len(failed)} failed/missing | {len(tickers)} requested."
    )
    return result, failed


# ════════════════════════════════════════════════════════════════════════════
#  SIMULATION
# ════════════════════════════════════════════════════════════════════════════

def _simulate_ticker(ticker: str, close: pd.Series, company_name: str) -> list:
    """
    Simulate the 52-week high strategy on a single ticker's close series.

    Signal (backtest):  close > max(close, prior 252 trading days)
    Entry / exit:       both use daily close (EOD data only)
    Trailing stop:      max(close since entry) × TRAILING_STOP_PCT
                        — updated each day, moves up only
    Re-entry:           suppressed while a trade is open

    Returns a list of trade dicts — both closed and still-open.
    """
    # 252-day rolling max of PRIOR closes (shift by 1 so today is excluded)
    rolling_max = close.shift(1).rolling(ROLLING_WINDOW).max()

    # Only simulate from BACKTEST_START onward (lookback period is warm-up only)
    mask = close.index >= BACKTEST_START
    close_bt = close[mask]
    rolling_max_bt = rolling_max[mask]

    if close_bt.empty:
        return []

    trades = []
    in_trade = False
    entry_date = entry_price = peak_close = trailing_stop = None

    for dt in close_bt.index:
        c   = close_bt[dt]
        rm  = rolling_max_bt[dt]

        if pd.isna(c):
            continue

        if in_trade:
            # Update trailing stop with today's close (close-based trailing)
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
                ))
                in_trade = entry_date = entry_price = peak_close = trailing_stop = None

        else:
            # Entry: new 252-day closing high
            if (not pd.isna(rm)) and (c > rm):
                in_trade      = True
                entry_date    = dt
                entry_price   = c
                peak_close    = c
                trailing_stop = c * TRAILING_STOP_PCT

    # Trade still open at end of data
    if in_trade:
        last_close = close_bt.iloc[-1]
        last_date  = close_bt.index[-1]
        unrealized = (last_close - entry_price) / entry_price * 100
        trades.append(_trade_record(
            ticker, company_name,
            entry_date, entry_price, peak_close, trailing_stop,
            exit_date=None, exit_price=None,
            holding_days=(last_date - entry_date).days,
            return_pct=None, status="open", exit_reason=None,
            extra={"unrealized_return_pct": round(unrealized, 4)},
        ))

    return trades


def _trade_record(
    ticker, company_name,
    entry_date, entry_price, peak_close, trailing_stop,
    exit_date, exit_price, holding_days, return_pct,
    status, exit_reason, extra=None,
) -> dict:
    rec = {
        "ticker":               ticker,
        "company_name":         company_name,
        "entry_date":           entry_date.strftime("%Y-%m-%d"),
        "entry_price":          round(float(entry_price), 4),
        "highest_price_reached": round(float(peak_close), 4),
        "trailing_stop":        round(float(trailing_stop), 4),
        "exit_date":            exit_date.strftime("%Y-%m-%d") if exit_date is not None else None,
        "exit_price":           round(float(exit_price), 4) if exit_price is not None else None,
        "holding_days":         int(holding_days),
        "return_pct":           round(float(return_pct), 4) if return_pct is not None else None,
        "trade_year":           int(entry_date.year),
        "status":               status,
        "exit_reason":          exit_reason,
        "source":               "backtest",
        "strategy_version":     STRATEGY_VERSION,
    }
    if extra:
        rec.update(extra)
    return rec


def simulate_all(price_data: dict, universe_df: pd.DataFrame) -> pd.DataFrame:
    """
    Run the 52-week high simulation across all tickers.
    Returns a DataFrame of all trade records (closed + open).
    """
    company_map = dict(zip(universe_df["ticker"], universe_df["company_name"]))
    all_trades = []
    n = len(price_data)

    for i, (ticker, close) in enumerate(price_data.items(), 1):
        if i % 100 == 0 or i == n:
            logger.info(f"  Simulating {i:3d}/{n} tickers ...")
        name = company_map.get(ticker, ticker)
        all_trades.extend(_simulate_ticker(ticker, close, name))

    logger.info(f"Simulation done: {len(all_trades)} total trade records.")
    return pd.DataFrame(all_trades) if all_trades else pd.DataFrame()


# ════════════════════════════════════════════════════════════════════════════
#  STATS
# ════════════════════════════════════════════════════════════════════════════

def _group_stats(df: pd.DataFrame) -> dict:
    """Compute summary metrics for a DataFrame of closed trades."""
    if df.empty:
        return {k: None for k in [
            "total_trades", "win_rate_pct", "avg_return_pct", "median_return_pct",
            "avg_holding_days", "best_trade_pct", "worst_trade_pct", "gross_return_pct",
        ]}
    wins = df[df["return_pct"] > 0]
    return {
        "total_trades":     int(len(df)),
        "win_rate_pct":     round(len(wins) / len(df) * 100, 1),
        "avg_return_pct":   round(float(df["return_pct"].mean()), 2),
        "median_return_pct": round(float(df["return_pct"].median()), 2),
        "avg_holding_days": round(float(df["holding_days"].mean()), 1),
        "best_trade_pct":   round(float(df["return_pct"].max()), 2),
        "worst_trade_pct":  round(float(df["return_pct"].min()), 2),
        "gross_return_pct": round(float(df["return_pct"].sum()), 2),
    }


def compute_stats(trades_df: pd.DataFrame) -> tuple:
    """
    Returns:
        combined_stats  — dict of full-period stats (closed trades only)
        yearly_table    — DataFrame, one row per year (trades opened that year)
        open_summary    — dict summarising still-open trades
    """
    closed = trades_df[trades_df["status"] == "closed"].copy()
    open_t = trades_df[trades_df["status"] == "open"].copy()

    combined = _group_stats(closed)
    combined["open_trades_count"] = len(open_t)
    if not open_t.empty and "unrealized_return_pct" in open_t.columns:
        combined["avg_unrealized_pct"] = round(float(open_t["unrealized_return_pct"].mean()), 2)

    current_year = date.today().year
    rows = []
    for year in sorted(closed["trade_year"].dropna().unique()):
        yr = int(year)
        row = _group_stats(closed[closed["trade_year"] == yr])
        row["year"] = f"{yr} (YTD)" if yr == current_year else str(yr)
        rows.append(row)
    yearly_table = pd.DataFrame(rows) if rows else pd.DataFrame()

    open_summary = {
        "count": len(open_t),
        "tickers": sorted(open_t["ticker"].tolist()) if not open_t.empty else [],
    }
    return combined, yearly_table, open_summary


def compute_equity_curve(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Cumulative sum of per-trade returns sorted by exit date.
    Each point = total gross return from all trades closed on or before that date.

    NOTE: This is NOT a compound portfolio equity curve.
    It is the additive sum of individual trade returns (equal-weight per trade).
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

def save_to_db(trades_df: pd.DataFrame) -> None:
    """
    Persist backtest trades to SQLite.
    Clears any previous backtest run for this strategy_version first,
    so re-running the backtest always reflects the latest results.
    Live trades (source='live') are never touched.
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
            logger.info(f"Cleared {deleted} previous backtest records (strategy_version={STRATEGY_VERSION}).")

    records = trades_df.fillna(np.nan).replace({np.nan: None}).to_dict("records")
    with session_scope() as session:
        for r in records:
            session.add(Trade(
                signal_id            = None,
                ticker               = r["ticker"],
                company_name         = r.get("company_name"),
                entry_date           = r["entry_date"],
                entry_price          = r["entry_price"],
                source               = "backtest",
                highest_price_reached = r.get("highest_price_reached"),
                trailing_stop        = r.get("trailing_stop"),
                exit_date            = r.get("exit_date"),
                exit_price           = r.get("exit_price"),
                exit_reason          = r.get("exit_reason"),
                return_pct           = r.get("return_pct"),
                holding_days         = r.get("holding_days"),
                trade_year           = r.get("trade_year"),
                status               = r.get("status", "open"),
                strategy_version     = r.get("strategy_version", STRATEGY_VERSION),
            ))

    logger.info(f"Saved {len(records)} trade records to SQLite ({STRATEGY_VERSION}).")


# ════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════

def run_full_backtest(universe_df: pd.DataFrame, force_refresh: bool = False) -> tuple:
    """
    End-to-end backtest:
      1. Download prices (with cache)
      2. Simulate strategy
      3. Compute stats
      4. Save to SQLite
    Returns (trades_df, combined_stats, yearly_table, equity_curve, failed_tickers).
    """
    tickers = universe_df["ticker"].tolist()

    price_data, failed = download_all_prices(tickers, force_refresh=force_refresh)

    logger.info("Running strategy simulation ...")
    trades_df = simulate_all(price_data, universe_df)

    if trades_df.empty:
        logger.error("No trades produced — check price data.")
        return trades_df, {}, pd.DataFrame(), pd.DataFrame(), failed

    combined, yearly_table, open_summary = compute_stats(trades_df)
    equity_curve = compute_equity_curve(trades_df)

    logger.info("Saving results to SQLite ...")
    save_to_db(trades_df)

    return trades_df, combined, yearly_table, equity_curve, failed, open_summary
