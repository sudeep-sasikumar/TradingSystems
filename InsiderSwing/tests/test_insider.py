"""
InsiderSwing unit tests.

Focused on the things that fail SILENTLY — a wrong number that still produces a
plausible-looking backtest. Specifically:

  * the noise filter keeping something it should discard (a grant read as a buy)
  * the point-in-time contract leaking (a window including future filings, or an
    insider's trailing average including the trade being measured)
  * the stop floor not engaging (microscopic risk-per-share inflating R)
  * the intrabar convention flipping (target assumed to fill before the stop)
  * slippage not scaling with participation

Run:  pytest InsiderSwing/tests/ -q
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_ROOT = _PKG.parent
for _p in (str(_ROOT), str(_PKG), str(_PKG / "sources"), str(_PKG / "backtest")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg          # noqa: E402
import filters                # noqa: E402
import prices as price_mod    # noqa: E402
import risk                   # noqa: E402
import technical              # noqa: E402
from base import classify_role   # noqa: E402


def _insider_module(name: str):
    """
    Import an InsiderSwing module by explicit file path.

    A bare ``import universe`` is not safe in tests: 52WeekHighUS/ also contains
    universe.py, models.py and db.py, and if that suite runs first in the same
    pytest session its sys.path entry shadows this package. Production entry
    points control their own sys.path ordering (see prices.py), but a shared
    test session does not.
    """
    import importlib.util

    key = f"insiderswing_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, _PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


CONF = cfg.InsiderConfig()


# ──────────────────────────────────────────────────────────────────────────────
#  Noise filter
# ──────────────────────────────────────────────────────────────────────────────

def _classify(**kw):
    base = dict(
        transaction_code="P", acquired_disposed="A", is_derivative=False,
        security_title="Common Stock", price_per_share=25.0, shares=1000.0,
        rule_10b5_1=False,
    )
    base.update(kw)
    return filters.classify_transaction(**base)


def test_open_market_purchase_is_kept():
    assert _classify() == (filters.OPEN_MARKET_BUY, None)


def test_open_market_sale_is_kept():
    assert _classify(transaction_code="S", acquired_disposed="D") == (filters.OPEN_MARKET_SALE, None)


@pytest.mark.parametrize("code,reason", [
    ("A", "grant_award"),
    ("M", "option_exercise_exempt"),
    ("F", "tax_withholding_in_kind"),
    ("G", "gift"),
    ("D", "disposition_to_issuer"),
    ("C", "derivative_conversion"),
    ("J", "other_footnote_defined"),
])
def test_mechanical_codes_are_discarded(code, reason):
    """These are the codes that make a naive insider dataset 95% noise."""
    cls, got = _classify(transaction_code=code)
    assert cls == filters.EXCLUDED
    assert got == reason


def test_fmp_style_hyphenated_codes_are_understood():
    """FMP returns 'P-Purchase'; EDGAR returns bare 'P'. Both must classify alike."""
    assert _classify(transaction_code="P-Purchase")[0] == filters.OPEN_MARKET_BUY
    assert _classify(transaction_code="F-InKind")[0] == filters.EXCLUDED


def test_zero_price_purchase_is_not_an_open_market_buy():
    """A $0 'purchase' is a mis-coded transfer, not a cash trade."""
    cls, reason = _classify(price_per_share=0.0)
    assert cls == filters.EXCLUDED and reason == "zero_or_missing_price"


def test_derivative_security_discarded_by_title_even_when_flag_is_false():
    cls, reason = _classify(security_title="Employee Stock Option (right to buy)",
                            is_derivative=False)
    assert cls == filters.EXCLUDED and reason == "derivative_security"


def test_confirmed_10b5_1_plan_is_discarded():
    cls, reason = _classify(rule_10b5_1=True)
    assert cls == filters.EXCLUDED and reason == "rule_10b5_1_plan"


def test_unknown_10b5_1_is_kept_by_default_but_droppable():
    """
    Unknown is a third state, not False. Pre-2022 Form 4s have no checkbox at
    all; dropping them by default would delete most of the history.
    """
    assert _classify(rule_10b5_1=None)[0] == filters.OPEN_MARKET_BUY
    cls, reason = _classify(rule_10b5_1=None, exclude_unknown_10b5_1=True)
    assert cls == filters.EXCLUDED and reason == "rule_10b5_1_unknown"


# ──────────────────────────────────────────────────────────────────────────────
#  Role bucketing
# ──────────────────────────────────────────────────────────────────────────────

def test_ceo_who_is_also_a_director_buckets_as_ceo():
    """The CEO hat carries the information, not the board seat."""
    assert classify_role(True, True, False, "Chief Executive Officer") == "ceo_cfo"


def test_pure_ten_percent_owner_buckets_last():
    assert classify_role(False, False, True, None) == "ten_pct"


def test_officer_without_ceo_cfo_title_is_plain_officer():
    assert classify_role(True, False, False, "SVP, Operations") == "officer"


# ──────────────────────────────────────────────────────────────────────────────
#  Point-in-time discipline
# ──────────────────────────────────────────────────────────────────────────────

def _hist(rows):
    """rows = [(cik, 'YYYY-MM-DD', value)]"""
    import scoring
    df = pd.DataFrame(rows, columns=["reporting_cik", "filing_date", "value_usd"])
    df["filing_dt"] = pd.to_datetime(df["filing_date"])
    return scoring._InsiderHistory(df)


def test_trailing_average_excludes_the_event_day():
    """A buy must never be benchmarked against itself."""
    h = _hist([("C1", "2024-01-10", 50_000.0), ("C1", "2024-06-01", 900_000.0)])
    avg = h.trailing_avg("C1", date(2024, 6, 1), 730)
    assert avg == pytest.approx(50_000.0)


def test_trailing_average_excludes_filings_outside_the_lookback():
    h = _hist([("C1", "2020-01-01", 1_000_000.0), ("C1", "2024-01-10", 50_000.0)])
    assert h.trailing_avg("C1", date(2024, 6, 1), 730) == pytest.approx(50_000.0)


def test_trailing_average_is_none_without_prior_history():
    h = _hist([("C1", "2024-06-01", 900_000.0)])
    assert h.trailing_avg("C1", date(2024, 6, 1), 730) is None


def test_novelty_detects_a_first_time_buyer():
    h = _hist([("C1", "2024-06-01", 100_000.0)])
    assert h.has_prior_buy("C1", date(2024, 6, 1), 365) is False


def test_novelty_is_false_for_a_habitual_buyer():
    h = _hist([("C1", "2024-03-01", 10_000.0), ("C1", "2024-06-01", 100_000.0)])
    assert h.has_prior_buy("C1", date(2024, 6, 1), 365) is True


def test_cluster_credit_saturates():
    """1 -> 2 insiders is the informative step; 5 is not 5x better than 1."""
    import scoring
    assert scoring.cluster_credit(1) < scoring.cluster_credit(2) < scoring.cluster_credit(3)
    assert scoring.cluster_credit(4) == scoring.cluster_credit(9) == 1.0
    assert scoring.cluster_credit(0) == 0.0


def test_ten_percent_owners_score_near_zero_on_role():
    import scoring
    assert scoring.role_credit("ten_pct", CONF) < scoring.role_credit("director", CONF)
    assert scoring.role_credit("ceo_cfo", CONF) > scoring.role_credit("officer", CONF)


# ──────────────────────────────────────────────────────────────────────────────
#  Price helpers
# ──────────────────────────────────────────────────────────────────────────────

def _price_frame(n=120, start_price=100.0, drift=0.0, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = start_price + np.cumsum(rng.normal(drift, 1.0, n))
    close = np.maximum(close, 1.0)
    df = pd.DataFrame({
        "Open": close * 0.998,
        "High": close * 1.012,
        "Low": close * 0.988,
        "Close": close,
        "Volume": rng.integers(800_000, 1_200_000, n).astype(float),
    }, index=idx)
    return df


def test_rsi_is_bounded_and_handles_a_pure_uptrend():
    close = pd.Series(np.arange(100, 160, dtype=float))
    rsi = price_mod.wilder_rsi(close, 14).dropna()
    assert (rsi <= 100.0).all() and (rsi >= 0.0).all()
    assert rsi.iloc[-1] == pytest.approx(100.0)


def test_donchian_level_excludes_the_current_bar():
    """A breakout level that includes today's own high is a tautology."""
    df = _price_frame()
    enriched = price_mod.add_insider_indicators(df, CONF)
    col = f"DonchianHigh{CONF.breakout_lookback}"
    i = 60
    expected = df["High"].iloc[i - CONF.breakout_lookback:i].max()
    assert enriched[col].iloc[i] == pytest.approx(expected)


