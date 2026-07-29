"""
InsiderSwing — insider conviction scoring (0–100), fully auditable.

The score is a weighted sum of four components plus one multiplier, computed
over a rolling window of FILING dates:

    cluster  (w=40)  distinct insiders with qualifying open-market buys
    role     (w=20)  seniority of the most senior buyer
    size     (w=20)  largest buy relative to that insider's OWN trailing average
    novelty  (w=20)  share of buyers making their first purchase in 12 months
    ------------------------------------------------------------------------
    × earnings-proximity penalty (0.85 when a print lands inside 14 days)

Weighting rationale (literature, not curve-fitting)
---------------------------------------------------
Cluster count carries the most weight because it is the most replicated result
in the field — Lakonishok & Lee (2001) and Jeng/Metrick/Zeckhauser (2003) both
find the abnormal return concentrated in multi-insider purchase episodes, while
isolated single buys are weak-to-noise.  Relative size is measured against the
insider's own history rather than against market cap, per the spec: a CFO
tripling their normal ticket is informative; a $1m buy is not informative in
itself if that insider buys $1m every quarter.  10%-owner buys are weighted near
zero because they are frequently fund rebalancing rather than conviction.

AUDITABILITY
------------
Every score row carries ``components_json`` containing the per-insider detail
that produced it — who bought, in what role, for how much, versus their own
average, and whether they were a first-time buyer.  A score can always be
explained after the fact, which is the same contract the Nifty and S&P 500
conviction tiers hold to.

POINT-IN-TIME
-------------
Every window boundary, every trailing average, and every novelty lookback is
computed on ``filing_date`` and is strictly backward-looking:

    window   = (d - cluster_window_days, d]     inclusive of the event day
    history  = [d - size_history_days, d)       EXCLUSIVE of the event day

The event day is excluded from an insider's own history so a buy is never
compared against itself.
"""
from __future__ import annotations

import json
import logging
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE), str(_HERE / "sources")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg      # noqa: E402
import earnings           # noqa: E402
from filters import OPEN_MARKET_BUY, OPEN_MARKET_SALE   # noqa: E402

logger = logging.getLogger(__name__)

# Cluster credit as a function of distinct buyers.  Saturating, not linear:
# the 1→2 step is the informative one; 5 buyers is not 5x better than 1.
_CLUSTER_CREDIT = {0: 0.00, 1: 0.35, 2: 0.70, 3: 0.90}
_CLUSTER_CREDIT_MAX = 1.00


def cluster_credit(n: int) -> float:
    return _CLUSTER_CREDIT.get(int(n), _CLUSTER_CREDIT_MAX if n >= 4 else 0.0)


def role_credit(bucket: str, conf: cfg.InsiderConfig) -> float:
    return {
        "ceo_cfo": conf.role_weight_ceo_cfo,
        "officer": conf.role_weight_officer,
        "director": conf.role_weight_director,
        "ten_pct": conf.role_weight_ten_pct,
    }.get(bucket, 0.0)


_ROLE_RANK = {"ceo_cfo": 4, "officer": 3, "director": 2, "ten_pct": 1, "other": 0}


# ──────────────────────────────────────────────────────────────────────────────
#  Loading
# ──────────────────────────────────────────────────────────────────────────────

