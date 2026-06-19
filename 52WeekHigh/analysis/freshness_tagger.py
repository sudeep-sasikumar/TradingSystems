"""
freshness_tagger.py — Compute and store the "freshness factor" for backtest trades.

For each closed backtest trade, finds the gap between the entry signal date
and the previous time the same stock made a new 252-day high, using only
price data strictly before the entry date (no lookahead).

THREE CATEGORIES:
    'insufficient_history'
        Fewer than 253 trading-day rows in the price cache before entry date.
        Cannot compute a 252-day benchmark at all.  Gap fields are NULL.

    'first_observed_high'
        >= 253 rows available before entry.  The 252-day benchmark was computed
        for all prior dates, but no prior date had close > benchmark in the
        available window.  Gap fields are NULL.

        IMPORTANT CAVEAT — LIMITED LOOKBACK:
        52wh_v1 cache starts 2021-01-01.  Stocks that last made a 52wk high
        before that date show as 'first_observed_high' even if the gap was
        just 15 months.  This bucket is not reliable for 52wh_v1; it is
        significantly more trustworthy for 52wh_v1_survivorship_10y (cache
        from 2018-01-01), where "no prior found" genuinely implies a gap of
        several years.

    'gap_computed'
        A prior 52wk high signal was found.  freshness_gap_td and
        freshness_gap_cal are populated.

STORAGE:
    Results are written as UPDATE statements on existing trade_regime_tags
    rows.  The regime tagger deletes and re-inserts those rows; re-run
    --checkpoint freshness after any --checkpoint tag run.

BUCKET BOUNDARIES (trading days, data-derived):
    < 5 td    → "< 1 week"
    5 – 21    → "1w – 1m"
    22 – 129  → "1 – 6 months"
    130 – 251 → "6 – 12 months"
    252 – 755 → "1 – 3 years"
    756+      → "3+ years"

    first_observed_high and insufficient_history are their own buckets.

CLI (via run_regime_analysis.py):
    python 52WeekHigh/run_regime_analysis.py --checkpoint freshness \\
           --strategy-version 52wh_v1
    python 52WeekHigh/run_regime_analysis.py --checkpoint freshness \\
           --strategy-version 52wh_v1_survivorship_10y
    python 52WeekHigh/run_regime_analysis.py --checkpoint freshness-analyze
"""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import text

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

logger = logging.getLogger(__name__)

ROLLING_WINDOW = 252

# Price cache directories keyed by strategy_version
_CACHE_DIRS: dict[str, Path] = {
    "52wh_v1":                   _ROOT / "data" / "cache" / "prices",
    "52wh_v1_survivorship_10y":  _ROOT / "data" / "cache" / "prices_historic",
}

# Bucket boundaries and labels (inclusive lower, exclusive upper, trading days)
# These were derived from the actual gap distribution; see --checkpoint freshness-analyze.
BUCKET_DEFS: list[tuple[int, Optional[int], str]] = [
    (1,   5,   "< 1 week"),
    (5,   22,  "1w – 1m"),
    (22,  130, "1 – 6 months"),
    (130, 252, "6 – 12 months"),
    (252, 756, "1 – 3 years"),
    (756, None, "3+ years"),
]

# Ordered list of all bucket labels (for display sorting)
BUCKET_ORDER: list[str] = (
    ["insufficient_history", "first_observed_high"]
    + [label for _, _, label in BUCKET_DEFS]
)


# ── Public helper ──────────────────────────────────────────────────────────────

def assign_bucket(category: str, gap_td: Optional[int]) -> str:
    """
    Convert (category, gap_td) → display bucket label.
    Import this from both the CLI analysis and the dashboard for consistency.
    """
    if category == "insufficient_history":
        return "insufficient_history"
    if category == "first_observed_high":
        return "first_observed_high"
    if gap_td is None:
        return "insufficient_history"
    for lo, hi, label in BUCKET_DEFS:
        if hi is None or gap_td < hi:
            return label
    return BUCKET_DEFS[-1][2]


# ── Schema migration ───────────────────────────────────────────────────────────