def test_shift_sessions_counts_trading_days_not_calendar_days():
    df = _price_frame(n=40)
    d0 = df.index[10].date()
    assert price_mod.shift_sessions(df, d0, 0) == df.index[10]
    assert price_mod.shift_sessions(df, d0, 3) == df.index[13]
    assert price_mod.shift_sessions(df, d0, 999) is None


def test_average_dollar_volume_ignores_future_bars():
    df = _price_frame(n=60)
    enriched = price_mod.add_insider_indicators(df, CONF)
    as_of = enriched.index[30].date()
    univ = _insider_module("universe")
    adv = univ.avg_dollar_volume(enriched, as_of, 20)
    window = enriched.iloc[11:31]
    assert adv == pytest.approx(float((window["Close"] * window["Volume"]).mean()))


# ──────────────────────────────────────────────────────────────────────────────
#  Risk
# ──────────────────────────────────────────────────────────────────────────────

def _bar(atr=2.0, swing_low=None, adv=50_000_000.0, price=100.0):
    data = {"ATR14": atr, "AvgDollarVol": adv, "Close": price,
            "Open": price, "High": price * 1.01, "Low": price * 0.99}
    data[f"SwingLow{CONF.swing_low_lookback}"] = swing_low if swing_low is not None else np.nan
    return pd.Series(data)


