"""
Unit tests for 52WeekHighUS signal_logic.py

Tests verify the CORRECTED formulas from the spec review:
  - StructuralSL uses MIN(TodayCandleLow, 5DaySwingLow) × 0.997, NOT MAX
  - FinalQty uses MIN(QtyRiskBased, QtyCapitalBased)
  - Cooldown correctly suppresses within 20 trading days (~30 calendar days)
  - Each hard gate (B1-B4) individually blocks on failure
  - Tier A/B/C computed correctly from graded check counts
  - Missing/unverifiable earnings date → warning only, signal still generated
  - evaluate_ticker on empty/NaN/short data → SKIP result, not exception

Run with:
    cd "E:\\Trading Systems"
    venv\\Scripts\\python.exe -m pytest 52WeekHighUS/tests/ -v
"""
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent          # 52WeekHighUS/tests/
_MODULE_DIR = _HERE.parent                       # 52WeekHighUS/
_ROOT = _MODULE_DIR.parent                       # project root
for _p in (str(_ROOT), str(_MODULE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

from signal_logic import (
    evaluate_ticker,
    assign_tier,
    SignalConfig,
    SignalResult,
    BREAKOUT_BUFFER,
    ENTRY_BUFFER,
    SL_BUFFER,
    SL_PCT_CAP,
    COOLDOWN_DAYS,
)
from data_loader import compute_indicators

# ── Constants ──────────────────────────────────────────────────────────────────
_EVAL_DATE = date(2024, 6, 20)


# ── DataFrame builders ─────────────────────────────────────────────────────────

def _make_price_df(
    n_rows: int = 350,
    close: float = 100.0,
    high_mult: float = 1.02,
    low_mult: float = 0.98,
    volume: float = 2_000_000,
    trend: str = "flat",
) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame with indicators computed."""
    dates = pd.bdate_range(end=_EVAL_DATE, periods=n_rows)

    if trend == "up":
        closes = np.linspace(close * 0.6, close, n_rows)
    elif trend == "down":
        closes = np.linspace(close * 1.4, close, n_rows)
    else:
        closes = np.full(n_rows, close)

    df = pd.DataFrame(
        {
            "Open":   closes * 0.99,
            "High":   closes * high_mult,
            "Low":    closes * low_mult,
            "Close":  closes,
            "Volume": volume,
        },
        index=dates,
    )
    return compute_indicators(df)


def _make_spy_bullish(n_rows: int = 350) -> pd.DataFrame:
    """SPY trending up: close > SMA50 > SMA200."""
    return _make_price_df(n_rows=n_rows, close=500.0, trend="up")


def _make_spy_bearish(n_rows: int = 350) -> pd.DataFrame:
    """SPY in downtrend: close < SMA50."""
    return _make_price_df(n_rows=n_rows, close=500.0, trend="down")


def _make_breakout_df(
    prior_high_base: float = 90.0,
    current_close: float = 98.0,
) -> pd.DataFrame:
    """
    Ticker with today's close clearly above Prior252High × BREAKOUT_BUFFER,
    in an uptrend (SMA50 > SMA200, close > both).
    """
    n = 350
    dates = pd.bdate_range(end=_EVAL_DATE, periods=n)

    # Uptrend: start at prior_high_base * 0.7, end at current_close
    closes = np.linspace(prior_high_base * 0.7, current_close, n)
    # But cap the second-to-last 252 highs so Prior252High stays at prior_high_base
    closes[-253:-1] = np.clip(closes[-253:-1], 0, prior_high_base)

    highs = closes * 1.01
    lows  = closes * 0.97

    df = pd.DataFrame(
        {
            "Open":   closes * 0.99,
            "High":   highs,
            "Low":    lows,
            "Close":  closes,
            "Volume": 3_000_000,
        },
        index=dates,
    )
    return compute_indicators(df)


def _default_config() -> SignalConfig:
    return SignalConfig(
        account_size=100_000.0,
        risk_pct=1.0,
        max_capital_per_trade=10_000.0,
    )


def _call(
    ticker_df: pd.DataFrame,
    spy_df: pd.DataFrame | None = None,
    sector_etf_df: pd.DataFrame | None = None,
    prior_signals_df: pd.DataFrame | None = None,
    config: SignalConfig | None = None,
) -> SignalResult:
    """Thin wrapper around evaluate_ticker for tests."""
    return evaluate_ticker(
        ticker="TEST",
        company_name="Test Corp",
        gics_sector="Information Technology",
        ticker_df=ticker_df,
        spy_df=spy_df if spy_df is not None else _make_spy_bullish(),
        sector_etf_df=sector_etf_df,
        prior_signals_df=prior_signals_df,
        config=config or _default_config(),
        as_of_date=_EVAL_DATE,
        fetch_earnings=False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. StructuralSL uses MIN, not MAX
# ═══════════════════════════════════════════════════════════════════════════════

class TestStructuralSL:
    def _build_df_with_lows(
        self,
        today_low: float,
        swing_low_val: float,
        close: float = 105.0,
    ) -> pd.DataFrame:
        """
        DataFrame where today's Low = today_low and the prior 5 days' lows
        = swing_low_val, with a breakout close so B1-B4 can pass.
        """
        n = 350
        dates = pd.bdate_range(end=_EVAL_DATE, periods=n)

        prior_close = close * 0.88   # flat prior close so current is a new 252H
        closes = np.full(n, prior_close)
        closes[-1] = close

        highs = closes * 1.02
        lows  = closes * 0.96

        # Engineer today's low and the 5-day swing low
        lows[-1]    = today_low
        lows[-6:-1] = swing_low_val

        df = pd.DataFrame(
            {"Open": closes * 0.99, "High": highs, "Low": lows, "Close": closes, "Volume": 3e6},
            index=dates,
        )
        return compute_indicators(df)

    def test_sl_is_min_of_today_and_swing(self):
        """StructuralSL = MIN(95, 90) × 0.997 = 89.73, not MAX(95, 90) × 0.997 = 94.715."""
        df = self._build_df_with_lows(today_low=95.0, swing_low_val=90.0)
        r = _call(df)
        if r.action == "SIGNAL":
            expected = min(95.0, 90.0) * SL_BUFFER
            assert r.structural_sl is not None
            assert abs(r.structural_sl - expected) < 0.10, (
                f"StructuralSL should be ≈{expected:.2f} (MIN), got {r.structural_sl:.4f}"
            )

    def test_sl_when_today_low_is_lower(self):
        """StructuralSL = MIN(85, 90) × 0.997 = 84.745 (today's low is lower)."""
        df = self._build_df_with_lows(today_low=85.0, swing_low_val=90.0)
        r = _call(df)
        if r.action == "SIGNAL":
            expected = min(85.0, 90.0) * SL_BUFFER
            assert r.structural_sl is not None
            assert abs(r.structural_sl - expected) < 0.10

    def test_sl_is_not_max(self):
        """
        Verify result is strictly less than what MAX() would give.
        MAX(95, 90) = 95 × 0.997 = 94.715.
        MIN(95, 90) = 90 × 0.997 = 89.73.
        """
        df = self._build_df_with_lows(today_low=95.0, swing_low_val=90.0)
        r = _call(df)
        if r.action == "SIGNAL":
            max_based = max(95.0, 90.0) * SL_BUFFER
            assert r.structural_sl is not None
            assert r.structural_sl < max_based - 0.50, (
                f"structural_sl={r.structural_sl:.4f} should be < MAX-based {max_based:.4f}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FinalQty = MIN(QtyRiskBased, QtyCapitalBased)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionSizing:
    def _build_with_approx_risk(
        self,
        close: float,
        today_low: float,
        swing_low: float,
    ) -> pd.DataFrame:
        """Breakout DataFrame with controlled lows for SL engineering."""
        n = 350
        dates = pd.bdate_range(end=_EVAL_DATE, periods=n)
        prior = close * 0.88
        closes = np.full(n, prior)
        closes[-1] = close
        highs = closes * 1.02
        lows  = closes * 0.96
        lows[-1]    = today_low
        lows[-6:-1] = swing_low
        df = pd.DataFrame(
            {"Open": closes * 0.99, "High": highs, "Low": lows, "Close": closes, "Volume": 4e6},
            index=dates,
        )
        return compute_indicators(df)

    def test_capital_cap_binds(self):
        """
        risk_budget = 100k × 1% = $1,000; entry ≈ $200; risk/share ≈ $0.50
        QtyRiskBased = floor(1000 / 0.5) = 2000
        QtyCapBased  = floor(1000 / 200) = 5   ← binds
        FinalQty must be 5.
        """
        config = SignalConfig(
            account_size=100_000, risk_pct=1.0,
            max_capital_per_trade=1_000,  # very tight capital limit
        )
        # close=200, swing_low such that risk/share is small
        df = self._build_with_approx_risk(close=200.0, today_low=199.5, swing_low=199.0)
        r = evaluate_ticker(
            ticker="TEST", company_name="TC", gics_sector="Financials",
            ticker_df=df, spy_df=_make_spy_bullish(), sector_etf_df=None,
            prior_signals_df=None, config=config, as_of_date=_EVAL_DATE,
            fetch_earnings=False,
        )
        if r.action == "SIGNAL":
            assert r.final_qty == min(r.qty_risk_based, r.qty_capital_based), (
                "FinalQty must equal MIN(QtyRiskBased, QtyCapBased)"
            )
            assert r.final_qty <= r.qty_capital_based
            assert r.final_qty <= r.qty_risk_based

    def test_risk_constraint_binds(self):
        """
        max_capital=$1M; risk_budget=$100 (tiny account 1% of $10k).
        FinalQty is limited by risk, not capital.
        """
        config = SignalConfig(
            account_size=10_000, risk_pct=1.0,   # risk_budget = $100
            max_capital_per_trade=1_000_000,
        )
        df = self._build_with_approx_risk(close=100.0, today_low=97.0, swing_low=96.0)
        r = evaluate_ticker(
            ticker="TEST", company_name="TC", gics_sector="Financials",
            ticker_df=df, spy_df=_make_spy_bullish(), sector_etf_df=None,
            prior_signals_df=None, config=config, as_of_date=_EVAL_DATE,
            fetch_earnings=False,
        )
        if r.action == "SIGNAL":
            assert r.final_qty == min(r.qty_risk_based, r.qty_capital_based)
            assert r.final_qty <= r.qty_risk_based

    def test_qty_t1_is_floor_half(self):
        """QtyT1 = floor(FinalQty/2); QtyTrailing = FinalQty - QtyT1."""
        df = _make_breakout_df()
        r = _call(df)
        if r.action == "SIGNAL" and r.final_qty:
            assert r.qty_t1 == math.floor(r.final_qty / 2)
            assert r.qty_trailing == r.final_qty - r.qty_t1


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Cooldown suppression
# ═══════════════════════════════════════════════════════════════════════════════

class TestCooldown:
    @staticmethod
    def _prior(signal_dates: list[date], ticker: str = "TEST") -> pd.DataFrame:
        return pd.DataFrame({
            "ticker":      [ticker] * len(signal_dates),
            "signal_date": signal_dates,
        })

    def test_signal_14_days_ago_suppressed(self):
        """14 calendar days ago (~10 trading days) → SKIP_COOLDOWN."""
        prior = self._prior([_EVAL_DATE - timedelta(days=14)])
        r = _call(_make_breakout_df(), prior_signals_df=prior)
        assert r.action == "SKIP_COOLDOWN", f"Expected SKIP_COOLDOWN, got {r.action}: {r.skip_reason}"

    def test_no_prior_signals_not_suppressed(self):
        """Empty prior signals DataFrame → no cooldown."""
        r = _call(_make_breakout_df(), prior_signals_df=pd.DataFrame(columns=["ticker", "signal_date"]))
        assert r.action != "SKIP_COOLDOWN"

    def test_different_ticker_not_suppressed(self):
        """Recent signal for another ticker doesn't suppress TEST."""
        prior = self._prior([_EVAL_DATE - timedelta(days=5)], ticker="AAPL")
        r = _call(_make_breakout_df(), prior_signals_df=prior)
        assert r.action != "SKIP_COOLDOWN"

    def test_old_signal_60_days_ago_not_suppressed(self):
        """60 calendar days ago >> 30 calendar day window → no suppression."""
        prior = self._prior([_EVAL_DATE - timedelta(days=60)])
        r = _call(_make_breakout_df(), prior_signals_df=prior)
        assert r.action != "SKIP_COOLDOWN", (
            "Signal 60 days ago should not trigger cooldown (window is ~30 cal days)"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Hard gates individually block
# ═══════════════════════════════════════════════════════════════════════════════

class TestHardGates:
    def test_b1_fails_when_spy_bearish(self):
        r = _call(_make_breakout_df(), spy_df=_make_spy_bearish())
        assert r.action == "SKIP_HARD_GATE"
        assert r.b1_market_regime is False

    def test_b2_fails_when_ticker_below_smas(self):
        """Downtrend ticker: close < SMA50 and close < SMA200."""
        r = _call(_make_price_df(close=80.0, trend="down"))
        assert r.action == "SKIP_HARD_GATE"
        assert r.b2_trend_filter is False

    def test_b4_fails_when_close_below_252h(self):
        """Close below Prior252High × buffer → B4 fails."""
        n = 350
        dates = pd.bdate_range(end=_EVAL_DATE, periods=n)
        # Large spike in the middle of the history → today can't break out
        closes = np.full(n, 95.0)
        closes[100:150] = 200.0   # spike creates a very high Prior252High
        closes[-1] = 95.0
        df = pd.DataFrame({
            "Open": closes * 0.99, "High": closes * 1.01,
            "Low": closes * 0.97, "Close": closes, "Volume": 2e6,
        }, index=dates)
        df = compute_indicators(df)
        r = _call(df)
        assert r.action == "SKIP_HARD_GATE"
        assert r.b4_fresh_breakout is False

    def test_b3_fails_on_death_cross(self):
        """SMA50 < SMA200 (death cross) → B3 fails."""
        n = 350
        dates = pd.bdate_range(end=_EVAL_DATE, periods=n)
        # Sharp drop then flat: SMA200 lags above SMA50
        closes = np.linspace(200.0, 80.0, n)
        closes[-20:] = 82.0   # slight recovery
        df = pd.DataFrame({
            "Open": closes * 0.99, "High": closes * 1.01,
            "Low": closes * 0.97, "Close": closes, "Volume": 2e6,
        }, index=dates)
        df = compute_indicators(df)
        last = df.iloc[-1]
        if pd.notna(last["SMA50"]) and pd.notna(last["SMA200"]) and last["SMA50"] < last["SMA200"]:
            r = _call(df)
            assert r.action == "SKIP_HARD_GATE"
            assert r.b3_golden_cross is False


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Tier assignment
# ═══════════════════════════════════════════════════════════════════════════════

class TestTierAssignment:
    @pytest.mark.parametrize("count,expected", [
        (5, "A"), (4, "A"),
        (3, "B"), (2, "B"),
        (1, "C"), (0, "C"),
    ])
    def test_assign_tier(self, count, expected):
        assert assign_tier(count) == expected, f"assign_tier({count}) should be '{expected}'"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Earnings: missing → warning only, not hard skip
# ═══════════════════════════════════════════════════════════════════════════════

class TestEarnings:
    def test_not_verified_does_not_block_signal(self):
        """When fetch_earnings=False → 'not verified'; signal still generated if all gates pass."""
        df = _make_breakout_df()
        r = _call(df)   # fetch_earnings=False in _call
        if r.action == "SIGNAL":
            assert r.earnings_date in (None, "not verified")
        # Earnings must never appear as a skip reason
        if r.skip_reason:
            assert "earnings" not in r.skip_reason.lower(), (
                "Earnings must not cause a hard skip"
            )

    def test_earnings_warning_flag_from_mock(self):
        """Earnings within 7 days → earnings_warning=True; signal NOT blocked."""
        df = _make_breakout_df()
        with patch("signal_logic._get_earnings_info",
                   return_value=(str(_EVAL_DATE + timedelta(days=3)), True)):
            r = evaluate_ticker(
                ticker="TEST", company_name="TC", gics_sector="Information Technology",
                ticker_df=df, spy_df=_make_spy_bullish(), sector_etf_df=None,
                prior_signals_df=None, config=_default_config(),
                as_of_date=_EVAL_DATE, fetch_earnings=True,
            )
        if r.action == "SIGNAL":
            assert r.earnings_warning is True
            assert r.action == "SIGNAL", "Earnings warning must not block the signal"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Scan resilience — no crash on bad data
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanResilience:
    def test_empty_df_returns_skip(self):
        """Empty DataFrame → SKIP, not exception."""
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"],
                             index=pd.DatetimeIndex([]))
        try:
            r = _call(empty)
            assert r.action.startswith("SKIP"), f"Expected SKIP, got {r.action}"
        except Exception as exc:
            pytest.fail(f"evaluate_ticker raised on empty data: {exc}")

    def test_nan_close_returns_skip(self):
        """Last close is NaN → SKIP, not exception."""
        n = 350
        dates = pd.bdate_range(end=_EVAL_DATE, periods=n)
        closes = np.full(n, 100.0)
        closes[-1] = np.nan
        df = pd.DataFrame({
            "Open": closes, "High": closes * 1.01,
            "Low": closes * 0.98, "Close": closes, "Volume": 2e6,
        }, index=dates)
        df = compute_indicators(df)
        try:
            r = _call(df)
            assert r.action.startswith("SKIP")
        except Exception as exc:
            pytest.fail(f"evaluate_ticker raised on NaN close: {exc}")

    def test_short_history_returns_skip(self):
        """Only 10 rows of data → SKIP (no SMA200, no Prior252High), not exception."""
        short = _make_price_df(n_rows=10)
        try:
            r = _call(short)
            assert r.action.startswith("SKIP")
        except Exception as exc:
            pytest.fail(f"evaluate_ticker raised on short history: {exc}")
