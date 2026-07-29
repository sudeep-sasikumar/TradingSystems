"""
InsiderSwing — point-in-time universe and liquidity screen.

SURVIVORSHIP BIAS — how it is handled here
------------------------------------------
The repo already paid for this lesson once: the first Nifty and S&P 500
backtests projected *today's* index membership backwards, which quietly deleted
every company that failed, was acquired, or was demoted.  Checkpoint 7 / CP-S2
fixed it by building time-varying membership tables.

This module reuses ``sp500_membership`` in trading.db directly (1,202 tickers
back to 1996, including names that have since been removed) rather than
rebuilding it.  A ticker is in the universe on date D if:

    added_date <= D AND (removed_date IS NULL OR removed_date > D)

Delisted names therefore appear in the universe for the period they were
genuinely in the index, and their trades are simulated and then closed with
``exit_reason='delisted'`` when the price series ends — the same convention
SP500/backtest/engine.py uses.

A second, optional universe layer is the user's own screened small-cap list, read
from ``data/cache/insider_extra_universe.csv``.  That list is NOT survivorship
corrected — it is whatever the user screened today — so it is tagged
``point_in_time=False`` and the backtest report says so explicitly rather than
blending it into the headline number silently.

LIQUIDITY
---------
Small-cap insider clusters are worthless if the name can't be filled.  The
screen is average dollar volume over ``adv_lookback_days``, evaluated
point-in-time on the signal date (never on today's liquidity).
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE), str(_HERE / "sources")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg   # noqa: E402

logger = logging.getLogger(__name__)

EXTRA_UNIVERSE_CSV = cfg.DATA_DIR / "cache" / "insider_extra_universe.csv"


# ──────────────────────────────────────────────────────────────────────────────
#  Membership
# ──────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_sp500_membership() -> pd.DataFrame:
    """
    Time-varying S&P 500 membership from the main trading.db (read-only).

    Returns columns [ticker, company_name, added_date, removed_date, date_quality].
    Empty DataFrame (with a loud warning) if CP-S2 has not been run yet.
    """
    from db import main_db_read_sql

    try:
        df = main_db_read_sql(
            "SELECT ticker, company_name, added_date, removed_date, date_quality "
            "FROM sp500_membership"
        )
    except Exception as exc:
        logger.error(
            "Could not read sp500_membership from trading.db (%s). "
            "Run: python SP500/run_sp500_backtest.py --checkpoint membership", exc
        )
        return pd.DataFrame(columns=["ticker", "company_name", "added_date",
                                     "removed_date", "date_quality"])

    if df.empty:
        logger.warning(
            "sp500_membership is empty — the universe will be survivorship-BIASED. "
            "Run CP-S2 (SP500/run_sp500_backtest.py --checkpoint membership) first."
        )
        return df

    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    logger.info(
        "S&P 500 membership: %d intervals, %d unique tickers (%d currently in index)",
        len(df), df["ticker"].nunique(), int(df["removed_date"].isna().sum()),
    )
    return df


def load_extra_universe() -> pd.DataFrame:
    """
    Optional user-screened extra names, e.g. a small-cap list.

    CSV needs a ``ticker`` column; ``company_name``, ``added_date`` and
    ``removed_date`` are optional.  Missing dates mean "assume present for the
    whole backtest", which is a survivorship assumption and is reported as one.
    """
    if not EXTRA_UNIVERSE_CSV.exists():
        return pd.DataFrame(columns=["ticker", "company_name", "added_date", "removed_date"])

    try:
        df = pd.read_csv(EXTRA_UNIVERSE_CSV)
    except Exception as exc:
        logger.error("Failed to read %s: %s", EXTRA_UNIVERSE_CSV, exc)
        return pd.DataFrame(columns=["ticker", "company_name", "added_date", "removed_date"])

    cols = {c.lower().strip(): c for c in df.columns}
    if "ticker" not in cols:
        logger.error("%s has no 'ticker' column — ignoring", EXTRA_UNIVERSE_CSV)
        return pd.DataFrame(columns=["ticker", "company_name", "added_date", "removed_date"])

    out = pd.DataFrame({"ticker": df[cols["ticker"]].astype(str).str.upper().str.strip()})
    out["company_name"] = df[cols["company_name"]] if "company_name" in cols else None
    out["added_date"] = df[cols["added_date"]] if "added_date" in cols else None
    out["removed_date"] = df[cols["removed_date"]] if "removed_date" in cols else None
    out = out[out["ticker"].str.len() > 0].drop_duplicates("ticker")

    logger.info(
        "Extra universe: %d tickers from %s — NOT survivorship corrected "
        "(no point-in-time membership history available for a hand-screened list)",
        len(out), EXTRA_UNIVERSE_CSV.name,
    )
    return out


def universe_on_date(as_of: date, include_extra: bool = True) -> set[str]:
    """Tickers that were genuinely in the universe on ``as_of``."""
    d = as_of.isoformat() if isinstance(as_of, date) else str(as_of)[:10]

    members = load_sp500_membership()
    tickers: set[str] = set()
    if not members.empty:
        mask = (members["added_date"] <= d) & (
            members["removed_date"].isna() | (members["removed_date"] > d)
        )
        tickers |= set(members.loc[mask, "ticker"])

    if include_extra:
        extra = load_extra_universe()
        for row in extra.itertuples(index=False):
            add = row.added_date if isinstance(row.added_date, str) else None
            rem = row.removed_date if isinstance(row.removed_date, str) else None
            if add and add > d:
                continue
            if rem and rem <= d:
                continue
            tickers.add(row.ticker)

    return tickers


def full_universe(start: date, end: date, include_extra: bool = True) -> pd.DataFrame:
    """
    Every ticker that was in the universe at ANY point in [start, end], with the
    interval(s) it was present.  This is the set to ingest Form 4 data for —
    ingesting only current members would reintroduce the survivorship bias one
    layer up, in the data itself.
    """
    s, e = start.isoformat(), end.isoformat()
    frames = []

    members = load_sp500_membership()
    if not members.empty:
        mask = (members["added_date"] <= e) & (
            members["removed_date"].isna() | (members["removed_date"] > s)
        )
        sub = members.loc[mask, ["ticker", "company_name", "added_date", "removed_date"]].copy()
        sub["point_in_time"] = True
        sub["universe_source"] = "sp500_membership"
        frames.append(sub)

    if include_extra:
        extra = load_extra_universe()
        if not extra.empty:
            sub = extra.copy()
            sub["point_in_time"] = False
            sub["universe_source"] = "extra_csv"
            frames.append(sub)

    if not frames:
        return pd.DataFrame(columns=["ticker", "company_name", "added_date",
                                     "removed_date", "point_in_time", "universe_source"])

    out = pd.concat(frames, ignore_index=True)
    out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  CIK mapping
# ──────────────────────────────────────────────────────────────────────────────

def ticker_cik_map(tickers: Optional[set[str]] = None) -> dict[str, str]:
    """
    ticker → 10-digit CIK, from SEC's own company_tickers.json.

    Delisted names are frequently absent from that file (it lists current
    registrants), so coverage is reported, not assumed.  For any ticker without
    a CIK, EDGAR per-issuer discovery is impossible and the daily-index path
    must be used instead.
    """
    from edgar_source import EdgarSource

    src = EdgarSource()
    if tickers is None:
        return src.ticker_to_cik()

    want = sorted({t.upper() for t in tickers})
    out: dict[str, str] = {}
    for t in want:
        cik = src.resolve_cik(t)
        if cik:
            out[t] = cik
    missing = sorted(set(want) - set(out))
    if missing:
        logger.warning(
            "No SEC CIK for %d/%d tickers (typically delisted or renamed): %s%s",
            len(missing), len(want), ", ".join(missing[:15]),
            " ..." if len(missing) > 15 else "",
        )
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  Liquidity
# ──────────────────────────────────────────────────────────────────────────────

def avg_dollar_volume(price_df: pd.DataFrame, as_of: date, lookback: int = 20) -> Optional[float]:
    """
    Average dollar volume over the ``lookback`` sessions ENDING on ``as_of``.

    Point-in-time by construction: rows after ``as_of`` are sliced off before
    the mean is taken, so a name that became liquid later doesn't qualify early.
    """
    if price_df is None or price_df.empty:
        return None
    if not {"Close", "Volume"}.issubset(price_df.columns):
        return None

    window = price_df.loc[price_df.index <= pd.Timestamp(as_of)].tail(lookback)
    if window.empty:
        return None
    dollar = (window["Close"] * window["Volume"]).dropna()
    if dollar.empty:
        return None
    return float(dollar.mean())


def passes_liquidity(
    price_df: pd.DataFrame,
    as_of: date,
    config: Optional[cfg.InsiderConfig] = None,
) -> tuple[bool, Optional[float]]:
    """(passes, adv) against config.min_avg_dollar_volume."""
    conf = config or cfg.DEFAULT_CONFIG
    adv = avg_dollar_volume(price_df, as_of, conf.adv_lookback_days)
    if adv is None:
        return False, None
    return adv >= conf.min_avg_dollar_volume, adv


# ──────────────────────────────────────────────────────────────────────────────
#  Market-cap bucketing (segmentation reporting)
# ──────────────────────────────────────────────────────────────────────────────

_MCAP_EDGES = [
    ("micro", 300e6),
    ("small", 2e9),
    ("mid", 10e9),
    ("large", float("inf")),
]


def mcap_bucket(market_cap: Optional[float]) -> str:
    """
    Standard US market-cap bands.  ``unknown`` when we have no shares-outstanding
    figure — reported as its own bucket rather than lumped into 'large', because
    silently defaulting would bias the segmentation table.
    """
    if market_cap is None or not (market_cap > 0):
        return "unknown"
    for name, edge in _MCAP_EDGES:
        if market_cap < edge:
            return name
    return "large"


def estimate_market_cap(
    price_df: pd.DataFrame,
    as_of: date,
    shares_outstanding: Optional[float],
) -> Optional[float]:
    """
    Point-in-time market cap = close on ``as_of`` × shares outstanding.

    ``shares_outstanding`` is today's figure (yfinance gives no history), so this
    is an approximation that drifts for companies with heavy buybacks or
    issuance.  It is used ONLY for reporting segmentation, never for entry
    decisions, so the drift cannot leak into returns.
    """
    if shares_outstanding is None or not (shares_outstanding > 0):
        return None
    if price_df is None or price_df.empty or "Close" not in price_df.columns:
        return None
    window = price_df.loc[price_df.index <= pd.Timestamp(as_of)]
    if window.empty:
        return None
    close = float(window["Close"].iloc[-1])
    return close * float(shares_outstanding)
