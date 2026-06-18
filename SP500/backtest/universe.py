"""
SP500/backtest/universe.py — Historical S&P 500 membership

Primary source: fja05680/sp500 (GitHub)
  sp500_ticker_start_end.csv  — pre-computed (ticker, start_date, end_date) for every
                                 membership interval from 1996 to current (~2026-06-02).
                                 2,588 rows, covers re-additions as separate rows.

Supplement source (future-proofing):
  sp500_changes_since_2019.csv — (date, add, remove) event log, 2019 to ~2026-06-02.
                                   Applied for any change events strictly after the latest
                                   end_date already captured in ticker_start_end.

Both files are maintained by fja05680 via Wikipedia-sourced S&P change announcements.

Writes to: sp500_membership table in trading.db (cleared + repopulated on each run).
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

_HERE = Path(__file__).resolve().parent          # SP500/backtest
_ROOT = _HERE.parent.parent                      # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import session_scope, get_engine
from shared.models import Base, Sp500Membership

logger = logging.getLogger(__name__)

COVERAGE_START = date(2006, 1, 1)
LOOKBACK_START = date(2005, 1, 1)   # extra year for 252-day warm-up

_GITHUB_BASE = "https://raw.githubusercontent.com/fja05680/sp500/master"
TICKER_START_END_URL = f"{_GITHUB_BASE}/sp500_ticker_start_end.csv"
CHANGES_2019_URL     = f"{_GITHUB_BASE}/sp500_changes_since_2019.csv"

_CACHE_DIR          = _ROOT / "data" / "cache"
_CACHE_START_END    = _CACHE_DIR / "sp500_ticker_start_end.csv"
_CACHE_CHANGES_2019 = _CACHE_DIR / "sp500_changes_since_2019.csv"
_CACHE_MAX_DAYS     = 7     # refresh weekly


# ── Download helpers ───────────────────────────────────────────────────────────

def _download_file(url: str, cache_path: Path, force_refresh: bool = False) -> Path:
    """Download url to cache_path; skip if fresh enough."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not force_refresh:
        age_days = (
            datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        ).days
        if age_days < _CACHE_MAX_DAYS:
            logger.info("Cache hit: %s (age %d days).", cache_path.name, age_days)
            return cache_path

    logger.info("Downloading %s ...", url)
    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    logger.info("  -> %d bytes saved to %s", len(resp.content), cache_path)
    return cache_path


# ── Parsers ────────────────────────────────────────────────────────────────────

def _parse_ticker_start_end(path: Path) -> pd.DataFrame:
    """
    Parse sp500_ticker_start_end.csv.

    Format: ticker, start_date, end_date
      - One row per membership interval (re-added stocks get multiple rows: AAL appears twice)
      - Blank end_date = still current member
    Returns DataFrame with columns: ticker, start_date (str), end_date (str or None)
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = ["ticker", "start_date", "end_date"]
    df["ticker"]     = df["ticker"].str.strip().str.upper()
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["end_date"]   = df["end_date"].str.strip().replace("", None)
    # Normalise non-empty end_dates to YYYY-MM-DD
    mask = df["end_date"].notna()
    df.loc[mask, "end_date"] = (
        pd.to_datetime(df.loc[mask, "end_date"], errors="coerce")
        .dt.strftime("%Y-%m-%d")
    )
    df = df.dropna(subset=["ticker", "start_date"])
    logger.info(
        "Parsed ticker_start_end: %d intervals, %d unique tickers",
        len(df), df["ticker"].nunique(),
    )
    return df


def _parse_changes_2019(path: Path) -> pd.DataFrame:
    """
    Parse sp500_changes_since_2019.csv.

    Format: date, add, remove
      - 'add' and 'remove' are comma-separated ticker lists; may be blank.
    Returns DataFrame with date (date), add (str), remove (str).
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = ["date", "add", "remove"]
    df["date"]   = pd.to_datetime(df["date"]).dt.date
    df["add"]    = df["add"].str.strip()
    df["remove"] = df["remove"].str.strip()
    return df.sort_values("date").reset_index(drop=True)


# ── Merge ──────────────────────────────────────────────────────────────────────

