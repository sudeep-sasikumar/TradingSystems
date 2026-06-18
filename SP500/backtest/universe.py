"""
SP500/backtest/universe.py — Historical S&P 500 membership

Primary source: fja05680/sp500 (GitHub)
URL: https://raw.githubusercontent.com/fja05680/sp500/master/
     S%26P%20500%20Historical%20Components%20%26%20Changes.csv

Format:
  One row per index-change event. The 'tickers' column lists ALL historical
  members: tickers WITHOUT a suffix are still current; tickers with -YYYYMM
  suffix were removed in that month.

Coverage: 1996-03-25 to present. Backtest window: COVERAGE_START = 2006-01-01.

Writes to: sp500_membership table in trading.db.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

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

GITHUB_CSV_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes.csv"
)
CACHE_PATH = _ROOT / "data" / "cache" / "sp500_constituents.csv"


# ── Download ───────────────────────────────────────────────────────────────────

def _download_constituents_csv(force_refresh: bool = False) -> Path:
    """Download or use weekly-cached fja05680 CSV."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and not force_refresh:
        age_days = (
            datetime.now() - datetime.fromtimestamp(CACHE_PATH.stat().st_mtime)
        ).days
        if age_days < 7:
            logger.info("Using cached S&P 500 constituents CSV (age: %d days).", age_days)
            return CACHE_PATH

    logger.info("Downloading S&P 500 historical constituents from GitHub...")
    resp = requests.get(
        GITHUB_CSV_URL,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    CACHE_PATH.write_bytes(resp.content)
    logger.info("Downloaded %d bytes -> %s", len(resp.content), CACHE_PATH)
    return CACHE_PATH


# ── Parse ──────────────────────────────────────────────────────────────────────

def _parse_ticker_list(tickers_str: str) -> list[tuple[str, date | None]]:
    """
    Parse 'AAPL,TMC-200006,BRK.B,GE-201812' into
    [(ticker, removal_date_or_None), ...].

    Removal suffix: TICKER-YYYYMM — exactly 6 digits at end of token.
    Tickers with dots (BRK.B, BF.B) and hyphens (BRK-B) are handled;
    the regex anchors the 6-digit suffix to distinguish BRK-B (class) from
    BRK-200006 (removal date).
    """
    result = []
    for token in tickers_str.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"^(.*)-(\d{6})$", token)
        if m:
            ticker = m.group(1).upper()
            yyyymm = m.group(2)
            year, month = int(yyyymm[:4]), int(yyyymm[4:])
            try:
                removal = date(year, month, 1)
            except ValueError:
                removal = None
            result.append((ticker, removal))
        else:
            result.append((token.upper(), None))
    return result


