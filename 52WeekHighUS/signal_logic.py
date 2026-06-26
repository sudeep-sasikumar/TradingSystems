"""
US S&P 500 Breakout System — Signal Logic (Parts B, C, D)

Implements the 10-point signal checklist exactly as specified:
  B1-B4: Hard gates (any FAIL → no signal)
  B5:    Risk/reward gate (SL% ≤ 6%, R:R(T1) ≥ 1.5, R:R(T2) ≥ 2.5)
  B6-B10: Graded checks → conviction tier A/B/C

Part C formulas (CORRECTED — match the reviewed spec, not the original framework):
  StructuralSL = MIN(TodayCandleLow, 5DaySwingLow) × 0.997  [MIN, not MAX]
  FinalQty     = MIN(QtyRiskBased, QtyCapitalBased)           [capital cap added]

IMPORTANT: live scanner always uses these corrected formulas.
Version A (buggy original) lives ONLY in the backtest comparison module
(52WeekHighUS/backtest/engine.py). Nothing here may be imported by Version A.

Named signal_logic.py (not signal.py) to avoid shadowing Python's built-in
signal module when this directory is on sys.path.
"""
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent   # 52WeekHighUS/
_ROOT = _HERE.parent                      # project root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

logger = logging.getLogger(__name__)

# ── Strategy constants ─────────────────────────────────────────────────────────
STRATEGY_VERSION        = "52whu_v1"
BREAKOUT_BUFFER         = 1.0025    # close must exceed Prior252High × this
ENTRY_BUFFER            = 1.001     # Entry = Close × this
SL_BUFFER               = 0.997     # StructuralSL = MIN(lows) × this
SL_PCT_CAP              = 6.0       # hard skip if SL% > this
RR_T1_MIN               = 1.5
RR_T2_MIN               = 2.5
T1_ATR_MULT             = 2.0
T2_ATR_MULT             = 3.5
ATR_TRAILING_MULT       = 1.0       # trailing stop trail = 1 × ATR14 (from signal date)
COOLDOWN_DAYS           = 20        # trading days — suppress repeat signals within this window
EARNINGS_WARNING_DAYS   = 7         # flag if earnings within this many calendar days

# Graded check thresholds
RVOL_THRESHOLD          = 1.5       # B8: volume ≥ 1.5 × 20-day avg
CLOSE_IN_RANGE_PCT      = 0.70      # B9: close in top 30% of H-L range (≥ 0.70)
MAX_RANGE_ATR_MULT      = 2.0       # B9: H-L range ≤ 2 × ATR14
AVG_DOLLAR_VOL_MIN      = 100e6     # B10: avg dollar volume ≥ $100M
RS3M_LOOKBACK           = 63        # trading days for 3-month return


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class SignalConfig:
    """
    Configurable parameters — read from .env via SP500_US_* vars.
    Three separate values (not the old single 'Capital' — that conflated
    account size with deployable capital, which is the original sizing bug).
    """
    account_size:          float = field(default_factory=lambda: _env_float("SP500_US_ACCOUNT_SIZE", 100_000.0))
    risk_pct:              float = field(default_factory=lambda: _env_float("SP500_US_RISK_PERCENT", 1.0))
    max_capital_per_trade: float = field(default_factory=lambda: _env_float("SP500_US_MAX_CAPITAL_PER_TRADE", 10_000.0))

    def risk_budget(self) -> float:
        return self.account_size * (self.risk_pct / 100.0)