def _migrate(engine) -> None:
    """Add freshness columns to trade_regime_tags (idempotent)."""
    stmts = [
        "ALTER TABLE trade_regime_tags ADD COLUMN freshness_category TEXT",
        "ALTER TABLE trade_regime_tags ADD COLUMN freshness_gap_td    INTEGER",
        "ALTER TABLE trade_regime_tags ADD COLUMN freshness_gap_cal   INTEGER",
        "ALTER TABLE trade_regime_tags ADD COLUMN freshness_prior_date TEXT",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            with suppress(Exception):
                conn.execute(text(stmt))
        conn.commit()
    logger.debug("Freshness schema migration complete (columns may already have existed)")


# ── Core computation ───────────────────────────────────────────────────────────

def _load_price_series(ticker: str, cache_dir: Path) -> Optional[pd.Series]:
    """Load close price series from parquet cache. Returns None on failure."""
    fname = ticker.replace(".", "_").replace("/", "_") + ".parquet"
    p = cache_dir / fname
    if not p.exists():
        return None
    with suppress(Exception):
        df = pd.read_parquet(p)
        s = df["Close"].dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.sort_index()
    return None


def _compute_freshness(
    ticker: str,
    entry_date_str: str,
    series_cache: dict[str, Optional[pd.Series]],
    cache_dir: Path,
) -> dict:
    """
    Compute freshness factor for one trade.

    Uses series_cache to avoid re-loading the same ticker's prices twice
    within a single tagging run (important for stocks with multiple trades).

    Returns dict with keys:
        category, gap_td, gap_cal, prior_date
    """
    _null = {"category": "insufficient_history", "gap_td": None, "gap_cal": None, "prior_date": None}

    if ticker not in series_cache:
        series_cache[ticker] = _load_price_series(ticker, cache_dir)
    s = series_cache[ticker]

    if s is None:
        return _null

    entry_ts = pd.Timestamp(entry_date_str)
    prior = s[s.index < entry_ts].dropna()

    # Need at least ROLLING_WINDOW + 1 rows to compute benchmark for any date
    if len(prior) <= ROLLING_WINDOW:
        return _null

    # 252-day rolling max benchmark (shift(1) → no lookahead on the same bar)
    bm = prior.shift(1).rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).max()

    # All prior dates where close exceeded the benchmark (= previous 52wh signals)
    signal_mask = (prior > bm) & bm.notna()
    signal_dates = prior.index[signal_mask]

    if len(signal_dates) == 0:
        return {
            "category":   "first_observed_high",
            "gap_td":     None,
            "gap_cal":    None,
            "prior_date": None,
        }

    last_prior = signal_dates[-1]

    # Calendar days: direct difference
    gap_cal = (entry_ts - last_prior).days

    # Trading days: rows in prior that fall AFTER last_prior, plus 1 for the entry date itself
    gap_td = int((prior.index > last_prior).sum()) + 1

    return {
        "category":   "gap_computed",
        "gap_td":     gap_td,
        "gap_cal":    gap_cal,
        "prior_date": last_prior.strftime("%Y-%m-%d"),
    }


# ── Main tagging entry point ───────────────────────────────────────────────────

def tag_freshness(strategy_version: str) -> dict:
    """
    Compute freshness factor for all backtest trades in strategy_version that
    have existing trade_regime_tags rows, and write results via UPDATE.

    Returns summary dict with counts per category.
    """
    engine = get_engine()
    _migrate(engine)

    cache_dir = _CACHE_DIRS.get(strategy_version)
    if cache_dir is None:
        raise ValueError(
            f"Unknown strategy_version {strategy_version!r}. "
            f"Known: {list(_CACHE_DIRS)}"
        )
    if not cache_dir.exists():
        raise FileNotFoundError(
            f"Price cache directory not found: {cache_dir}\n"
            "Run the backtest first to populate the cache."
        )

    # Load all backtest trades that have regime tags for this strategy version
    tagged = pd.read_sql(
        """
        SELECT t.id  AS trade_id,
               t.ticker,
               t.entry_date
        FROM trades t
        JOIN trade_regime_tags r ON r.trade_id = t.id
        WHERE t.strategy_version = :sv
          AND t.source = 'backtest'
        ORDER BY t.entry_date
        """,
        engine,
        params={"sv": strategy_version},
    )

    if tagged.empty:
        logger.warning(
            "No regime-tagged backtest trades found for %s. "
            "Run --checkpoint tag first.",
            strategy_version,
        )
        return {"total": 0}

    logger.info("Computing freshness for %d trades (strategy=%s) …", len(tagged), strategy_version)

    series_cache: dict[str, Optional[pd.Series]] = {}
    records: list[dict] = []
    counts: dict[str, int] = {"insufficient_history": 0, "first_observed_high": 0, "gap_computed": 0}

    for i, row in tagged.iterrows():
        result = _compute_freshness(
            ticker         = str(row["ticker"]),
            entry_date_str = str(row["entry_date"]),
            series_cache   = series_cache,
            cache_dir      = cache_dir,
        )
        counts[result["category"]] = counts.get(result["category"], 0) + 1

        records.append({
            "trade_id":           int(row["trade_id"]),
            "freshness_category": result["category"],
            "freshness_gap_td":   result["gap_td"],
            "freshness_gap_cal":  result["gap_cal"],
            "freshness_prior_date": result["prior_date"],
        })

        if (i + 1) % 200 == 0 or (i + 1) == len(tagged):
            logger.info("  … %d / %d done", i + 1, len(tagged))

    # Batch UPDATE
    stmt = text("""
        UPDATE trade_regime_tags
        SET freshness_category   = :freshness_category,
            freshness_gap_td     = :freshness_gap_td,
            freshness_gap_cal    = :freshness_gap_cal,
            freshness_prior_date = :freshness_prior_date
        WHERE trade_id = :trade_id
    """)
    with engine.connect() as conn:
        conn.execute(stmt, records)
        conn.commit()

    logger.info(
        "Freshness tagged: %d gap_computed | %d first_observed_high | %d insufficient_history",
        counts["gap_computed"], counts["first_observed_high"], counts["insufficient_history"],
    )
    return {"total": len(tagged), **counts}


