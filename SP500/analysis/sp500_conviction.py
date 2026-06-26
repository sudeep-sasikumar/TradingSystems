"""
S&P 500 52-Week High — Signal Conviction Tiers (CP-S6)

Conviction is advisory only — never blocks a signal. User makes the final call.

Tier rules (calibrated from sp500_52wh_v1 backtest + sp500_market_regime, 2006–2026):
    HIGH:     Bull regime (GSPC > 200-DMA) AND calm VIX (< 20).
              Score = +2.  Strongest historical environment for SP500 momentum.
              Best years: 2012 (70% win, +60% avg), 2013 (74% win, +40% avg),
              2016 (66% win, +25% avg), 2017 (59% win, +12% avg).

    AVOID:    Bear regime (GSPC <= 200-DMA) AND elevated or stressed VIX (>= 20).
              Score <= -1.  Market in downtrend + fear elevated.
              Worst years: 2008 (11% win, -15% avg), 2022 (27% win, -0.5% avg).

    STANDARD: All other combinations (score 0 or 1):
              - Bull + elevated VIX (20–25):  market up, mild uncertainty
              - Bull + stressed VIX (>= 25):  market up, fear spike
              - Bear + calm VIX (< 20):       very rare, ambiguous

Scoring:
    Market:  bull = +1  | bear  = -1  | unknown = 0
    VIX:     calm = +1  | stressed = -1 | elevated = 0

    score >= 2   → HIGH
    score <= -1  → AVOID
    score 0 or 1 → STANDARD

Source: sp500_market_regime table (populated by SP500/backtest/regime.py, CP-S4).
Falls back to STANDARD with warning if no regime data is found for the signal date.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Optional

import pandas as pd

logger = logging.getLogger("sp500_conviction")

HIGH     = "HIGH"
STANDARD = "STANDARD"
AVOID    = "AVOID"


# ── Public API ─────────────────────────────────────────────────────────────────

def get_sp500_conviction(signal_date: str) -> dict:
    """
    Look up the S&P 500 market regime for signal_date and return a conviction tier.

    Uses the closest prior date in sp500_market_regime (handles weekends + holidays).

    Args:
        signal_date: YYYY-MM-DD

    Returns:
        {
          "tier":               "HIGH" | "STANDARD" | "AVOID",
          "regime_score":       int (-2 to +2),
          "gspc_regime":        "bull" | "bear" | "unknown",
          "vix_tier":           "calm" | "elevated" | "stressed" | "unknown",
          "vix_close":          float | None,
          "gspc_dist_200dma_pct": float | None,
        }
    """
    from shared.db import get_engine

    _default = {
        "tier":               STANDARD,
        "regime_score":       0,
        "gspc_regime":        "unknown",
        "vix_tier":           "unknown",
        "vix_close":          None,
        "gspc_dist_200dma_pct": None,
    }

    engine = get_engine()

    with suppress(Exception):
        df = pd.read_sql(
            "SELECT gspc_regime, vix_tier, vix_close, gspc_dist_200dma_pct "
            "FROM sp500_market_regime "
            "WHERE date <= :d ORDER BY date DESC LIMIT 1",
            engine,
            params={"d": signal_date},
        )
        if df.empty:
            logger.warning("No regime data found for date <= %s — defaulting to STANDARD", signal_date)
            return _default

        row          = df.iloc[0]
        gspc_regime  = str(row.get("gspc_regime") or "unknown")
        vix_tier     = str(row.get("vix_tier")    or "unknown")
        vix_close    = float(row["vix_close"])           if pd.notna(row.get("vix_close"))           else None
        dist_200     = float(row["gspc_dist_200dma_pct"]) if pd.notna(row.get("gspc_dist_200dma_pct")) else None

        tier, score  = _assign_tier(gspc_regime, vix_tier)

        logger.debug(
            "SP500 conviction for %s: regime=%s vix=%s score=%d tier=%s",
            signal_date, gspc_regime, vix_tier, score, tier,
        )

        return {
            "tier":               tier,
            "regime_score":       score,
            "gspc_regime":        gspc_regime,
            "vix_tier":           vix_tier,
            "vix_close":          vix_close,
            "gspc_dist_200dma_pct": dist_200,
        }

    logger.warning("Exception while querying regime for %s — defaulting to STANDARD", signal_date)
    return _default


# ── Tier assignment ────────────────────────────────────────────────────────────

def _assign_tier(gspc_regime: str, vix_tier: str) -> tuple[str, int]:
    """
    Maps (gspc_regime, vix_tier) → (tier, regime_score).

    Score components:
        Market:  bull = +1 | bear = -1 | unknown = 0
        VIX:     calm = +1 | stressed = -1 | elevated = 0

    Tier boundaries:
        score >= 2  → HIGH
        score <= -1 → AVOID
        else        → STANDARD
    """
    market_score = 1 if gspc_regime == "bull" else (-1 if gspc_regime == "bear" else 0)
    vix_score    = 1 if vix_tier == "calm"    else (-1 if vix_tier == "stressed" else 0)
    score        = market_score + vix_score

    if score >= 2:
        return HIGH, score
    if score <= -1:
        return AVOID, score
    return STANDARD, score