def test_stop_takes_the_tighter_of_atr_and_swing_low():
    """Tighter = higher stop price = smaller risk per share."""
    plan = risk.build_entry(_bar(atr=2.0, swing_low=98.0), 100.0, CONF)
    assert plan.ok
    assert plan.stop_basis.startswith("swing_low")     # 98.0 beats 100 - 2*2 = 96.0
    assert plan.initial_stop == pytest.approx(98.0)


def test_stop_falls_back_to_atr_when_swing_low_is_far_below():
    plan = risk.build_entry(_bar(atr=2.0, swing_low=80.0), 100.0, CONF)
    assert plan.ok and plan.stop_basis.startswith("atr")
    assert plan.initial_stop == pytest.approx(96.0)


def test_microscopic_stop_is_floored():
    """
    A swing low a cent under entry would otherwise give a near-zero
    risk-per-share, exploding the R-multiple and the risk-based share count.
    """
    plan = risk.build_entry(_bar(atr=2.0, swing_low=99.99), 100.0, CONF)
    assert plan.ok
    assert "floored" in plan.stop_basis
    expected = max(CONF.min_stop_atr_mult * 2.0, 100.0 * CONF.min_stop_pct / 100.0)
    assert plan.risk_per_share == pytest.approx(expected)


def test_position_size_takes_the_capital_cap_when_it_is_tightest():
    """
    At default settings the capital cap ($10k = 10% of a $100k account) is always
    tighter than the liquidity cap, because the $2M ADV floor puts the 2%-of-ADV
    ceiling at $40k minimum. Risk-based sizing here would be 250 shares
    ($1,000 risk budget / $4 risk per share), so capital binds at 100.
    """
    plan = risk.build_entry(_bar(atr=2.0, swing_low=None, adv=3_000_000.0), 100.0, CONF)
    assert plan.ok
    assert plan.qty == 100                      # $10,000 / $100
    assert plan.qty < int(1_000.0 / 4.0)        # tighter than the risk-based size