# ── Cross-dataset analysis ─────────────────────────────────────────────────────

def load_freshness_df(strategy_version: str) -> pd.DataFrame:
    """
    Load closed backtest trades joined with regime tags + freshness columns.
    Returns DataFrame with freshness_bucket column computed on-the-fly.
    """
    engine = get_engine()
    df = pd.read_sql(
        """
        SELECT t.id, t.ticker, t.entry_date, t.trade_year,
               t.return_pct, t.holding_days,
               r.market_vs_200dma, r.market_6m_quintile,
               r.synthetic_vs_200dma,
               r.freshness_category,
               r.freshness_gap_td,
               r.freshness_gap_cal,
               r.freshness_prior_date
        FROM trades t
        JOIN trade_regime_tags r ON t.id = r.trade_id
        WHERE t.strategy_version = :sv
          AND t.source = 'backtest'
          AND t.status = 'closed'
          AND t.return_pct IS NOT NULL
          AND r.freshness_category IS NOT NULL
        ORDER BY t.entry_date
        """,
        engine,
        params={"sv": strategy_version},
    )
    if df.empty:
        return df

    df["freshness_bucket"] = df.apply(
        lambda r: assign_bucket(r["freshness_category"], r.get("freshness_gap_td")),
        axis=1,
    )
    return df


def _trade_stats(grp: pd.DataFrame) -> dict:
    n = len(grp)
    if n == 0:
        return {"n": 0, "win_pct": None, "avg_ret": None, "median_ret": None, "avg_days": None}
    wins = (grp["return_pct"] > 0).sum()
    return {
        "n":          n,
        "win_pct":    round(wins / n * 100, 1),
        "avg_ret":    round(float(grp["return_pct"].mean()), 2),
        "median_ret": round(float(grp["return_pct"].median()), 2),
        "avg_days":   int(round(grp["holding_days"].mean())),
    }


