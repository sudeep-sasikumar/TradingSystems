"""
Extended 52-Week High Backtest — Survivorship-Corrected, ~7-Year Window

Key differences from the standard backtest (engine.py):
  - Universe is time-varying: a signal is only valid when the stock was an
    actual Nifty 500 constituent on that date.
  - Open trades are NOT force-closed on index removal (trailing stop runs as normal).
  - Lookback data starts 2018-01-01 (252-day warm-up before Oct 2019 start).
  - Backtest start: BACKTEST_START (first date with reliable membership data).
  - Price cache: separate directory (data/cache/prices_historic/) to avoid
    contaminating the live scanner's 2021-present cache.
  - strategy_version: "52wh_v1_survivorship_10y" (user-specified tag, ~7y actual coverage).
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
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import session_scope, get_engine
from shared.models import Base, IndexMembership, Trade

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
LOOKBACK_START    = "2018-01-01"     # pre-backtest warm-up
BACKTEST_START    = pd.Timestamp("2019-10-01")
TRAILING_STOP_PCT = 0.80
ROLLING_WINDOW    = 252
STRATEGY_VERSION  = "52wh_v1_survivorship_10y"
CHUNK_SIZE        = 50

_PRICE_CACHE_DIR  = _ROOT / "data" / "cache" / "prices_historic"
_CACHE_MAX_AGE_H  = 23


# ════════════════════════════════════════════════════════════════════════════
#  MEMBERSHIP CHECKER
# ════════════════════════════════════════════════════════════════════════════

def load_membership_checker():
    """
    Load all index_membership intervals from DB into memory.
    Returns a function: is_member(symbol: str, date: pd.Timestamp) -> bool
    """
    with session_scope() as session:
        rows = session.query(IndexMembership).all()
        # Build: symbol -> list of (added_ts, removed_ts or NaT)
        membership: dict[str, list] = {}
        for r in rows:
            sym = r.symbol
            added   = pd.Timestamp(r.added_date)
            removed = pd.Timestamp(r.removed_date) if r.removed_date else pd.NaT
            if sym not in membership:
                membership[sym] = []
            membership[sym].append((added, removed))

    total_intervals = sum(len(v) for v in membership.values())
    logger.info(
        f"Membership loaded: {len(membership)} symbols, {total_intervals} intervals."
    )

    def is_member(ticker: str, dt: pd.Timestamp) -> bool:
        # Strip .NS suffix
        sym = ticker.replace(".NS", "").upper()
        intervals = membership.get(sym)
        if not intervals:
            return False
        for added, removed in intervals:
            if added <= dt and (pd.isna(removed) or dt <= removed):
                return True
        return False

    return is_member, membership


def get_historic_universe(membership: dict) -> list[str]:
    """Return the union of all tickers ever in the index (for bulk price download)."""
    return [f"{sym}.NS" for sym in membership]


# ════════════════════════════════════════════════════════════════════════════
#  PRICE CACHE  (mirrors engine.py, different directory)
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
    result = {}
    if raw is None or raw.empty:
        return result

    cols = raw.columns

    def _clean(s, ticker):
        s = s.dropna()
        if s.empty:
            return None
        s = s.copy()
        s.index = pd.to_datetime(s.index)
        if hasattr(s.index, "tz") and s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s

    if not isinstance(cols, pd.MultiIndex):
        if len(tickers) == 1 and "Close" in cols:
            s = _clean(raw["Close"], tickers[0])
            if s is not None:
                result[tickers[0]] = s
        return result

    l0 = set(cols.get_level_values(0))
    l1 = set(cols.get_level_values(1))

    if "Close" in l0:
        close_block = raw["Close"]
        if isinstance(close_block, pd.Series):
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
        for t in tickers:
            try:
                s = _clean(raw[t]["Close"], t)
                if s is not None:
                    result[t] = s
            except (KeyError, TypeError):
                pass
        return result

    return result


def download_all_prices(
    tickers: list,
    force_refresh: bool = False,
) -> tuple[dict, list]:
    """Download 2018-present close prices for all tickers, using historic cache."""
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
        logger.info(f"Cache hit: {len(from_cache)} tickers from disk.")
    if not to_download:
        return from_cache, []

    chunks = [to_download[i: i + CHUNK_SIZE] for i in range(0, len(to_download), CHUNK_SIZE)]
    logger.info(
        f"Downloading {len(to_download)} tickers in {len(chunks)} batches "
        f"from {LOOKBACK_START} to {end_date} ..."
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
                logger.warning(f"    No data for: {', '.join(missing)}")
                failed.extend(missing)
        except Exception as exc:
            logger.error(f"  Batch {i} failed: {exc}")
            for t in chunk:
                s = _load_cache(t)
                if s is not None:
                    downloaded[t] = s
                else:
                    failed.append(t)

    if failed:
        logger.warning(f"[!] {len(failed)} tickers with no data: {', '.join(failed[:20])}" +
                       (" ..." if len(failed) > 20 else ""))

    result = {**from_cache, **downloaded}
    logger.info(f"Prices ready: {len(result)} tickers with data, {len(failed)} failed.")
    return result, failed


# ════════════════════════════════════════════════════════════════════════════
#  SIMULATION
# ════════════════════════════════════════════════════════════════════════════

def _simulate_ticker_historic(
    ticker: str,
    close: pd.Series,
    company_name: str,
    is_member,      # Callable[[str, pd.Timestamp], bool]
) -> list:
    """
    Same logic as engine._simulate_ticker but with membership gating:
    - Signals (new entries) only generated when ticker is an index constituent.
    - Open trades continue running even if ticker is removed from index.
    """
    rolling_max = close.shift(1).rolling(ROLLING_WINDOW).max()

    mask = close.index >= BACKTEST_START
    close_bt = close[mask]
    rolling_max_bt = rolling_max[mask]

    if close_bt.empty:
        return []

    trades = []
    in_trade = False
    entry_date = entry_price = peak_close = trailing_stop = None

    for dt in close_bt.index:
        c  = close_bt[dt]
        rm = rolling_max_bt[dt]

        if pd.isna(c):
            continue

        if in_trade:
            if c > peak_close:
                peak_close    = c
                trailing_stop = peak_close * TRAILING_STOP_PCT

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
            # Only enter if the stock was in the Nifty 500 on this date
            if is_member(ticker, dt) and (not pd.isna(rm)) and (c > rm):
                in_trade      = True
                entry_date    = dt
                entry_price   = c
                peak_close    = c
                trailing_stop = c * TRAILING_STOP_PCT

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
        "ticker":                ticker,
        "company_name":          company_name,
        "entry_date":            entry_date.strftime("%Y-%m-%d"),
        "entry_price":           round(float(entry_price), 4),
        "highest_price_reached": round(float(peak_close), 4),
        "trailing_stop":         round(float(trailing_stop), 4),
        "exit_date":             exit_date.strftime("%Y-%m-%d") if exit_date else None,
        "exit_price":            round(float(exit_price), 4)    if exit_price else None,
        "holding_days":          int(holding_days),
        "return_pct":            round(float(return_pct), 4)    if return_pct is not None else None,
        "trade_year":            int(entry_date.year),
        "status":                status,
        "exit_reason":           exit_reason,
        "source":                "backtest",
        "strategy_version":      STRATEGY_VERSION,
    }
    if extra:
        rec.update(extra)
    return rec


def simulate_all_historic(
    price_data: dict,
    is_member,
    membership: dict,
) -> pd.DataFrame:
    company_map = {}
    for sym in membership:
        company_map[f"{sym}.NS"] = sym   # fallback to symbol; PDF names may differ

    all_trades = []
    tickers = list(price_data.keys())
    n = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        if i % 100 == 0 or i == n:
            logger.info(f"  Simulating {i:3d}/{n} ...")
        close = price_data[ticker]
        name  = company_map.get(ticker, ticker.replace(".NS", ""))
        all_trades.extend(_simulate_ticker_historic(ticker, close, name, is_member))

    logger.info(f"Simulation done: {len(all_trades)} trade records.")
    return pd.DataFrame(all_trades) if all_trades else pd.DataFrame()


# ════════════════════════════════════════════════════════════════════════════
#  STATS  (reused from engine.py logic, duplicated to stay self-contained)
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


def compute_stats_historic(trades_df: pd.DataFrame, membership: dict) -> tuple:
    """
    Returns (combined_stats, yearly_table, open_summary).
    yearly_table includes a 'constituents_in_universe' column showing how many
    unique tickers had membership in each backtest year.
    """
    closed = trades_df[trades_df["status"] == "closed"].copy()
    open_t = trades_df[trades_df["status"] == "open"].copy()

    combined = _group_stats(closed)
    combined["open_trades_count"] = len(open_t)
    combined["strategy_version"]  = STRATEGY_VERSION
    combined["backtest_start"]    = BACKTEST_START.strftime("%Y-%m-%d")
    if not open_t.empty and "unrealized_return_pct" in open_t.columns:
        combined["avg_unrealized_pct"] = round(float(open_t["unrealized_return_pct"].mean()), 2)

    # Count how many unique tickers had membership in each calendar year
    def _member_count_for_year(year: int) -> int:
        jan1 = pd.Timestamp(year, 1, 1)
        dec31 = pd.Timestamp(year, 12, 31)
        count = 0
        for sym, intervals in membership.items():
            for added, removed in intervals:
                # overlap with [jan1, dec31]
                if added <= dec31 and (pd.isna(removed) or removed >= jan1):
                    count += 1
                    break
        return count

    current_year = date.today().year
    rows = []
    for year in sorted(closed["trade_year"].dropna().unique()):
        yr = int(year)
        row = _group_stats(closed[closed["trade_year"] == yr])
        row["year"] = f"{yr} (YTD — partial)" if yr == current_year else str(yr)
        row["constituents_in_universe"] = _member_count_for_year(yr)
        rows.append(row)
    yearly_table = pd.DataFrame(rows) if rows else pd.DataFrame()

    open_summary = {
        "count":   len(open_t),
        "tickers": sorted(open_t["ticker"].tolist()) if not open_t.empty else [],
    }
    return combined, yearly_table, open_summary


def compute_equity_curve_historic(trades_df: pd.DataFrame) -> pd.DataFrame:
    closed = trades_df[trades_df["status"] == "closed"].copy()
    if closed.empty:
        return pd.DataFrame(columns=["exit_date", "cumulative_return_pct"])
    closed["exit_date"] = pd.to_datetime(closed["exit_date"])
    daily = (
        closed.groupby("exit_date")["return_pct"]
        .sum().sort_index().cumsum().reset_index()
    )
    daily.columns = ["exit_date", "cumulative_return_pct"]
    return daily


# ════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════════════════

def save_to_db_historic(trades_df: pd.DataFrame) -> None:
    from sqlalchemy import text

    engine = get_engine()
    Base.metadata.create_all(engine)

    with session_scope() as session:
        deleted = session.execute(
            text("DELETE FROM trades WHERE source='backtest' AND strategy_version=:sv"),
            {"sv": STRATEGY_VERSION},
        ).rowcount
        if deleted:
            logger.info(f"Cleared {deleted} previous records (strategy_version={STRATEGY_VERSION}).")

    records = trades_df.fillna(np.nan).replace({np.nan: None}).to_dict("records")
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

    logger.info(f"Saved {len(records)} historic backtest records ({STRATEGY_VERSION}).")


# ════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════

def run_historic_backtest(force_refresh: bool = False) -> tuple:
    """
    End-to-end historic backtest with survivorship correction.
    Returns (trades_df, combined_stats, yearly_table, equity_curve, failed_tickers).
    """
    is_member, membership = load_membership_checker()

    if not membership:
        raise RuntimeError(
            "index_membership table is empty. Run build_membership.py first."
        )

    tickers = get_historic_universe(membership)
    logger.info(f"Historic universe: {len(tickers)} unique tickers ever in Nifty 500.")

    price_data, failed = download_all_prices(tickers, force_refresh=force_refresh)

    logger.info("Running survivorship-corrected simulation ...")
    trades_df = simulate_all_historic(price_data, is_member, membership)

    if trades_df.empty:
        logger.error("No trades produced.")
        return trades_df, {}, pd.DataFrame(), pd.DataFrame(), failed

    combined, yearly_table, open_summary = compute_stats_historic(trades_df, membership)
    equity_curve = compute_equity_curve_historic(trades_df)

    logger.info("Saving to SQLite ...")
    save_to_db_historic(trades_df)

    return trades_df, combined, yearly_table, equity_curve, failed, open_summary