@dataclass
class SignalResult:
    """
    Full result record for one ticker evaluation.

    action values:
      'SIGNAL'         — cleared all hard gates and risk gate; alert warranted
      'SKIP_COOLDOWN'  — within COOLDOWN_DAYS of a prior signal for this ticker
      'SKIP_HARD_GATE' — failed B1, B2, B3, or B4
      'SKIP_RISK'      — passed B1-B4 but SL% > 6% or R:R below minimums
    """
    ticker:       str
    company_name: str
    gics_sector:  str
    signal_date:  date

    action:       str
    skip_reason:  Optional[str] = None

    # B1-B4 outcome (always computed)
    b1_market_regime:  Optional[bool] = None
    b2_trend_filter:   Optional[bool] = None
    b3_golden_cross:   Optional[bool] = None
    b4_fresh_breakout: Optional[bool] = None

    # Part C levels (None if hard gates failed)
    close:          Optional[float] = None
    entry:          Optional[float] = None
    structural_sl:  Optional[float] = None
    risk_per_share: Optional[float] = None
    sl_pct:         Optional[float] = None
    t1:             Optional[float] = None
    t2:             Optional[float] = None
    rr_t1:          Optional[float] = None
    rr_t2:          Optional[float] = None
    atr14:          Optional[float] = None
    prior_252_high: Optional[float] = None

    # Position sizing
    account_size:          Optional[float] = None
    risk_pct:              Optional[float] = None
    max_capital_per_trade: Optional[float] = None
    qty_risk_based:        Optional[int]   = None
    qty_capital_based:     Optional[int]   = None
    final_qty:             Optional[int]   = None
    qty_t1:                Optional[int]   = None
    qty_trailing:          Optional[int]   = None
    capital_deployed:      Optional[float] = None
    max_loss:              Optional[float] = None

    # Graded checks (None if hard gates or risk gate failed)
    b6_sector_strength:   Optional[bool] = None
    b7_relative_strength: Optional[bool] = None
    b8_volume:            Optional[bool] = None
    b9_candle_quality:    Optional[bool] = None
    b10_liquidity:        Optional[bool] = None
    graded_checks_passed: Optional[int]  = None
    tier:                 Optional[str]  = None   # 'A' | 'B' | 'C'

    # Earnings (best-effort non-blocking)
    earnings_date:    Optional[str]  = None    # YYYY-MM-DD or 'not verified'
    earnings_warning: Optional[bool] = None

    # SPY context (logged even for skipped signals)
    spy_close:  Optional[float] = None
    spy_sma50:  Optional[float] = None
    spy_sma200: Optional[float] = None


# ── Cooldown helper ────────────────────────────────────────────────────────────

def _is_in_cooldown(
    ticker: str,
    eval_date: date,
    prior_signals_df: Optional[pd.DataFrame],
) -> bool:
    """
    Return True if a SIGNAL was generated for this ticker within COOLDOWN_DAYS
    trading days before eval_date.

    Check is against stored signal history (not recomputed rolling highs) to
    avoid ambiguity. Calendar-day approximation: 20 trading days ≈ 30 calendar
    days. Conservative: may suppress one or two extra days, which is acceptable.
    """
    if prior_signals_df is None or prior_signals_df.empty:
        return False

    ticker_signals = prior_signals_df[
        prior_signals_df["ticker"].str.upper() == ticker.upper()
    ]
    if ticker_signals.empty:
        return False

    cutoff = eval_date - timedelta(days=int(COOLDOWN_DAYS * 1.5))

    def _to_date(v) -> Optional[date]:
        if isinstance(v, date):
            return v
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    for sd in ticker_signals["signal_date"]:
        sd_date = _to_date(sd)
        if sd_date is not None and sd_date >= cutoff:
            return True
    return False


# ── Earnings helper ────────────────────────────────────────────────────────────