def run_freshness_analysis() -> None:
    """
    Full printed analysis across both strategy versions.
    Called from --checkpoint freshness-analyze.
    """
    SVS = {
        "52wh_v1":                  "2022–present (original, current-list)",
        "52wh_v1_survivorship_10y": "2019–present (survivorship-corrected)",
    }

    dfs: dict[str, pd.DataFrame] = {}
    for sv, label in SVS.items():
        dfs[sv] = load_freshness_df(sv)

    print()
    print("=" * 72)
    print("  FRESHNESS FACTOR ANALYSIS — 52-Week High Strategy")
    print("  (gap since prior 52-week high signal for same stock)")
    print("=" * 72)

    # ── Part A: Distribution ───────────────────────────────────────────────────

    for sv, label in SVS.items():
        df = dfs[sv]
        if df.empty:
            print(f"\n  [{label}]  No data — run --checkpoint freshness --strategy-version {sv}")
            continue

        print(f"\n{'─'*72}")
        print(f"  DATASET: {label}  |  {len(df):,} closed trades with freshness tags")
        print(f"{'─'*72}")

        cat_counts = df["freshness_category"].value_counts()
        print("\nCategory breakdown:")
        for cat in ["gap_computed", "first_observed_high", "insufficient_history"]:
            n = cat_counts.get(cat, 0)
            pct = n / len(df) * 100
            note = ""
            if cat == "first_observed_high" and sv == "52wh_v1":
                note = "  ← LIMITED LOOKBACK: gap may just exceed 2021 cache start"
            print(f"  {cat:<25}  {n:>5}  ({pct:4.1f}%){note}")

        # Gap distribution (gap_computed only)
        gap_df = df[df["freshness_category"] == "gap_computed"]["freshness_gap_td"].dropna()
        if len(gap_df) > 0:
            pcts = [0, 5, 10, 25, 50, 75, 90, 95, 100]
            vals = np.nanpercentile(gap_df, pcts)
            print(f"\nGap distribution (trading days, n={len(gap_df):,} gap_computed trades):")
            print(f"  {'Pctile':>6}  {'Gap td':>8}  {'~Calendar':>11}")
            for p, v in zip(pcts, vals):
                v_int = int(round(v))
                cal_approx = round(v_int * 365 / 252)
                print(f"  {p:>5}%  {v_int:>8}  {cal_approx:>8}d")

        # Bucket breakdown
        print("\nFreshness buckets (all categories):")
        print(f"  {'Bucket':<22}  {'n':>5}  {'Win%':>6}  {'Avg%':>7}  {'Med%':>7}  {'Avg Days':>9}  Note")
        print(f"  {'─'*22}  {'─'*5}  {'─'*6}  {'─'*7}  {'─'*7}  {'─'*9}  ───────")

        for bkt in BUCKET_ORDER:
            grp = df[df["freshness_bucket"] == bkt]
            if len(grp) == 0:
                continue
            s = _trade_stats(grp)
            note = "* directional only — n<30" if s["n"] < 30 else ""
            print(
                f"  {bkt:<22}  {s['n']:>5}  "
                f"{s['win_pct']:>5.1f}%  "
                f"{s['avg_ret']:>+7.2f}%  "
                f"{s['median_ret']:>+7.2f}%  "
                f"{s['avg_days']:>9}  {note}"
            )

    # ── Part B: Consistency across datasets ────────────────────────────────────

    print()
    print("=" * 72)
    print("  CONSISTENCY CHECK — both datasets, gap_computed trades only")
    print("=" * 72)
    print()
    print(f"  {'Bucket':<22}  {'2022+ n':>7}  {'2022+ avg':>9}  {'2019+ n':>7}  {'2019+ avg':>9}  Consistent?")
    print(f"  {'─'*22}  {'─'*7}  {'─'*9}  {'─'*7}  {'─'*9}  ─────────")

    sv1_df = dfs.get("52wh_v1", pd.DataFrame())
    sv2_df = dfs.get("52wh_v1_survivorship_10y", pd.DataFrame())

    sv1_gap = sv1_df[sv1_df["freshness_category"] == "gap_computed"] if not sv1_df.empty else pd.DataFrame()
    sv2_gap = sv2_df[sv2_df["freshness_category"] == "gap_computed"] if not sv2_df.empty else pd.DataFrame()

    for bkt in [label for _, _, label in BUCKET_DEFS]:
        g1 = sv1_gap[sv1_gap["freshness_bucket"] == bkt] if not sv1_gap.empty else pd.DataFrame()
        g2 = sv2_gap[sv2_gap["freshness_bucket"] == bkt] if not sv2_gap.empty else pd.DataFrame()
        s1 = _trade_stats(g1)
        s2 = _trade_stats(g2)
        n1 = s1["n"]; a1 = s1["avg_ret"]
        n2 = s2["n"]; a2 = s2["avg_ret"]

        a1_str = f"{a1:+.2f}%" if a1 is not None else "—"
        a2_str = f"{a2:+.2f}%" if a2 is not None else "—"

        # Consistent: both have >= 10 trades and same direction (both positive or both negative)
        if n1 >= 10 and n2 >= 10 and a1 is not None and a2 is not None:
            same_dir = (a1 >= 0) == (a2 >= 0)
            consistent = "YES" if same_dir else "NO — direction differs"
        elif n1 < 10 or n2 < 10:
            consistent = "— (n<10 in one dataset)"
        else:
            consistent = "— (no data)"

        a1_note = "*" if n1 < 30 else " "
        a2_note = "*" if n2 < 30 else " "
        print(
            f"  {bkt:<22}  {n1:>6}{a1_note}  {a1_str:>9}  {n2:>6}{a2_note}  {a2_str:>9}  {consistent}"
        )

    print("  (* = n<30 — directional only)")

    # ── Part B-5: Cross-tab freshness × regime ─────────────────────────────────

    print()
    print("=" * 72)
    print("  FRESHNESS × REGIME CROSS-TAB")
    print("  (gap_computed trades only, 52wh_v1_survivorship_10y as primary dataset)")
    print("  Q: does freshness add information WITHIN a single regime bucket?")
    print("=" * 72)

    if sv2_gap.empty:
        print("  No data for 52wh_v1_survivorship_10y.")
    else:
        for regime_val in ["below_200dma", "above_200dma"]:
            regime_grp = sv2_gap[sv2_gap["market_vs_200dma"] == regime_val]
            all_stats  = _trade_stats(regime_grp)
            print()
            print(
                f"  Market {regime_val}  ({all_stats['n']} trades, "
                f"baseline avg={all_stats['avg_ret']:+.2f}% win={all_stats['win_pct']:.1f}%)"
            )
            print(f"  {'Bucket':<22}  {'n':>5}  {'Win%':>6}  {'Avg%':>7}  {'Med%':>7}  vs baseline")
            print(f"  {'─'*22}  {'─'*5}  {'─'*6}  {'─'*7}  {'─'*7}  ─────────")

            for bkt in [lbl for _, _, lbl in BUCKET_DEFS]:
                grp = regime_grp[regime_grp["freshness_bucket"] == bkt]
                s = _trade_stats(grp)
                if s["n"] < 5:
                    continue
                delta = (s["avg_ret"] - all_stats["avg_ret"]) if all_stats["avg_ret"] else 0.0
                delta_str = f"{delta:+.2f}%"
                note = "* n<30" if s["n"] < 30 else ""
                print(
                    f"  {bkt:<22}  {s['n']:>5}  "
                    f"{s['win_pct']:>5.1f}%  "
                    f"{s['avg_ret']:>+7.2f}%  "
                    f"{s['median_ret']:>+7.2f}%  "
                    f"{delta_str}  {note}"
                )

    # ── Part B-6: first_observed_high vs rest ──────────────────────────────────

    print()
    print("=" * 72)
    print("  FIRST OBSERVED HIGH vs ALL OTHER TRADES")
    print("  (tests the 'breakout from long base' hypothesis)")
    print("=" * 72)

    for sv, label in SVS.items():
        df = dfs.get(sv, pd.DataFrame())
        if df.empty:
            continue
        foh  = df[df["freshness_bucket"] == "first_observed_high"]
        rest = df[df["freshness_bucket"] != "first_observed_high"]
        sf   = _trade_stats(foh)
        sr   = _trade_stats(rest)

        print(f"\n  {label}")
        print(f"  {'Group':<28}  {'n':>5}  {'Win%':>6}  {'Avg%':>7}  {'Med%':>7}  {'Avg Days':>9}")
        print(f"  {'─'*28}  {'─'*5}  {'─'*6}  {'─'*7}  {'─'*7}  {'─'*9}")
        for grp_name, s in [("first_observed_high", sf), ("all other (gap known)", sr)]:
            note = " *" if s["n"] < 30 else ""
            avg_s = f"{s['avg_ret']:+.2f}%" if s["avg_ret"] is not None else "—"
            med_s = f"{s['median_ret']:+.2f}%" if s["median_ret"] is not None else "—"
            win_s = f"{s['win_pct']:.1f}%" if s["win_pct"] is not None else "—"
            days_s = str(s["avg_days"]) if s["avg_days"] is not None else "—"
            print(f"  {grp_name:<28}{note}  {s['n']:>5}  {win_s:>6}  {avg_s:>7}  {med_s:>7}  {days_s:>9}")

        if sv == "52wh_v1":
            print(
                "  ← CAVEAT: 52wh_v1 cache starts 2021.  first_observed_high here includes\n"
                "     stocks whose last real signal was simply before 2021 (gap > ~15 months),\n"
                "     not necessarily a multi-year base breakout.  Use 2019+ dataset for this."
            )

    # ── Part C: Honest summary ─────────────────────────────────────────────────

    print()
    print("=" * 72)
    print("  SUMMARY — does freshness add independent value?")
    print("=" * 72)
    print("""
  See the cross-tab section above for the data-grounded answer.

  Interpretation guide:
  ─ If the freshness bucket rows within each regime group are clustered
    near the regime baseline (< 5pp avg-return spread), freshness adds
    little once regime is known.
  ─ If one bucket consistently outperforms the baseline across BOTH
    regime conditions AND both datasets, freshness is doing something
    the regime score cannot capture.
  ─ The 'first_observed_high' bucket in 52wh_v1_survivorship_10y
    (which has a trustworthy lookback) is the cleanest test of the
    "long-base breakout" hypothesis.  If it outperforms all other
    freshness buckets by >= 10pp avg return AND the effect is present
    in both datasets, that is a meaningful independent finding.
    If it merely matches the "below_200dma" regime bucket (which already
    captures crisis/recovery entries), the finding is redundant.

  NO changes to the live scanner, bot, or conviction tier are made
  by this analysis step.  Findings are advisory; implementation is
  a separate decision.
""")
