"""
InsiderSwing — configuration.

Every tunable lives here as a dataclass field with an ``INSIDER_*`` env
override.  The backtest parameter-stability sweep mutates copies of
``InsiderConfig``; nothing reads os.environ directly outside this module.

Rationale for the defaults is inline — where a default came from the academic
literature rather than from a fit to this dataset, that is stated, because a
literature-derived default is not an overfit but a swept-and-tuned one is.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent      # InsiderSwing/
_ROOT = _HERE.parent                         # project root

load_dotenv(_ROOT / ".env")

STRATEGY_VERSION = "insider_v1"


# ── env helpers ───────────────────────────────────────────────────────────────

def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, str(default))))
    except (TypeError, ValueError):
        return default


def _s(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v else default


def _b(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── paths (module-level, not swept) ───────────────────────────────────────────

DATA_DIR         = _ROOT / "data"
CACHE_DIR        = DATA_DIR / "cache" / "insider"
EDGAR_CACHE_DIR  = CACHE_DIR / "edgar"          # raw Form 4 XML + form.idx
PRICE_CACHE_DIR  = CACHE_DIR / "prices"         # OHLCV parquet per ticker
REPORT_DIR       = DATA_DIR / "reports" / "insider"

# SEC requires a descriptive User-Agent with a contact address on every request.
# https://www.sec.gov/os/accessing-edgar-data
SEC_USER_AGENT   = _s("INSIDER_SEC_USER_AGENT", "TradingSystems research (coolcactus@gmail.com)")
SEC_RATE_LIMIT   = _f("INSIDER_SEC_RATE_LIMIT", 8.0)   # requests/sec; SEC's ceiling is 10

FMP_API_KEY      = os.getenv("FMP_API_KEY", "")
FMP_BASE_URL     = _s("INSIDER_FMP_BASE_URL", "https://financialmodelingprep.com/stable")


# ── the sweepable config ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class InsiderConfig:
    """All strategy parameters.  Frozen so a sweep can't mutate a shared copy."""

    # ── Signal construction ───────────────────────────────────────────────────
    cluster_window_days: int = field(default_factory=lambda: _i("INSIDER_CLUSTER_WINDOW_DAYS", 45))
    """Rolling lookback (calendar days, keyed on filing_date) for cluster counting."""

    conviction_threshold: float = field(default_factory=lambda: _f("INSIDER_CONVICTION_THRESHOLD", 60.0))
    """Score (0-100) a symbol must clear to become a qualifying signal."""

    # Score component weights.  Cluster count is weighted heaviest because it is
    # the most replicated finding in the literature (Lakonishok & Lee 2001);
    # isolated single buys are materially weaker.
    w_cluster:  float = field(default_factory=lambda: _f("INSIDER_W_CLUSTER", 40.0))
    w_role:     float = field(default_factory=lambda: _f("INSIDER_W_ROLE", 20.0))
    w_size:     float = field(default_factory=lambda: _f("INSIDER_W_SIZE", 20.0))
    w_novelty:  float = field(default_factory=lambda: _f("INSIDER_W_NOVELTY", 20.0))

    # Role weights (0-1), applied to the highest-ranked buying insider in the window.
    # 10%-owner buys are frequently fund rebalancing rather than conviction, so
    # they score near zero by default.
    role_weight_ceo_cfo:   float = field(default_factory=lambda: _f("INSIDER_ROLE_CEO_CFO", 1.00))
    role_weight_officer:   float = field(default_factory=lambda: _f("INSIDER_ROLE_OFFICER", 0.70))
    role_weight_director:  float = field(default_factory=lambda: _f("INSIDER_ROLE_DIRECTOR", 0.45))
    role_weight_ten_pct:   float = field(default_factory=lambda: _f("INSIDER_ROLE_TEN_PCT", 0.10))
    exclude_ten_pct_owners: bool = field(default_factory=lambda: _b("INSIDER_EXCLUDE_TEN_PCT", False))

    # Relative size: buy $ vs. that insider's OWN trailing average buy size.
    size_history_days: int = field(default_factory=lambda: _i("INSIDER_SIZE_HISTORY_DAYS", 730))
    size_ratio_full_credit: float = field(default_factory=lambda: _f("INSIDER_SIZE_RATIO_FULL", 3.0))
    """Ratio vs. own trailing average that earns full size credit. 3x = 'well outside normal'."""
    min_trade_value_usd: float = field(default_factory=lambda: _f("INSIDER_MIN_TRADE_VALUE", 25_000.0))
    """Buys below this notional are ignored entirely — routine/token purchases."""

    # Rule 10b5-1 plan handling.  Confirmed-plan rows are always dropped.
    # UNKNOWN is a third state: the Form 4 checkbox only exists on filings from
    # 2022 onward, and FMP never exposes it, so most historical rows are
    # unknown.  Dropping unknowns would delete the entire pre-2022 backtest, so
    # the default keeps them and the limitation is reported instead of hidden.
    exclude_unknown_10b5_1: bool = field(default_factory=lambda: _b("INSIDER_EXCLUDE_UNKNOWN_10B5_1", False))

    # Novelty: first purchase by that insider in the trailing N days.
    novelty_lookback_days: int = field(default_factory=lambda: _i("INSIDER_NOVELTY_LOOKBACK_DAYS", 365))

    # Earnings-proximity confound.  Down-weight, never exclude — exposed as a flag.
    earnings_proximity_days: int = field(default_factory=lambda: _i("INSIDER_EARNINGS_PROXIMITY_DAYS", 14))
    earnings_proximity_penalty: float = field(default_factory=lambda: _f("INSIDER_EARNINGS_PENALTY", 0.85))
    """Multiplier applied to the final score when the flag is set (0.85 = -15%)."""

    # Cluster selling — caution filter only.  We deliberately do NOT build a
    # mirrored short signal: the literature does not support insider selling as
    # a symmetric predictor (sales are diversification/liquidity-driven).
    sell_pressure_min_sellers: int = field(default_factory=lambda: _i("INSIDER_SELL_MIN_SELLERS", 2))
    sell_pressure_value_ratio: float = field(default_factory=lambda: _f("INSIDER_SELL_VALUE_RATIO", 1.0))
    """Block a long if sell$ / buy$ in the window exceeds this AND >= min_sellers sold."""
    sell_pressure_blocks_entry: bool = field(default_factory=lambda: _b("INSIDER_SELL_BLOCKS", True))

    # ── Availability lag ──────────────────────────────────────────────────────
    signal_lag_days: int = field(default_factory=lambda: _i("INSIDER_SIGNAL_LAG_DAYS", 1))
    """Trading days after filing_date before the signal is actionable.  1 = we can
    act no earlier than the next session's open.  Filings accepted after 17:30 ET
    are disseminated the next business day, so 0 would be optimistic."""

    # ── Technical confirmation ────────────────────────────────────────────────
    require_technical_trigger: bool = field(default_factory=lambda: _b("INSIDER_REQUIRE_TRIGGER", True))
    confirmation_window_days: int = field(default_factory=lambda: _i("INSIDER_CONFIRMATION_WINDOW_DAYS", 15))
    """Trading days after the filing for a technical trigger to fire. No fire = expire."""
    trigger_types: str = field(default_factory=lambda: _s("INSIDER_TRIGGER_TYPES", "dma_reclaim,range_breakout,rsi_reset"))

    rsi_oversold: float = field(default_factory=lambda: _f("INSIDER_RSI_OVERSOLD", 35.0))
    rsi_reset_level: float = field(default_factory=lambda: _f("INSIDER_RSI_RESET_LEVEL", 50.0))
    breakout_lookback: int = field(default_factory=lambda: _i("INSIDER_BREAKOUT_LOOKBACK", 20))

    # ── Risk management ───────────────────────────────────────────────────────
    account_size: float = field(default_factory=lambda: _f("INSIDER_ACCOUNT_SIZE", 100_000.0))
    risk_pct: float = field(default_factory=lambda: _f("INSIDER_RISK_PERCENT", 1.0))
    max_capital_per_trade_pct: float = field(default_factory=lambda: _f("INSIDER_MAX_CAPITAL_PCT", 10.0))
    max_concurrent_positions: int = field(default_factory=lambda: _i("INSIDER_MAX_CONCURRENT_POSITIONS", 10))

    atr_period: int = field(default_factory=lambda: _i("INSIDER_ATR_PERIOD", 14))
    atr_stop_mult: float = field(default_factory=lambda: _f("INSIDER_ATR_STOP_MULT", 2.0))
    swing_low_lookback: int = field(default_factory=lambda: _i("INSIDER_SWING_LOW_LOOKBACK", 10))

    # Minimum stop distance.  The "tighter of ATR-stop or swing low" rule can
    # place the stop within pennies of entry when the prior swing low sits just
    # under the entry bar.  That produces a microscopic risk-per-share, which
    # inflates R-multiples to absurd values and lets the risk-based sizing
    # formula ask for an impossible number of shares.  The floor is the larger
    # of a fraction of ATR and a fraction of price, so it adapts to both
    # volatility and price level.
    min_stop_atr_mult: float = field(default_factory=lambda: _f("INSIDER_MIN_STOP_ATR_MULT", 0.5))
    min_stop_pct: float = field(default_factory=lambda: _f("INSIDER_MIN_STOP_PCT", 1.0))
    target_r_multiple: float = field(default_factory=lambda: _f("INSIDER_TARGET_R", 2.5))
    trail_atr_mult: float = field(default_factory=lambda: _f("INSIDER_TRAIL_ATR_MULT", 2.5))
    time_stop_days: int = field(default_factory=lambda: _i("INSIDER_TIME_STOP_DAYS", 20))
    """Hard exit in trading days.  This factor's edge decays; without a time stop
    the strategy silently becomes a long-term hold, which is a different system."""

    # ── Liquidity + costs ─────────────────────────────────────────────────────
    min_avg_dollar_volume: float = field(default_factory=lambda: _f("INSIDER_MIN_ADV_USD", 2_000_000.0))
    adv_lookback_days: int = field(default_factory=lambda: _i("INSIDER_ADV_LOOKBACK", 20))
    max_pct_of_adv: float = field(default_factory=lambda: _f("INSIDER_MAX_PCT_ADV", 2.0))
    """Cap position notional at this % of ADV; also the input to the slippage model."""
    commission_bps: float = field(default_factory=lambda: _f("INSIDER_COMMISSION_BPS", 1.0))
    base_spread_bps: float = field(default_factory=lambda: _f("INSIDER_BASE_SPREAD_BPS", 5.0))
    impact_coefficient: float = field(default_factory=lambda: _f("INSIDER_IMPACT_COEF", 30.0))
    """Slippage bps = base_spread + impact_coef * sqrt(participation_rate).
    A square-root market-impact law (Almgren et al.) rather than a flat bps
    assumption, because small-cap fills are size-dependent."""

    # ── Backtest window ───────────────────────────────────────────────────────
    backtest_start: str = field(default_factory=lambda: _s("INSIDER_BACKTEST_START", "2016-01-01"))
    backtest_end: str = field(default_factory=lambda: _s("INSIDER_BACKTEST_END", ""))
    """Empty = today."""
    walk_forward_windows: int = field(default_factory=lambda: _i("INSIDER_WF_WINDOWS", 3))
    benchmark_symbol: str = field(default_factory=lambda: _s("INSIDER_BENCHMARK", "SPY"))
    bootstrap_iterations: int = field(default_factory=lambda: _i("INSIDER_BOOTSTRAP_ITERS", 2000))
    thin_sample_threshold: int = field(default_factory=lambda: _i("INSIDER_THIN_SAMPLE", 30))
    """Below this trade count, report the result as statistically inconclusive."""

    # ── Data source ───────────────────────────────────────────────────────────
    preferred_source: str = field(default_factory=lambda: _s("INSIDER_SOURCE", "edgar"))
    """'edgar' | 'fmp' | 'auto'.  Default edgar: it is free, complete, and carries
    the 10b5-1 checkbox that FMP does not expose."""

    def with_overrides(self, **kw) -> "InsiderConfig":
        """Return a copy with fields replaced — used by the parameter sweep."""
        return replace(self, **kw)

    def trigger_list(self) -> list[str]:
        return [t.strip() for t in self.trigger_types.split(",") if t.strip()]

    def risk_budget(self) -> float:
        return self.account_size * (self.risk_pct / 100.0)

    def max_capital_per_trade(self) -> float:
        return self.account_size * (self.max_capital_per_trade_pct / 100.0)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def param_key(self) -> str:
        """Short identifier for the parameters that actually change signal output."""
        return (
            f"cw{self.cluster_window_days}"
            f"_th{self.conviction_threshold:g}"
            f"_conf{self.confirmation_window_days}"
            f"_tech{int(self.require_technical_trigger)}"
        )


DEFAULT_CONFIG = InsiderConfig()


def ensure_dirs() -> None:
    for d in (CACHE_DIR, EDGAR_CACHE_DIR, PRICE_CACHE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