def load_qualifying_transactions(
    start: Optional[date] = None,
    end: Optional[date] = None,
    tickers: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """
    Every open-market buy/sale in the DB, keyed on filing_date.

    Note the ``start`` bound is intentionally NOT applied here by callers that
    need trailing history — ``compute_scores`` widens it by the history lookback
    so an insider's trailing average is complete at the first scored event.
    """
    from db import get_engine
    from sqlalchemy import text

    where = ["classification IN (:b, :s)"]
    params: dict = {"b": OPEN_MARKET_BUY, "s": OPEN_MARKET_SALE}
    if start:
        where.append("filing_date >= :start")
        params["start"] = start.isoformat()
    if end:
        where.append("filing_date <= :end")
        params["end"] = end.isoformat()

    sql = (
        "SELECT ticker, reporting_cik, reporting_name, role_bucket, "
        "       filing_date, transaction_date, classification, value_usd, shares, "
        "       price_per_share, accession_no "
        "FROM ins_transactions WHERE " + " AND ".join(where)
    )
    with get_engine().connect() as conn:
        df = pd.read_sql(text(sql), conn, params=params)

    if df.empty:
        return df

    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df = df[df["ticker"].notna() & (df["ticker"] != "") & (df["ticker"] != "NONE")]
    if tickers is not None:
        want = {t.upper() for t in tickers}
        df = df[df["ticker"].isin(want)]

    df["filing_dt"] = pd.to_datetime(df["filing_date"], errors="coerce")
    df = df[df["filing_dt"].notna()]
    df["value_usd"] = pd.to_numeric(df["value_usd"], errors="coerce").fillna(0.0)
    df["role_bucket"] = df["role_bucket"].fillna("other")
    df["reporting_cik"] = df["reporting_cik"].fillna("")
    return df.sort_values("filing_dt").reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
#  Per-insider history index
# ──────────────────────────────────────────────────────────────────────────────

class _InsiderHistory:
    """
    Trailing-buy index per reporting CIK, across ALL issuers.

    Cross-issuer is deliberate: "this insider's normal ticket size" is a property
    of the person, and a director who sits on three boards has one wallet.
    """

    def __init__(self, buys: pd.DataFrame):
        self._dates: dict[str, list[int]] = defaultdict(list)   # ordinal day numbers
        self._values: dict[str, list[float]] = defaultdict(list)

        if buys.empty:
            return
        sub = buys[["reporting_cik", "filing_dt", "value_usd"]].sort_values("filing_dt")
        for cik, grp in sub.groupby("reporting_cik", sort=False):
            self._dates[cik] = [d.toordinal() for d in grp["filing_dt"]]
            self._values[cik] = grp["value_usd"].tolist()

    def trailing_avg(self, cik: str, as_of: date, history_days: int) -> Optional[float]:
        """
        Mean buy value by this insider in [as_of - history_days, as_of).

        Excludes the event day so a buy is never benchmarked against itself.
        None when the insider has no prior buys in the window.
        """
        dates = self._dates.get(cik)
        if not dates:
            return None
        hi = as_of.toordinal()
        lo = hi - history_days
        i = bisect_left(dates, lo)
        j = bisect_left(dates, hi)          # strictly before as_of
        if j <= i:
            return None
        vals = [v for v in self._values[cik][i:j] if v and v > 0]
        if not vals:
            return None
        return float(np.mean(vals))

    def has_prior_buy(self, cik: str, as_of: date, lookback_days: int) -> bool:
        """True if this insider bought at all in [as_of - lookback_days, as_of)."""
        dates = self._dates.get(cik)
        if not dates:
            return False
        hi = as_of.toordinal()
        lo = hi - lookback_days
        return bisect_left(dates, hi) > bisect_left(dates, lo)


# ──────────────────────────────────────────────────────────────────────────────
#  Scoring
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoreRow:
    ticker: str
    as_of_date: str
    score: float
    cluster_count: int
    cluster_component: float
    role_bucket_top: str
    role_component: float
    size_ratio_max: Optional[float]
    size_component: float
    novelty_count: int
    novelty_component: float
    buy_value_total: float
    sell_value_total: float
    sell_cluster_count: int
    sell_pressure_flag: bool
    earnings_date: Optional[str]
    earnings_proximity_flag: bool
    earnings_penalty_applied: float
    window_days: int
    components_json: str


def compute_scores(
    start: date,
    end: date,
    config: Optional[cfg.InsiderConfig] = None,
    tickers: Optional[Iterable[str]] = None,
    use_earnings: bool = True,
    txns: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Score every (ticker, filing_date) on which a qualifying buy was disclosed.

    Only days with a new qualifying buy filing are scored.  That is exactly the
    set of days on which the score can RISE, and signals are generated on a
    crossing — so no signal is missed.  (Days on which old filings roll out of
    the window can only lower the score.)

    Returns a DataFrame of ScoreRow fields, one row per scored event.
    """
    conf = config or cfg.DEFAULT_CONFIG

    if txns is None:
        # Widen the load window backwards so trailing averages and novelty
        # lookbacks are complete at the very first scored event.
        history_pad = max(conf.size_history_days, conf.novelty_lookback_days,
                          conf.cluster_window_days) + 5
        txns = load_qualifying_transactions(
            start=start - timedelta(days=history_pad), end=end, tickers=tickers
        )

    if txns is None or txns.empty:
        logger.warning("No qualifying insider transactions in range — nothing to score.")
        return pd.DataFrame()

    buys_all = txns[txns["classification"] == OPEN_MARKET_BUY]
    sells_all = txns[txns["classification"] == OPEN_MARKET_SALE]

    # Materiality floor: token purchases carry no information and would
    # otherwise inflate cluster counts.
    buys = buys_all[buys_all["value_usd"] >= conf.min_trade_value_usd]
    if conf.exclude_ten_pct_owners:
        buys = buys[buys["role_bucket"] != "ten_pct"]

    if buys.empty:
        logger.warning("No buys above the $%.0f materiality floor — nothing to score.",
                       conf.min_trade_value_usd)
        return pd.DataFrame()

    # History index built from ALL buys (including sub-floor ones): an insider's
    # normal ticket size should reflect their small buys too.
    history = _InsiderHistory(buys_all)

    weight_total = conf.w_cluster + conf.w_role + conf.w_size + conf.w_novelty
    if weight_total <= 0:
        raise ValueError("Score component weights sum to zero — check INSIDER_W_* config.")

    rows: list[ScoreRow] = []
    start_ord, end_ord = start.toordinal(), end.toordinal()

    for ticker, tgrp in buys.groupby("ticker", sort=False):
        tgrp = tgrp.sort_values("filing_dt")
        b_ord = np.array([d.toordinal() for d in tgrp["filing_dt"]])

        sgrp = sells_all[sells_all["ticker"] == ticker].sort_values("filing_dt")
        s_ord = np.array([d.toordinal() for d in sgrp["filing_dt"]]) if not sgrp.empty else np.array([])

        event_ords = sorted({int(o) for o in b_ord if start_ord <= o <= end_ord})

        for d_ord in event_ords:
            d = date.fromordinal(d_ord)
            lo = d_ord - conf.cluster_window_days

            # window is (d - N, d] — exclusive at the far end, inclusive today
            i = int(np.searchsorted(b_ord, lo, side="right"))
            j = int(np.searchsorted(b_ord, d_ord, side="right"))
            win = tgrp.iloc[i:j]
            if win.empty:
                continue

            # ── cluster ───────────────────────────────────────────────────────
            buyer_ciks = [c for c in win["reporting_cik"].unique() if c]
            cluster_count = len(buyer_ciks)
            c_comp = conf.w_cluster * cluster_credit(cluster_count)

            # ── role (most senior buyer in the window) ────────────────────────
            top_bucket = max(
                (str(b) for b in win["role_bucket"].unique()),
                key=lambda b: _ROLE_RANK.get(b, 0),
                default="other",
            )
            r_comp = conf.w_role * role_credit(top_bucket, conf)

            # ── size + novelty (per insider) ──────────────────────────────────
            per_insider: list[dict] = []
            size_ratios: list[float] = []
            novelty_count = 0

            for cik, igrp in win.groupby("reporting_cik", sort=False):
                if not cik:
                    continue
                biggest = igrp.loc[igrp["value_usd"].idxmax()]
                buy_val = float(biggest["value_usd"])
                buy_day = biggest["filing_dt"].date()

                avg = history.trailing_avg(cik, buy_day, conf.size_history_days)
                ratio = (buy_val / avg) if (avg and avg > 0) else None
                if ratio is not None:
                    size_ratios.append(ratio)

                is_novel = not history.has_prior_buy(cik, buy_day, conf.novelty_lookback_days)
                if is_novel:
                    novelty_count += 1

                per_insider.append({
                    "cik": cik,
                    "name": str(biggest["reporting_name"]),
                    "role": str(biggest["role_bucket"]),
                    "buy_value": round(buy_val, 2),
                    "own_trailing_avg": round(avg, 2) if avg else None,
                    "size_ratio": round(ratio, 2) if ratio else None,
                    "first_buy_in_lookback": is_novel,
                    "filing_date": buy_day.isoformat(),
                    "n_buys_in_window": int(len(igrp)),
                })

            if size_ratios:
                ratio_max = max(size_ratios)
                size_credit = min(ratio_max / conf.size_ratio_full_credit, 1.0)
            else:
                # No comparable history for ANY buyer in the window.  Half credit:
                # asserting either "unusually large" or "routine" would be a
                # fabrication.  Novelty already rewards genuine first-timers.
                ratio_max = None
                size_credit = 0.5
            s_comp = conf.w_size * size_credit

            n_comp = conf.w_novelty * (novelty_count / cluster_count if cluster_count else 0.0)

            raw = c_comp + r_comp + s_comp + n_comp
            score = 100.0 * raw / weight_total

            # ── earnings proximity (down-weight, never exclude) ───────────────
            if use_earnings:
                e_date, e_flag = earnings.proximity_flag(ticker, d, conf.earnings_proximity_days)
            else:
                e_date, e_flag = "not verified", False
            penalty = conf.earnings_proximity_penalty if e_flag else 1.0
            score = max(0.0, min(100.0, score * penalty))

            # ── cluster selling (caution filter, NOT a short signal) ──────────
            sell_val = 0.0
            sell_ciks = 0
            if s_ord.size:
                si = int(np.searchsorted(s_ord, lo, side="right"))
                sj = int(np.searchsorted(s_ord, d_ord, side="right"))
                swin = sgrp.iloc[si:sj]
                if not swin.empty:
                    sell_val = float(swin["value_usd"].sum())
                    sell_ciks = int(swin["reporting_cik"].nunique())

            buy_val_total = float(win["value_usd"].sum())
            sell_flag = bool(
                sell_ciks >= conf.sell_pressure_min_sellers
                and buy_val_total > 0
                and (sell_val / buy_val_total) > conf.sell_pressure_value_ratio
            )

            rows.append(ScoreRow(
                ticker=ticker,
                as_of_date=d.isoformat(),
                score=round(score, 2),
                cluster_count=cluster_count,
                cluster_component=round(c_comp, 3),
                role_bucket_top=top_bucket,
                role_component=round(r_comp, 3),
                size_ratio_max=round(ratio_max, 3) if ratio_max else None,
                size_component=round(s_comp, 3),
                novelty_count=novelty_count,
                novelty_component=round(n_comp, 3),
                buy_value_total=round(buy_val_total, 2),
                sell_value_total=round(sell_val, 2),
                sell_cluster_count=sell_ciks,
                sell_pressure_flag=sell_flag,
                earnings_date=e_date,
                earnings_proximity_flag=bool(e_flag),
                earnings_penalty_applied=penalty,
                window_days=conf.cluster_window_days,
                components_json=json.dumps({
                    "weights": {
                        "cluster": conf.w_cluster, "role": conf.w_role,
                        "size": conf.w_size, "novelty": conf.w_novelty,
                    },
                    "cluster_credit": round(cluster_credit(cluster_count), 3),
                    "role_credit": round(role_credit(top_bucket, conf), 3),
                    "size_credit": round(size_credit, 3),
                    "size_credit_basis": "half credit — no comparable insider history"
                                         if ratio_max is None else "ratio vs own trailing average",
                    "novelty_fraction": round(novelty_count / cluster_count, 3) if cluster_count else 0.0,
                    "earnings_penalty": penalty,
                    "insiders": per_insider,
                }, separators=(",", ":")),
            ))

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame([r.__dict__ for r in rows])
    logger.info(
        "Scored %d (ticker, filing-date) events across %d tickers; "
        "%d at/above threshold %.0f",
        len(out), out["ticker"].nunique(),
        int((out["score"] >= conf.conviction_threshold).sum()), conf.conviction_threshold,
    )
    return out.sort_values(["as_of_date", "score"], ascending=[True, False]).reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
#  Persistence
# ──────────────────────────────────────────────────────────────────────────────

def save_scores(scores: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> int:
    """
    Upsert scores into ins_scores keyed on (ticker, as_of_date, param_key).

    Re-running with the same parameters overwrites rather than duplicating, so
    a re-scored backfill converges instead of growing.
    """
    if scores is None or scores.empty:
        return 0
    conf = config or cfg.DEFAULT_CONFIG
    pk = conf.param_key()

    from db import get_engine
    from sqlalchemy import text

    df = scores.copy()
    df["param_key"] = pk
    # to_sql bypasses the ORM, so SQLAlchemy column defaults don't fire —
    # populate the NOT NULL bookkeeping columns explicitly.
    df["strategy_version"] = cfg.STRATEGY_VERSION
    df["created_at"] = datetime.utcnow().isoformat()
    df = df.drop(columns=[c for c in ("status", "block_reason") if c in df.columns])

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ins_scores WHERE param_key = :pk "
                 "AND as_of_date BETWEEN :lo AND :hi"),
            {"pk": pk, "lo": df["as_of_date"].min(), "hi": df["as_of_date"].max()},
        )
        df.to_sql("ins_scores", conn, if_exists="append", index=False)
    logger.info("Saved %d score rows (param_key=%s)", len(df), pk)
    return len(df)


def load_scores(
    start: Optional[date] = None,
    end: Optional[date] = None,
    param_key: Optional[str] = None,
    min_score: Optional[float] = None,
) -> pd.DataFrame:
    from db import get_engine
    from sqlalchemy import text

    where, params = ["1=1"], {}
    if start:
        where.append("as_of_date >= :start"); params["start"] = start.isoformat()
    if end:
        where.append("as_of_date <= :end"); params["end"] = end.isoformat()
    if param_key:
        where.append("param_key = :pk"); params["pk"] = param_key
    if min_score is not None:
        where.append("score >= :ms"); params["ms"] = min_score

    with get_engine().connect() as conn:
        return pd.read_sql(
            text("SELECT * FROM ins_scores WHERE " + " AND ".join(where)
                 + " ORDER BY as_of_date, score DESC"),
            conn, params=params,
        )


def qualifying_signals(scores: pd.DataFrame, config: Optional[cfg.InsiderConfig] = None) -> pd.DataFrame:
    """
    Scores that clear the threshold, with the sell-pressure veto applied.

    Blocked rows are RETAINED with status='blocked' rather than dropped — the
    dashboard shows them, and the backtest reports how many entries the caution
    filter cost.
    """
    conf = config or cfg.DEFAULT_CONFIG
    if scores is None or scores.empty:
        return pd.DataFrame()

    out = scores[scores["score"] >= conf.conviction_threshold].copy()
    if out.empty:
        return out

    if conf.sell_pressure_blocks_entry:
        out["status"] = np.where(out["sell_pressure_flag"], "blocked", "pending")
        out["block_reason"] = np.where(
            out["sell_pressure_flag"],
            "cluster selling exceeds cluster buying in window",
            None,
        )
    else:
        out["status"] = "pending"
        out["block_reason"] = None

    return out.reset_index(drop=True)
