"""
conviction.py -- Conviction tier classification for live scanner signals.

3-tier system (Checkpoint 8b, 2026-06):
  HIGH     : market 6M trailing return in bottom-2 quintiles
             AND synthetic sector basket above its 200-DMA
  AVOID    : market 6M trailing return in strong_uptrend quintile
  STANDARD : everything else
             (includes cases where basket data is unavailable -- we do not
              inflate conviction when we can't confirm the basket condition)

Additive regime score (stored for longitudinal tracking, not used to set tier):
  +1  market below 200-DMA
  +1  market 6M in bottom-2 quintiles (strong_downtrend | moderate_downtrend)
  +1  synthetic basket above 200-DMA  (0 when basket unavailable)
  -1  market above 200-DMA AND strong_uptrend (penalty)
  Range: -1 to +3

NOTE: Revisit finer-grained tiers (beyond HIGH / STANDARD / AVOID) once
12-18 months of live tier-tagged signals are available with >=50 trades per tier
to revalidate. Current Checkpoint 8b sample sizes do not justify a finer scale.
See PROJECT_STATUS.md Checkpoint 8b note.

Data sources (all cached with 23-hour TTL in data/cache/regime/):
  Market regime : ^CRSLDX (Nifty 500) via load_market_regime()
  Basket regime : equal-weighted industry baskets via build_synthetic_baskets()
  Ticker map    : nifty500_baseline_20200725.csv via load_baseline_industries()
"""

from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from analysis.regime_data import (
    load_market_regime,
    build_synthetic_baskets,
    load_baseline_industries,
    regime_at_date,
)

logger = logging.getLogger(__name__)

# Quintile labels that count as "bottom-2" (weak market)
_BOTTOM2 = frozenset(["strong_downtrend", "moderate_downtrend"])


@lru_cache(maxsize=1)
def _ticker_to_industry() -> dict[str, str]:
    """
    Build reverse map {yfinance_ticker -> industry} from baseline CSV.
    Cached for the process lifetime (baseline CSV is static).
    """
    try:
        industries = load_baseline_industries()
        return {ticker: ind for ind, tickers in industries.items() for ticker in tickers}
    except Exception as e:
        logger.warning("conviction: could not build ticker-to-industry map: %s", e)
        return {}


def get_signal_conviction(ticker: str, signal_date: str | None = None) -> dict:
    """
    Classify a live signal into a conviction tier.

    Args:
        ticker      : Yahoo Finance ticker (e.g. "RELIANCE.NS")
        signal_date : YYYY-MM-DD string; defaults to today if not provided

    Returns:
        {
            "tier":                 "HIGH" | "STANDARD" | "AVOID",
            "score":                int (-1 to 3),
            "market_6m_quintile":   str | None,
            "market_vs_200dma":     str | None,
            "synthetic_vs_200dma":  str | None,
        }
    Falls back to tier="STANDARD", score=None on any data failure.
    """
    if signal_date is None:
        signal_date = date.today().isoformat()

    _fallback = {
        "tier": "STANDARD", "score": None,
        "market_6m_quintile": None, "market_vs_200dma": None,
        "synthetic_vs_200dma": None,
    }

    # ── Market regime ─────────────────────────────────────────────────────────
    try:
        market_df, _ = load_market_regime()
        mkt = regime_at_date(market_df, signal_date)
    except Exception as e:
        logger.warning("conviction: market regime unavailable for %s: %s", ticker, e)
        return _fallback

    mkt_q    = mkt.get("quintile_6m")
    mkt_dma  = mkt.get("vs_200dma")

    if mkt_q is None:
        logger.debug("conviction: market quintile unknown for %s on %s", ticker, signal_date)
        return _fallback

    # ── Synthetic basket ──────────────────────────────────────────────────────
    syn_dma: str | None = None
    try:
        t2i = _ticker_to_industry()
        industry = t2i.get(ticker)
        if industry:
            baskets = build_synthetic_baskets()
            basket_entry = baskets.get(industry)
            if basket_entry is not None:
                bdf, _ = basket_entry
                syn = regime_at_date(bdf, signal_date)
                syn_dma = syn.get("vs_200dma")
    except Exception as e:
        logger.debug("conviction: basket lookup failed for %s: %s", ticker, e)

    # ── Score ─────────────────────────────────────────────────────────────────
    score = 0
    if mkt_dma == "below_200dma":
        score += 1
    if mkt_q in _BOTTOM2:
        score += 1
    if syn_dma == "above_200dma":
        score += 1
    if mkt_dma == "above_200dma" and mkt_q == "strong_uptrend":
        score -= 1

    # ── Tier ──────────────────────────────────────────────────────────────────
    if mkt_q == "strong_uptrend":
        tier = "AVOID"
    elif mkt_q in _BOTTOM2 and syn_dma == "above_200dma":
        tier = "HIGH"
    else:
        tier = "STANDARD"

    logger.debug(
        "conviction: %s on %s -> tier=%s score=%d mkt_q=%s mkt_dma=%s syn_dma=%s",
        ticker, signal_date, tier, score, mkt_q, mkt_dma, syn_dma,
    )

    return {
        "tier":                tier,
        "score":               score,
        "market_6m_quintile":  mkt_q,
        "market_vs_200dma":    mkt_dma,
        "synthetic_vs_200dma": syn_dma,
    }