def _get_earnings_info(ticker: str, eval_date: date) -> tuple[str, bool]:
    """
    Best-effort earnings date via yfinance — non-blocking, no hard skip on failure.
    Returns ('YYYY-MM-DD', warning_bool) or ('not verified', False).
    Never fabricates or guesses a date.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).calendar
        if info is None:
            return "not verified", False

        # yfinance may return dict or DataFrame
        if isinstance(info, dict):
            raw = info.get("Earnings Date")
            if raw is None:
                return "not verified", False
            dates = pd.to_datetime([raw], errors="coerce")
        elif isinstance(info, pd.DataFrame):
            if "Earnings Date" not in info.columns:
                return "not verified", False
            dates = pd.to_datetime(info["Earnings Date"], errors="coerce")
        else:
            return "not verified", False

        dates = dates.dropna()
        future = [d.date() for d in dates if d.date() >= eval_date]
        if not future:
            return "not verified", False

        nearest = min(future)
        warning = (nearest - eval_date).days <= EARNINGS_WARNING_DAYS
        return str(nearest), warning

    except Exception as exc:
        logger.debug("Earnings lookup failed for %s: %s", ticker, exc)
        return "not verified", False


# ── Core signal evaluation ─────────────────────────────────────────────────────

def evaluate_ticker(
    ticker: str,
    company_name: str,
    gics_sector: str,
    ticker_df: pd.DataFrame,
    spy_df: pd.DataFrame,
    sector_etf_df: Optional[pd.DataFrame],
    prior_signals_df: Optional[pd.DataFrame],
    config: Optional[SignalConfig] = None,
    as_of_date: Optional[date] = None,
    fetch_earnings: bool = True,
) -> SignalResult:
    """
    Run the 10-point signal checklist for one ticker.

    ticker_df:       DataFrame with indicators from data_loader.compute_indicators()
    spy_df:          SPY DataFrame with indicators
    sector_etf_df:   Sector ETF DataFrame (None if sector unknown)
    prior_signals_df: Prior signals with columns [ticker, signal_date] for cooldown check
    as_of_date:      Evaluation date override (defaults to last complete bar)
    fetch_earnings:  Set False to skip yfinance earnings call (unit tests / backtest)
    """
    from data_loader import get_last_row, _last_complete_bar_date

    if config is None:
        config = SignalConfig()

    # Resolve evaluation date
    if as_of_date is None:
        as_of_date = _last_complete_bar_date(ticker_df)
    if as_of_date is None:
        return SignalResult(
            ticker=ticker, company_name=company_name, gics_sector=gics_sector,
            signal_date=date.today(), action="SKIP_HARD_GATE",
            skip_reason="no completed bar data available",
        )

    row     = get_last_row(ticker_df, as_of_date=as_of_date)
    spy_row = get_last_row(spy_df,    as_of_date=as_of_date)

    if row is None:
        return SignalResult(
            ticker=ticker, company_name=company_name, gics_sector=gics_sector,
            signal_date=as_of_date, action="SKIP_HARD_GATE",
            skip_reason="no data row for evaluation date",
        )

    def _get(row_: Optional[pd.Series], col: str) -> Optional[float]:
        if row_ is None or col not in row_.index:
            return None
        v = row_[col]
        if isinstance(v, (pd.Series, np.ndarray)):
            v = v.iloc[0] if isinstance(v, pd.Series) else v[0]
        return float(v) if v is not None and not (isinstance(v, float) and math.isnan(v)) else None

    close        = _get(row, "Close")
    high_today   = _get(row, "High")
    low_today    = _get(row, "Low")
    vol_today    = _get(row, "Volume")
    atr14        = _get(row, "ATR14")
    sma50        = _get(row, "SMA50")
    sma200       = _get(row, "SMA200")
    prior_252_h  = _get(row, "Prior252High")
    avg_vol_20   = _get(row, "AvgVol20")
    swing_low_5  = _get(row, "SwingLow5")
    rs3m         = _get(row, "RS3M")

    spy_close  = _get(spy_row, "Close")
    spy_sma50  = _get(spy_row, "SMA50")
    spy_sma200 = _get(spy_row, "SMA200")

    _base = dict(
        ticker=ticker, company_name=company_name, gics_sector=gics_sector,
        signal_date=as_of_date,
        spy_close=spy_close, spy_sma50=spy_sma50, spy_sma200=spy_sma200,
    )

    # ── Cooldown ───────────────────────────────────────────────────────────────
    if _is_in_cooldown(ticker, as_of_date, prior_signals_df):
        return SignalResult(
            **_base, action="SKIP_COOLDOWN",
            skip_reason=f"prior signal within last {COOLDOWN_DAYS} trading days",
        )

    # ── B1: Market regime ──────────────────────────────────────────────────────
    b1 = (spy_close is not None and spy_sma50 is not None and spy_close > spy_sma50)

    # ── B2: Trend filter ───────────────────────────────────────────────────────
    b2 = (
        close is not None and sma50 is not None and sma200 is not None
        and close > sma50 and close > sma200
    )

    # ── B3: Golden cross ───────────────────────────────────────────────────────
    b3 = (sma50 is not None and sma200 is not None and sma50 > sma200)

    # ── B4: Fresh 52-week breakout ─────────────────────────────────────────────
    # Prior252High = MAX(daily High) over previous 252 trading days, today excluded.
    # Breakout: today's Close > Prior252High × 1.0025
    b4 = (
        close is not None and prior_252_h is not None and prior_252_h > 0
        and close > prior_252_h * BREAKOUT_BUFFER
    )

    if not (b1 and b2 and b3 and b4):
        reasons = []
        if not b1: reasons.append(f"B1 SPY {spy_close} vs 50DMA {spy_sma50}")
        if not b2: reasons.append(f"B2 trend (close {close} SMA50 {sma50} SMA200 {sma200})")
        if not b3: reasons.append(f"B3 golden cross (SMA50 {sma50} SMA200 {sma200})")
        if not b4: reasons.append(f"B4 breakout (close {close} 252H×{BREAKOUT_BUFFER} = {prior_252_h})")
        return SignalResult(
            **_base,
            action="SKIP_HARD_GATE", skip_reason="; ".join(reasons),
            b1_market_regime=b1, b2_trend_filter=b2,
            b3_golden_cross=b3, b4_fresh_breakout=b4,
            close=close, prior_252_high=prior_252_h, atr14=atr14,
        )

    # ── B5 / Part C: Risk levels ───────────────────────────────────────────────
    if close is None or atr14 is None or low_today is None:
        return SignalResult(
            **_base, action="SKIP_RISK",
            skip_reason="missing close/ATR14/low_today for level computation",
            b1_market_regime=b1, b2_trend_filter=b2,
            b3_golden_cross=b3, b4_fresh_breakout=b4,
        )

    entry = close * ENTRY_BUFFER

    # StructuralSL = MIN(TodayCandleLow, 5DaySwingLow) × 0.997
    # MIN is conservative — picks the LOWER of the two lows.
    # MAX was the original bug; it picked the tighter (higher) level, understating risk.
    if swing_low_5 is not None:
        structural_sl = min(low_today, swing_low_5) * SL_BUFFER
    else:
        structural_sl = low_today * SL_BUFFER   # fallback: today's low only

    risk_per_share = entry - structural_sl
    if risk_per_share <= 0:
        return SignalResult(
            **_base, action="SKIP_RISK",
            skip_reason=f"RiskPerShare ≤ 0 (entry={entry:.4f} sl={structural_sl:.4f})",
            b1_market_regime=b1, b2_trend_filter=b2,
            b3_golden_cross=b3, b4_fresh_breakout=b4,
            close=close, entry=entry, structural_sl=structural_sl,
            risk_per_share=risk_per_share, atr14=atr14, prior_252_high=prior_252_h,
        )

    sl_pct = (risk_per_share / entry) * 100.0
    if sl_pct > SL_PCT_CAP:
        return SignalResult(
            **_base, action="SKIP_RISK",
            skip_reason=f"SL%={sl_pct:.2f}% exceeds {SL_PCT_CAP}% cap",
            b1_market_regime=b1, b2_trend_filter=b2,
            b3_golden_cross=b3, b4_fresh_breakout=b4,
            close=close, entry=entry, structural_sl=structural_sl,
            risk_per_share=risk_per_share, sl_pct=sl_pct, atr14=atr14,
            prior_252_high=prior_252_h,
        )

    t1    = entry + T1_ATR_MULT * atr14
    t2    = entry + T2_ATR_MULT * atr14
    rr_t1 = (t1 - entry) / risk_per_share
    rr_t2 = (t2 - entry) / risk_per_share

    if rr_t1 < RR_T1_MIN or rr_t2 < RR_T2_MIN:
        return SignalResult(
            **_base, action="SKIP_RISK",
            skip_reason=(
                f"R:R insufficient (T1={rr_t1:.2f} min {RR_T1_MIN}, T2={rr_t2:.2f} min {RR_T2_MIN})"
            ),
            b1_market_regime=b1, b2_trend_filter=b2,
            b3_golden_cross=b3, b4_fresh_breakout=b4,
            close=close, entry=entry, structural_sl=structural_sl,
            risk_per_share=risk_per_share, sl_pct=sl_pct,
            t1=t1, t2=t2, rr_t1=rr_t1, rr_t2=rr_t2,
            atr14=atr14, prior_252_high=prior_252_h,
        )

    # ── Position sizing ────────────────────────────────────────────────────────
    risk_budget      = config.risk_budget()
    qty_risk_based   = math.floor(risk_budget / risk_per_share)
    qty_cap_based    = math.floor(config.max_capital_per_trade / entry)
    final_qty        = min(qty_risk_based, qty_cap_based)   # capital cap: MIN, not just risk-based

    if final_qty <= 0:
        return SignalResult(
            **_base, action="SKIP_RISK",
            skip_reason=f"FinalQty=0 (risk_budget={risk_budget:.0f}, rps={risk_per_share:.4f})",
            b1_market_regime=b1, b2_trend_filter=b2,
            b3_golden_cross=b3, b4_fresh_breakout=b4,
            close=close, entry=entry, structural_sl=structural_sl,
            risk_per_share=risk_per_share, sl_pct=sl_pct,
            t1=t1, t2=t2, rr_t1=rr_t1, rr_t2=rr_t2, atr14=atr14,
            prior_252_high=prior_252_h,
        )

    qty_t1           = math.floor(final_qty / 2)
    qty_trailing     = final_qty - qty_t1
    capital_deployed = final_qty * entry
    max_loss         = final_qty * risk_per_share

    # ── Graded checks (B6-B10) ─────────────────────────────────────────────────

    # B6: Sector strength — sector ETF close > sector ETF 50-DMA
    b6 = False
    if sector_etf_df is not None and not sector_etf_df.empty:
        etf_row = get_last_row(sector_etf_df, as_of_date=as_of_date)
        if etf_row is not None:
            etf_c = _get(etf_row, "Close")
            etf_s = _get(etf_row, "SMA50")
            if etf_c is not None and etf_s is not None:
                b6 = etf_c > etf_s

    # B7: Relative strength — stock 3M return > SPY 3M return
    b7 = False
    if rs3m is not None and not math.isnan(rs3m):
        spy_rs3m = _get(spy_row, "RS3M")
        if spy_rs3m is not None and not math.isnan(spy_rs3m):
            b7 = rs3m > spy_rs3m

    # B8: Volume ≥ 1.5 × 20-day avg
    b8 = (
        vol_today is not None and avg_vol_20 is not None
        and avg_vol_20 > 0 and vol_today >= RVOL_THRESHOLD * avg_vol_20
    )

    # B9: Candle quality
    #   (a) close in top 30% of today's H-L range
    #   (b) today's H-L range ≤ 2 × ATR14 (not a blow-off/exhaustion candle)
    b9 = False
    if high_today is not None and low_today is not None and close is not None:
        hl_range = high_today - low_today
        if hl_range > 0:
            close_pos = (close - low_today) / hl_range
            b9 = close_pos >= CLOSE_IN_RANGE_PCT and hl_range <= MAX_RANGE_ATR_MULT * atr14

    # B10: Avg dollar volume > $100M (sanity check — virtually all S&P 500 pass)
    b10 = (
        avg_vol_20 is not None and close is not None and avg_vol_20 > 0
        and avg_vol_20 * close > AVG_DOLLAR_VOL_MIN
    )

    graded_passed = sum([b6, b7, b8, b9, b10])
    tier          = assign_tier(graded_passed)

    # ── Earnings ───────────────────────────────────────────────────────────────
    if fetch_earnings:
        earnings_date_str, earnings_warning = _get_earnings_info(ticker, as_of_date)
    else:
        earnings_date_str, earnings_warning = "not verified", False

    return SignalResult(
        **_base,
        action="SIGNAL",
        b1_market_regime=b1, b2_trend_filter=b2,
        b3_golden_cross=b3, b4_fresh_breakout=b4,
        close=close, entry=entry, structural_sl=structural_sl,
        risk_per_share=risk_per_share, sl_pct=sl_pct,
        t1=t1, t2=t2, rr_t1=rr_t1, rr_t2=rr_t2,
        atr14=atr14, prior_252_high=prior_252_h,
        account_size=config.account_size,
        risk_pct=config.risk_pct,
        max_capital_per_trade=config.max_capital_per_trade,
        qty_risk_based=qty_risk_based,
        qty_capital_based=qty_cap_based,
        final_qty=final_qty, qty_t1=qty_t1, qty_trailing=qty_trailing,
        capital_deployed=capital_deployed, max_loss=max_loss,
        b6_sector_strength=b6, b7_relative_strength=b7,
        b8_volume=b8, b9_candle_quality=b9, b10_liquidity=b10,
        graded_checks_passed=graded_passed, tier=tier,
        earnings_date=earnings_date_str, earnings_warning=earnings_warning,
    )


def assign_tier(graded_checks_passed: int) -> str:
    """
    Tier A: 4 or 5 graded checks passed.
    Tier B: 2 or 3.
    Tier C: 0 or 1.
    """
    if graded_checks_passed >= 4:
        return "A"
    if graded_checks_passed >= 2:
        return "B"
    return "C"