def _merge_sources(
    df_periods: pd.DataFrame,
    df_changes: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build final membership intervals by:
      1. Using ticker_start_end.csv as the primary source.
      2. Applying any change events from changes_since_2019.csv that are strictly
         after the latest end_date already captured in ticker_start_end (future-proofing).

    Returns DataFrame with columns: ticker, added_date, removed_date, date_quality, source.
    """
    # Find the cutoff: latest non-null end_date in ticker_start_end
    valid_ends = pd.to_datetime(
        df_periods["end_date"].dropna(), errors="coerce"
    ).dropna()
    cutoff: date = valid_ends.max().date() if not valid_ends.empty else date.min

    # Build working dict from ticker_start_end
    membership: dict[str, list[list]] = {}   # ticker -> [[start, end], ...]
    for _, row in df_periods.iterrows():
        ticker = row["ticker"]
        start_str = row["start_date"]
        end_str   = row.get("end_date")
        try:
            start = date.fromisoformat(start_str)
        except (ValueError, TypeError):
            continue
        end = date.fromisoformat(end_str) if end_str else None
        membership.setdefault(ticker, []).append([start, end])

    # Apply any changes strictly after the cutoff (handles future repo updates)
    recent = df_changes[df_changes["date"] > cutoff]
    if not recent.empty:
        logger.info(
            "Applying %d change events after cutoff %s from changes_since_2019.csv",
            len(recent), cutoff,
        )
        for _, row in recent.iterrows():
            change_date = row["date"]

            # Additions
            for ticker in _split_tickers(row.get("add", "")):
                membership.setdefault(ticker, []).append([change_date, None])

            # Removals — close the most recent open period for each removed ticker
            for ticker in _split_tickers(row.get("remove", "")):
                if ticker in membership:
                    for interval in reversed(membership[ticker]):
                        if interval[1] is None:
                            interval[1] = change_date
                            break
                else:
                    logger.warning(
                        "REMOVE event for %s on %s: ticker not found in membership dict",
                        ticker, change_date,
                    )
    else:
        logger.info(
            "ticker_start_end.csv is current through %s — no supplemental changes needed.",
            cutoff,
        )

    # Flatten to DataFrame
    records = []
    for ticker, periods in membership.items():
        for start, end in periods:
            records.append({
                "ticker":       ticker,
                "added_date":   str(start),
                "removed_date": str(end) if end else None,
                "date_quality": "confirmed",
                "source":       "fja05680_sp500_ticker_start_end",
            })

    result = pd.DataFrame(records)
    logger.info("Final membership: %d intervals for %d unique tickers", len(result), result["ticker"].nunique())
    return result


def _split_tickers(s: str) -> list[str]:
    """Split a comma-separated ticker string into a list of clean upper-case tickers."""
    if not s or pd.isna(s):
        return []
    return [t.strip().upper() for t in str(s).split(",") if t.strip()]


# ── Coverage report ────────────────────────────────────────────────────────────

def _coverage_report(intervals_df: pd.DataFrame) -> None:
    today_str = str(date.today())

    def _count_on(d: str) -> int:
        added_ok   = intervals_df["added_date"] <= d
        removed_ok = (
            intervals_df["removed_date"].isna() |
            (intervals_df["removed_date"] > d)
        )
        return int((added_ok & removed_ok).sum())

    print("\n" + "=" * 62)
    print("  S&P 500 CONSTITUENT COVERAGE REPORT")
    print("  Source: fja05680/sp500_ticker_start_end.csv + changes_since_2019")
    print("=" * 62)
    print(f"  Total unique tickers in history:  {intervals_df['ticker'].nunique()}")
    print(f"  Total membership intervals:       {len(intervals_df)}")
    print()
    for yr in [2006, 2010, 2015, 2020, 2025]:
        d = f"{yr}-01-01"
        print(f"  Members on {d}:  {_count_on(d):>4}")
    print(f"  Members today  ({today_str}):  {_count_on(today_str):>4}")
    print()

    cov_str = str(COVERAGE_START)
    n_cov   = _count_on(cov_str)
    removed_before = int(
        (intervals_df["removed_date"].notna() &
         (intervals_df["removed_date"] < cov_str)).sum()
    )
    print(f"  Members on COVERAGE_START ({cov_str}):  {n_cov}")
    print(f"  Intervals ended before {cov_str}:  {removed_before}  (excluded at entry signal time)")
    print("=" * 62 + "\n")


# ── DB persistence ─────────────────────────────────────────────────────────────

def build_sp500_membership(force_refresh: bool = False) -> pd.DataFrame:
    """
    Download, parse, merge and persist S&P 500 membership intervals.
    Clears and repopulates sp500_membership on each run.
    Returns the intervals DataFrame.
    """
    path_start_end = _download_file(TICKER_START_END_URL, _CACHE_START_END, force_refresh)
    path_changes   = _download_file(CHANGES_2019_URL,     _CACHE_CHANGES_2019, force_refresh)

    df_periods = _parse_ticker_start_end(path_start_end)
    df_changes = _parse_changes_2019(path_changes)

    intervals_df = _merge_sources(df_periods, df_changes)
    _coverage_report(intervals_df)

    engine = get_engine()
    Base.metadata.create_all(engine)

    from sqlalchemy import text

    with session_scope() as session:
        deleted = session.execute(text("DELETE FROM sp500_membership")).rowcount
        if deleted:
            logger.info("Cleared %d previous sp500_membership records.", deleted)

    with session_scope() as session:
        for _, row in intervals_df.iterrows():
            session.add(Sp500Membership(
                ticker       = row["ticker"],
                added_date   = row["added_date"],
                removed_date = row.get("removed_date"),
                date_quality = row.get("date_quality", "confirmed"),
                source       = row.get("source", "fja05680_sp500_ticker_start_end"),
            ))

    logger.info("Wrote %d rows to sp500_membership.", len(intervals_df))
    return intervals_df


# ── Queries for backtest engine ────────────────────────────────────────────────

def get_backtest_tickers() -> list[str]:
    """
    All tickers that were S&P 500 members at any point in the backtest window
    (COVERAGE_START to today). Used to determine which price series to download.
    """
    from sqlalchemy import text

    end_str   = str(date.today())
    start_str = str(COVERAGE_START)

    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT ticker FROM sp500_membership "
            "WHERE added_date <= :end "
            "  AND (removed_date IS NULL OR removed_date > :start)"
        ), {"start": start_str, "end": end_str}).fetchall()

    return [r[0] for r in rows]


def load_all_membership_periods() -> dict[str, list[tuple[date, Optional[date]]]]:
    """
    Load all (added_date, removed_date) intervals from DB into a dict.
    {ticker: [(added, removed_or_None), ...]}
    Dates pre-converted to datetime.date for fast comparison in the simulation loop.
    """
    from sqlalchemy import text

    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT ticker, added_date, removed_date FROM sp500_membership"
        )).fetchall()

    result: dict[str, list[tuple[date, Optional[date]]]] = {}
    for r in rows:
        ticker  = r[0]
        added   = date.fromisoformat(r[1])
        removed = date.fromisoformat(r[2]) if r[2] else None
        result.setdefault(ticker, []).append((added, removed))

    return result