def _build_membership_intervals(csv_path: Path) -> pd.DataFrame:
    """
    Convert the fja05680 date-indexed CSV to (ticker, added_date, removed_date) rows.

    Algorithm:
      - Process rows chronologically.
      - A ticker's added_date = date of its FIRST appearance in any row.
      - A ticker's removed_date = the month encoded in its -YYYYMM suffix
        (earliest such suffix if the ticker appears with conflicting ones).
      - Tickers in the first row (1996-03-25) have date_quality='baseline'
        because they were S&P 500 members before our data begins.
    """
    logger.info("Parsing S&P 500 constituent CSV...")
    df_raw = pd.read_csv(csv_path, header=0, dtype=str)
    df_raw.columns = [c.strip().lower() for c in df_raw.columns]

    # Normalise column names: fja05680 uses 'date' and 'tickers'
    if "date" not in df_raw.columns or "tickers" not in df_raw.columns:
        if len(df_raw.columns) >= 2:
            df_raw.columns = ["date", "tickers"] + list(df_raw.columns[2:])
        else:
            raise ValueError(
                f"Cannot identify date/tickers columns. Found: {df_raw.columns.tolist()}"
            )

    df_raw["date"] = pd.to_datetime(df_raw["date"]).dt.date
    df_raw = df_raw.sort_values("date").reset_index(drop=True)

    baseline_date: date = df_raw["date"].iloc[0]
    logger.info(
        "  Rows: %d  |  date range: %s -> %s",
        len(df_raw), df_raw["date"].iloc[0], df_raw["date"].iloc[-1],
    )

    ticker_first_seen: dict[str, date] = {}
    ticker_removed:    dict[str, date] = {}

    for _, row in df_raw.iterrows():
        row_date   = row["date"]
        ticker_str = str(row.get("tickers", ""))

        for ticker, removal in _parse_ticker_list(ticker_str):
            if not ticker:
                continue
            if ticker not in ticker_first_seen:
                ticker_first_seen[ticker] = row_date
            if removal is not None:
                # Keep the earliest removal date if seen more than once
                if ticker not in ticker_removed or removal < ticker_removed[ticker]:
                    ticker_removed[ticker] = removal

    records = []
    for ticker, first_seen in ticker_first_seen.items():
        removal = ticker_removed.get(ticker)
        records.append({
            "ticker":       ticker,
            "added_date":   str(first_seen),
            "removed_date": str(removal) if removal else None,
            "date_quality": "baseline" if first_seen == baseline_date else "confirmed",
            "source":       "fja05680_sp500_github",
        })

    result = pd.DataFrame(records)
    logger.info("  Parsed %d unique tickers.", len(result))
    return result


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

    print("\n" + "=" * 60)
    print("  S&P 500 CONSTITUENT COVERAGE REPORT")
    print("=" * 60)
    print(f"  Total unique tickers in history:  {len(intervals_df)}")
    print()
    for yr in [2006, 2010, 2015, 2020, 2025]:
        d = f"{yr}-01-01"
        print(f"  Members on {d}:  {_count_on(d):>4}")
    print(f"  Members today  ({today_str}):  {_count_on(today_str):>4}")
    print()

    cov_str = str(COVERAGE_START)
    n_cov = _count_on(cov_str)
    total = len(intervals_df)
    removed_before_coverage = int(
        (intervals_df["removed_date"].notna() &
         (intervals_df["removed_date"] < cov_str)).sum()
    )
    print(f"  Members on COVERAGE_START ({cov_str}):  {n_cov}")
    print(f"  Tickers removed before {cov_str}:  {removed_before_coverage}  (excluded from backtest)")
    print(f"  Tickers relevant to backtest:  {total - removed_before_coverage}")
    print("=" * 60 + "\n")


# ── DB persistence ─────────────────────────────────────────────────────────────

def build_sp500_membership(force_refresh: bool = False) -> pd.DataFrame:
    """
    Download, parse, persist S&P 500 membership intervals.
    Clears and re-populates the sp500_membership table on each run.
    Returns the intervals DataFrame.
    """
    csv_path = _download_constituents_csv(force_refresh=force_refresh)
    intervals_df = _build_membership_intervals(csv_path)
    _coverage_report(intervals_df)

    engine = get_engine()
    Base.metadata.create_all(engine)

    from sqlalchemy import text

    with session_scope() as session:
        deleted = session.execute(
            text("DELETE FROM sp500_membership")
        ).rowcount
        if deleted:
            logger.info("Cleared %d previous sp500_membership records.", deleted)

    with session_scope() as session:
        for _, row in intervals_df.iterrows():
            session.add(Sp500Membership(
                ticker       = row["ticker"],
                added_date   = row["added_date"],
                removed_date = row.get("removed_date"),
                date_quality = row.get("date_quality", "confirmed"),
                source       = row.get("source", "fja05680_sp500_github"),
            ))

    logger.info("Wrote %d rows to sp500_membership.", len(intervals_df))
    return intervals_df


# ── Queries ────────────────────────────────────────────────────────────────────

def get_backtest_tickers() -> list[str]:
    """
    All tickers that were S&P 500 members at any point in the backtest window
    (COVERAGE_START to today). These are the tickers to download prices for.
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


def load_all_membership_periods() -> dict[str, list[tuple[date, date | None]]]:
    """
    Load all (added_date, removed_date) intervals from DB into a dict.
    {ticker: [(added, removed_or_None), ...]}
    Pre-converts dates so the simulation loop does no string parsing.
    """
    from sqlalchemy import text

    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT ticker, added_date, removed_date FROM sp500_membership"
        )).fetchall()

    result: dict[str, list[tuple[date, date | None]]] = {}
    for r in rows:
        ticker  = r[0]
        added   = date.fromisoformat(r[1])
        removed = date.fromisoformat(r[2]) if r[2] else None
        result.setdefault(ticker, []).append((added, removed))

    return result
