"""
InsiderSwing — earnings calendar helper (for the earnings-proximity confound).

WHY THIS EXISTS
---------------
Insider buys filed shortly before a scheduled earnings release are more likely
to be mechanical: many issuers run trading windows that close ~2–4 weeks ahead
of a print, so trades landing in that period are disproportionately plan-driven
or window-constrained rather than discretionary conviction.

Per spec this is a DOWN-WEIGHT and a VISIBLE FLAG, not an exclusion.  The flag
travels all the way to the dashboard and to the backtest segmentation table so
the edge with and without it can be compared directly.

KNOWN LIMITATION — state it, don't hide it
------------------------------------------
yfinance returns *actual reported* earnings dates, not the date that was on the
calendar at the time of the filing.  Companies occasionally move a print by a
few days after announcing it.  So this flag is a near-point-in-time
approximation: it can differ from what a trader saw by a few days around the
boundary.  It is deliberately used only as a soft multiplier on the score and
as a reporting bucket — never as an entry/exit condition — so the approximation
cannot leak into simulated returns.

When no calendar is available the flag is False and ``earnings_date`` is
recorded as 'not verified'.  Nothing is ever guessed.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402

logger = logging.getLogger(__name__)

_CACHE_DIR = cfg.CACHE_DIR / "earnings"
_MEM: dict[str, Optional[pd.DatetimeIndex]] = {}


def _cache_path(ticker: str) -> Path:
    safe = ticker.replace(".", "_").replace("/", "_").replace("-", "_")
    return _CACHE_DIR / f"{safe}.parquet"


def load_earnings_dates(ticker: str, refresh: bool = False) -> Optional[pd.DatetimeIndex]:
    """
    All known earnings dates for a ticker, sorted ascending.

    Cached to disk indefinitely (historical earnings dates don't change), with
    ``refresh=True`` to re-pull.  Returns None when unavailable — the caller
    must treat that as 'not verified', never as 'no earnings nearby'.
    """
    key = ticker.upper()
    if not refresh and key in _MEM:
        return _MEM[key]

    path = _cache_path(key)
    if not refresh and path.exists():
        try:
            df = pd.read_parquet(path)
            idx = pd.DatetimeIndex(pd.to_datetime(df["earnings_date"])).sort_values()
            _MEM[key] = idx
            return idx
        except Exception as exc:
            logger.debug("Earnings cache read failed for %s: %s", key, exc)

    idx: Optional[pd.DatetimeIndex] = None
    try:
        import yfinance as yf
        raw = yf.Ticker(key).get_earnings_dates(limit=200)
        if raw is not None and len(raw) > 0:
            dates = pd.to_datetime(pd.Series(raw.index), errors="coerce", utc=True).dropna()
            if len(dates):
                idx = pd.DatetimeIndex(dates.dt.tz_localize(None)).sort_values()
    except Exception as exc:
        logger.debug("Earnings lookup failed for %s: %s", key, exc)

    if idx is not None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            pd.DataFrame({"earnings_date": idx}).to_parquet(path)
        except Exception as exc:
            logger.debug("Earnings cache write failed for %s: %s", key, exc)

    _MEM[key] = idx
    return idx


def next_earnings_after(ticker: str, as_of: date) -> Optional[date]:
    """Nearest known earnings date strictly on/after ``as_of``, or None."""
    idx = load_earnings_dates(ticker)
    if idx is None or len(idx) == 0:
        return None
    cutoff = pd.Timestamp(as_of)
    future = idx[idx >= cutoff]
    if len(future) == 0:
        return None
    return future[0].date()


def proximity_flag(
    ticker: str,
    as_of: date,
    window_days: Optional[int] = None,
) -> tuple[Optional[str], bool]:
    """
    (earnings_date_str, flag).

    flag is True when a scheduled print falls within ``window_days`` calendar
    days after ``as_of``.  ('not verified', False) when no calendar is available
    — an unknown is not evidence of absence, and the report says how many scores
    fell into that state.
    """
    win = window_days if window_days is not None else cfg.DEFAULT_CONFIG.earnings_proximity_days
    nxt = next_earnings_after(ticker, as_of)
    if nxt is None:
        return "not verified", False
    return nxt.isoformat(), (nxt - as_of).days <= win


def prefetch(tickers: list[str], refresh: bool = False) -> dict[str, bool]:
    """
    Warm the cache for a whole universe before a backtest.

    Returns ticker → availability, so the run can report honest coverage
    ("earnings calendar available for 431/503 names") instead of pretending the
    confound check ran everywhere.
    """
    out: dict[str, bool] = {}
    for i, t in enumerate(tickers, 1):
        out[t] = load_earnings_dates(t, refresh=refresh) is not None
        if i % 50 == 0:
            logger.info("Earnings prefetch: %d/%d", i, len(tickers))
    have = sum(out.values())
    logger.info("Earnings calendar available for %d/%d tickers (%.0f%%)",
                have, len(out), 100.0 * have / max(len(out), 1))
    return out