def test_liquidity_cap_binds_once_the_account_is_large_enough():
    """
    The liquidity cap only becomes the binding constraint for an account big
    enough that 10% of it exceeds 2% of the name's daily dollar volume. That is
    exactly the case it exists for.
    """
    big = CONF.with_overrides(account_size=5_000_000.0)
    adv = 3_000_000.0
    plan = risk.build_entry(_bar(atr=2.0, swing_low=None, adv=adv), 100.0, big)
    assert plan.ok
    liquidity_cap = int((adv * big.max_pct_of_adv / 100.0) // 100.0)
    capital_cap = int(big.max_capital_per_trade() // 100.0)
    assert liquidity_cap < capital_cap
    assert plan.qty == liquidity_cap


def test_entry_rejected_below_the_liquidity_floor():
    plan = risk.build_entry(_bar(adv=100_000.0), 100.0, CONF)
    assert not plan.ok and "ADV" in plan.reason


def test_slippage_grows_with_participation():
    small = risk.slippage_bps(5_000.0, 100_000_000.0, CONF)
    large = risk.slippage_bps(50_000.0, 500_000.0, CONF)
    assert large > small > 0
    # Unknown ADV must be penalised, not assumed benign.
    assert risk.slippage_bps(10_000.0, None, CONF) > CONF.base_spread_bps


def test_slippage_always_works_against_the_trade():
    buy, _ = risk.fill_price(100.0, "buy", 10_000.0, 5_000_000.0, CONF)
    sell, _ = risk.fill_price(100.0, "sell", 10_000.0, 5_000_000.0, CONF)
    assert buy > 100.0 > sell


# ──────────────────────────────────────────────────────────────────────────────
#  Exit state machine
# ──────────────────────────────────────────────────────────────────────────────

def _open_position(price=100.0, atr=2.0, swing_low=None):
    plan = risk.build_entry(_bar(atr=atr, swing_low=swing_low), price, CONF)
    assert plan.ok
    return risk.OpenPosition(plan, pd.Timestamp("2024-03-01"), CONF), plan


def test_stop_wins_when_a_bar_touches_both_stop_and_target():
    """
    Daily bars cannot resolve intrabar ordering. Assuming the favourable one is
    how a backtest invents returns it never had.
    """
    pos, plan = _open_position()
    bar = pd.Series({"Low": plan.initial_stop - 1.0,
                     "High": plan.target_price + 1.0,
                     "Close": 100.0})
    ev = pos.update(bar, pd.Timestamp("2024-03-04"))
    assert ev.exited and ev.exit_reason == "stop"
    assert ev.exit_ref_price == pytest.approx(plan.initial_stop)


def test_target_fills_when_the_stop_is_untouched():
    pos, plan = _open_position()
    bar = pd.Series({"Low": 99.0, "High": plan.target_price + 0.5, "Close": plan.target_price})
    ev = pos.update(bar, pd.Timestamp("2024-03-04"))
    assert ev.exited and ev.exit_reason == "target"


def test_time_stop_fires_unconditionally():
    """This factor's edge decays; without a hard time stop it becomes a hold."""
    pos, plan = _open_position()
    flat = pd.Series({"Low": 99.5, "High": 100.5, "Close": 100.0})
    ev = None
    for i in range(CONF.time_stop_days):
        ev = pos.update(flat, pd.Timestamp("2024-03-01") + timedelta(days=i + 1))
    assert ev.exited and ev.exit_reason == "time_stop"


def test_trailing_stop_only_ratchets_up_and_only_in_profit():
    pos, plan = _open_position()
    start_stop = pos.stop

    # A losing bar must not move the stop at all.
    pos.update(pd.Series({"Low": 99.0, "High": 99.8, "Close": 99.2}), pd.Timestamp("2024-03-04"))
    assert pos.stop == pytest.approx(start_stop)

    # A profitable bar that stays UNDER the target lifts the stop. (Touching
    # the target would exit first, before any trailing update runs.)
    assert plan.target_price == pytest.approx(110.0)
    pos.update(pd.Series({"Low": 106.0, "High": 109.0, "Close": 108.0}), pd.Timestamp("2024-03-05"))
    assert pos.stop > start_stop
    lifted = pos.stop

    # A pullback must never lower it.
    pos.update(pd.Series({"Low": 104.0, "High": 106.0, "Close": 105.0}), pd.Timestamp("2024-03-06"))
    assert pos.stop == pytest.approx(lifted)


def test_realise_charges_costs_on_both_sides():
    pos, plan = _open_position()
    out = pos.realise(110.0)
    assert out["gross_pnl"] > out["net_pnl"]       # costs subtracted
    assert out["slippage_cost"] > 0
    assert out["r_multiple"] == pytest.approx(
        out["net_pnl"] / (plan.qty * plan.risk_per_share)
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Technical triggers
# ──────────────────────────────────────────────────────────────────────────────

def test_no_trigger_in_window_returns_expired_not_an_exception():
    """An expiry is a meaningful result that gets recorded, not an error."""
    df = _price_frame(n=120)
    res = technical.find_trigger(df, df.index[60].date(), 5, CONF, allowed=[])
    assert res.fired is False and res.reason == "window_expired"


def test_missing_price_data_is_reported_not_raised():
    res = technical.find_trigger(pd.DataFrame(), date(2024, 3, 1), 10, CONF)
    assert res.fired is False and res.reason == "no_price_data"


def test_dma_reclaim_requires_a_cross_not_merely_being_above():
    above_prev = pd.Series({"Close": 105.0, "SMA20": 100.0, "SMA50": 95.0})
    above_now = pd.Series({"Close": 106.0, "SMA20": 100.0, "SMA50": 95.0})
    assert technical._dma_reclaim(above_prev, above_now) is False

    below_prev = pd.Series({"Close": 99.0, "SMA20": 100.0, "SMA50": 95.0})
    assert technical._dma_reclaim(below_prev, above_now) is True


def test_range_breakout_needs_a_close_above_the_prior_high():
    conf = CONF
    col = f"DonchianHigh{conf.breakout_lookback}"
    assert technical._range_breakout(pd.Series({"Close": 101.0, col: 100.0}), conf.breakout_lookback)
    assert not technical._range_breakout(pd.Series({"Close": 99.0, col: 100.0}), conf.breakout_lookback)
    # A NaN level (insufficient history) must not fire.
    assert not technical._range_breakout(pd.Series({"Close": 99.0, col: np.nan}), conf.breakout_lookback)


def test_rsi_reset_requires_prior_oversold_and_a_cross():
    window = pd.DataFrame({"RSI14": [28.0, 33.0, 45.0, 52.0]})
    assert technical._rsi_reset(window, 3, CONF) is True

    # Crossed 50, but was never oversold in the window → not a reset.
    never_oversold = pd.DataFrame({"RSI14": [45.0, 46.0, 48.0, 52.0]})
    assert technical._rsi_reset(never_oversold, 3, CONF) is False


def test_find_trigger_never_fires_before_the_from_date():
    df = _price_frame(n=150, drift=0.4)
    enriched = price_mod.add_insider_indicators(df, CONF)
    from_date = enriched.index[100].date()
    res = technical.find_trigger(enriched, from_date, 15, CONF)
    if res.fired:
        assert res.trigger_date >= pd.Timestamp(from_date)


# ──────────────────────────────────────────────────────────────────────────────
#  Metrics
# ──────────────────────────────────────────────────────────────────────────────

def test_thin_sample_is_flagged():
    import metrics
    trades = pd.DataFrame({
        "net_pnl": [100.0, -50.0, 75.0],
        "r_multiple": [1.0, -0.5, 0.75],
        "return_pct": [0.01, -0.005, 0.008],
        "holding_days": [5, 8, 12],
        "gross_pnl": [110.0, -45.0, 80.0],
        "slippage_cost": [10.0, 5.0, 5.0],
        "notional": [10_000.0] * 3,
    })
    st = metrics.trade_stats(trades, CONF)
    assert st["trades"] == 3 and st["thin_sample"] is True
    assert st["profit_factor"] == pytest.approx(175.0 / 50.0)


def test_empty_inputs_do_not_raise():
    import metrics
    assert metrics.trade_stats(pd.DataFrame(), CONF)["trades"] == 0
    assert metrics.equity_stats(pd.DataFrame(), CONF)["sessions"] == 0
    assert metrics.bootstrap_trade_ci(pd.DataFrame(), CONF)["available"] is False
    assert metrics.segment_table(pd.DataFrame(), "role", CONF).empty


def test_max_drawdown_is_negative_and_finds_the_trough():
    import metrics
    series = pd.Series([100.0, 120.0, 90.0, 110.0], index=pd.bdate_range("2024-01-01", periods=4))
    mdd, when = metrics.max_drawdown(series)
    assert mdd == pytest.approx(-0.25)
    assert str(when).startswith("2024-01-03")


def test_permutation_test_finds_no_difference_between_identical_samples():
    import metrics
    rng = np.random.default_rng(3)
    a = rng.normal(0.2, 1.0, 200)
    b = rng.normal(0.2, 1.0, 200)
    res = metrics.permutation_diff_test(a, b, iterations=800)
    assert res["available"] and res["p_value"] > 0.05


def test_permutation_test_detects_a_real_difference():
    import metrics
    rng = np.random.default_rng(4)
    a = rng.normal(1.5, 1.0, 200)
    b = rng.normal(0.0, 1.0, 200)
    res = metrics.permutation_diff_test(a, b, iterations=800)
    assert res["significant_at_5pct"] is True


def test_stability_verdict_calls_out_an_isolated_spike():
    import walkforward
    grid = pd.DataFrame([
        {"conviction_threshold": t, "cluster_window_days": 45,
         "confirmation_window_days": 15, "trades": 60,
         "expectancy_r": 3.0 if t == 60 else 0.01, "thin_sample": False}
        for t in (50, 55, 60, 65, 70)
    ])
    verdict = walkforward.assess_stability(grid)["verdict"]
    assert "OVERFIT RISK" in verdict


def test_stability_verdict_recognises_a_plateau():
    import walkforward
    grid = pd.DataFrame([
        {"conviction_threshold": t, "cluster_window_days": 45,
         "confirmation_window_days": 15, "trades": 60,
         "expectancy_r": 0.30 + 0.01 * i, "thin_sample": False}
        for i, t in enumerate((50, 55, 60, 65, 70))
    ])
    assert "STABLE" in walkforward.assess_stability(grid)["verdict"]


def test_stability_verdict_when_nothing_works():
    import walkforward
    grid = pd.DataFrame([
        {"conviction_threshold": t, "cluster_window_days": 45,
         "confirmation_window_days": 15, "trades": 60,
         "expectancy_r": -0.2, "thin_sample": False}
        for t in (50, 55, 60)
    ])
    assert "does not work" in walkforward.assess_stability(grid)["verdict"]


def test_walk_forward_windows_are_contiguous_and_cover_the_range():
    import walkforward
    windows = walkforward.split_windows(date(2016, 1, 1), date(2025, 12, 31), 3)
    assert len(windows) == 3
    assert windows[0][1] == date(2016, 1, 1)
    assert windows[-1][2] == date(2025, 12, 31)
    for (_, _, prev_end), (_, next_start, _) in zip(windows, windows[1:]):
        assert next_start == prev_end + timedelta(days=1)


# ──────────────────────────────────────────────────────────────────────────────
#  Form 4 XML parsing
# ──────────────────────────────────────────────────────────────────────────────

_FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2024-05-06</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Test Corp</issuerName>
    <issuerTradingSymbol>TEST</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001111111</rptOwnerCik>
      <rptOwnerName>Doe Jane</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Financial Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>0</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-05-06</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>42.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>12000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Employee Stock Option</value></securityTitle>
      <transactionDate><value>2024-05-06</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
  <footnotes><footnote id="F1">Routine purchase.</footnote></footnotes>
</ownershipDocument>
"""


def _parse(xml=_FORM4, filing_date="2024-05-08"):
    from edgar_source import EdgarSource
    return EdgarSource().parse_form4(xml, "0001234567-24-000001", filing_date)


def test_form4_parse_extracts_issuer_owner_and_transactions():
    recs = _parse()
    assert len(recs) == 1
    r = recs[0]
    assert r.ticker == "TEST"
    assert r.reporting_name == "Doe Jane"
    assert r.is_officer and r.is_director and not r.is_ten_percent
    assert r.officer_title == "Chief Financial Officer"
    assert len(r.transactions) == 2


def test_form4_parse_keeps_filing_date_and_transaction_date_separate():
    """The whole point-in-time contract rests on these not being conflated."""
    r = _parse()[0]
    assert r.filing_date == "2024-05-08"
    assert r.period_of_report == "2024-05-06"
    assert r.transactions[0].transaction_date == "2024-05-06"
    assert r.transactions[0].filing_date == "2024-05-08"


def test_form4_parse_marks_derivative_lines_and_keeps_line_numbers_unique():
    r = _parse()[0]
    nd = [t for t in r.transactions if not t.is_derivative]
    dv = [t for t in r.transactions if t.is_derivative]
    assert len(nd) == 1 and len(dv) == 1
    assert len({t.line_no for t in r.transactions}) == 2


def test_form4_parse_computes_value_and_role_bucket():
    txn = _parse()[0].transactions[0]
    assert txn.value_usd == pytest.approx(2000 * 42.50)
    assert txn.role_bucket == "ceo_cfo"


def test_form4_10b5_1_checkbox_is_read():
    assert _parse()[0].aff_10b5_one is False
    assert _parse(_FORM4.replace("<aff10b5One>0<", "<aff10b5One>1<"))[0].aff_10b5_one is True


def test_form4_10b5_1_detected_from_footnote_when_checkbox_says_no():
    """Pre-2022 filings have no checkbox; the footnote is the only marker."""
    xml = _FORM4.replace("Routine purchase.",
                         "Sale executed pursuant to a Rule 10b5-1 trading plan.")
    assert _parse(xml)[0].aff_10b5_one is True


def test_form4_missing_checkbox_is_unknown_not_false():
    xml = _FORM4.replace("<aff10b5One>0</aff10b5One>", "")
    assert _parse(xml)[0].aff_10b5_one is None


def test_form4_malformed_xml_returns_empty_rather_than_raising():
    assert _parse("<ownershipDocument><broken>") == []


def test_pad_cik_normalises_all_the_shapes_edgar_uses():
    from edgar_source import pad_cik
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("CIK0000320193") == "0000320193"
    assert pad_cik("0000320193") == "0000320193"
    assert pad_cik(None) == ""
