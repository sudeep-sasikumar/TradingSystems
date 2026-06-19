"""
Regime Analysis Tab — Checkpoint 8.

Visualises market + sector regime tags for backtest trades.
Supports both strategy versions via a dataset selector:
  - 52wh_v1                  : original 2022-present backtest
  - 52wh_v1_survivorship_10y : survivorship-corrected 2019-present

Data source: trade_regime_tags table (written by run_regime_analysis.py --checkpoint tag).
Analysis-only — no changes to trade outcomes or live scanner.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent.parent
_52WH = _ROOT / "52WeekHigh"
for _d in (str(_ROOT), str(_52WH)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from shared.db import get_engine
from analysis.freshness_tagger import (
    BUCKET_ORDER as FRESHNESS_BUCKET_ORDER,
    assign_bucket as freshness_assign_bucket,
    load_freshness_df,
)

MIN_COUNT  = 30
TRADE_SIZE = 1_000   # Rs. per trade for the flat-allocation simulation

DATASETS = {
    "2022 – present  (original backtest, Nifty 500 current list)":
        "52wh_v1",
    "2019 – present  (survivorship-corrected, actual membership)":
        "52wh_v1_survivorship_10y",
}

QUINTILE_ORDER = [
    "strong_downtrend",
    "moderate_downtrend",
    "flat",
    "moderate_uptrend",
    "strong_uptrend",
]

TIER_ORDER = ["HIGH", "STANDARD", "AVOID"]


# ── Conviction tier helpers ────────────────────────────────────────────────────

def _assign_conviction(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute additive regime score and assign 3-tier conviction label to each trade.

    Score:  +1 market below 200-DMA
            +1 market 6M in bottom-2 quintiles
            +1 synthetic basket above 200-DMA  (0 when tag unavailable)
            -1 market above 200-DMA AND strong_uptrend

    Tier:   HIGH     = market bottom-2 quintile AND basket above 200-DMA
            AVOID    = market strong_uptrend quintile
            STANDARD = everything else
    """
    df = df.copy()
    score = pd.Series(0, index=df.index)
    score += (df["market_vs_200dma"] == "below_200dma").astype(int)
    score += df["market_6m_quintile"].isin(["strong_downtrend", "moderate_downtrend"]).astype(int)
    score += (df["synthetic_vs_200dma"].fillna("") == "above_200dma").astype(int)
    penalty = (
        (df["market_vs_200dma"] == "above_200dma")
        & (df["market_6m_quintile"] == "strong_uptrend")
    )
    score -= penalty.astype(int)
    df["regime_score"] = score

    is_avoid    = df["market_6m_quintile"] == "strong_uptrend"
    is_bottom2  = df["market_6m_quintile"].isin(["strong_downtrend", "moderate_downtrend"])
    is_bsk_abv  = df["synthetic_vs_200dma"] == "above_200dma"

    tier = pd.Series("STANDARD", index=df.index)
    tier[is_bottom2 & is_bsk_abv] = "HIGH"
    tier[is_avoid]                = "AVOID"   # overrides (strong_uptrend can't also be bottom-2)
    df["conviction_tier"] = tier
    return df


# ── Rs.1,000/trade simulation helpers ─────────────────────────────────────────

def _sim_stats(grp: pd.DataFrame) -> dict:
    n = len(grp)
    if n == 0:
        return dict(n=0, deployed=0, pnl=0.0, ret=0.0, win_pct=0.0)
    deployed = n * TRADE_SIZE
    pnl      = (grp["return_pct"] / 100 * TRADE_SIZE).sum()
    wins     = int((grp["return_pct"] > 0).sum())
    return dict(
        n        = n,
        deployed = deployed,
        pnl      = pnl,
        final    = deployed + pnl,
        ret      = pnl / deployed * 100,
        win_pct  = wins / n * 100,
        max_gain = float((grp["return_pct"] / 100 * TRADE_SIZE).max()),
        max_loss = float((grp["return_pct"] / 100 * TRADE_SIZE).min()),
    )


def _inr(v: float) -> str:
    sign  = "-" if v < 0 else ""
    abs_v = abs(v)
    if abs_v >= 1_00_000:
        return f"{sign}Rs.{abs_v / 1_00_000:.2f}L"
    return f"{sign}Rs.{abs_v:,.0f}"


@st.cache_data(ttl=300)
def _load_tagged_closed(strategy_version: str) -> pd.DataFrame:
    engine = get_engine()
    try:
        df = pd.read_sql(
            """
            SELECT t.id, t.ticker, t.entry_date, t.trade_year,
                   t.return_pct, t.holding_days,
                   r.market_index_used,
                   r.market_vs_200dma, r.market_6m_quintile,
                   r.market_dist_200dma_pct, r.market_6m_return_pct,
                   r.official_sector,
                   r.official_vs_200dma, r.official_6m_quintile,
                   r.official_dist_200dma_pct, r.official_6m_return_pct,
                   r.industry_group, r.synthetic_basket_size,
                   r.synthetic_vs_200dma, r.synthetic_6m_quintile,
                   r.synthetic_dist_200dma_pct, r.synthetic_6m_return_pct,
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
            ORDER BY t.entry_date
            """,
            engine,
            params={"sv": strategy_version},
        )
        if df.empty:
            return df
        df = _assign_conviction(df)
        # Freshness bucket (NULL → "—" handled in display)
        if "freshness_category" in df.columns:
            df["freshness_bucket"] = df.apply(
                lambda r: freshness_assign_bucket(
                    r["freshness_category"] or "insufficient_history",
                    r.get("freshness_gap_td"),
                )
                if pd.notna(r.get("freshness_category"))
                else None,
                axis=1,
            )
        return df
    except Exception:
        return pd.DataFrame()


# ── Stats / breakdown helpers ──────────────────────────────────────────────────

def _stats(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {"n": 0, "Win%": None, "Avg Ret%": None, "Median%": None, "Avg Days": None}
    wins = (df["return_pct"] > 0).sum()
    return {
        "n":        n,
        "Win%":     round(wins / n * 100, 1),
        "Avg Ret%": round(df["return_pct"].mean(), 2),
        "Median%":  round(df["return_pct"].median(), 2),
        "Avg Days": int(round(df["holding_days"].mean())),
    }


def _breakdown(df: pd.DataFrame, col: str) -> pd.DataFrame:
    sub = df.dropna(subset=[col])
    rows = []
    vals = sub[col].unique()
    order = [v for v in QUINTILE_ORDER if v in vals] if all(v in QUINTILE_ORDER for v in vals) else sorted(vals)
    for val in order:
        s = _stats(sub[sub[col] == val])
        s["Regime"] = val
        s["!"] = "*" if s["n"] < MIN_COUNT else ""
        rows.append(s)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)[["Regime", "n", "Win%", "Avg Ret%", "Median%", "Avg Days", "!"]]


def _top_combos(df: pd.DataFrame, cols: list[str], top_n: int = 5, worst: bool = False) -> pd.DataFrame:
    rows = []
    for c1, c2 in itertools.combinations(cols, 2):
        sub = df.dropna(subset=[c1, c2])
        for (v1, v2), grp in sub.groupby([c1, c2]):
            if len(grp) < MIN_COUNT:
                continue
            s = _stats(grp)
            s[c1] = v1
            s[c2] = v2
            rows.append(s)
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).sort_values("Avg Ret%", ascending=worst)
    all_cols = list(dict.fromkeys([c1, c2, "n", "Win%", "Avg Ret%", "Median%", "Avg Days"]))
    return result[[c for c in all_cols if c in result.columns]].head(top_n)


_NUM_CFG = {
    "Win%":     st.column_config.NumberColumn(format="%.1f%%"),
    "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
    "Median%":  st.column_config.NumberColumn(format="%.2f%%"),
}


# ── Freshness Factor helpers ───────────────────────────────────────────────────

_FRESHNESS_NUM_CFG = {
    "Win%":     st.column_config.NumberColumn(format="%.1f%%"),
    "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
    "Median%":  st.column_config.NumberColumn(format="%.2f%%"),
}


def _freshness_stats(grp: pd.DataFrame) -> dict:
    n = len(grp)
    if n == 0:
        return {"n": 0, "Win%": None, "Avg Ret%": None, "Median%": None, "Avg Days": None}
    wins = (grp["return_pct"] > 0).sum()
    return {
        "n":        n,
        "Win%":     round(wins / n * 100, 1),
        "Avg Ret%": round(float(grp["return_pct"].mean()), 2),
        "Median%":  round(float(grp["return_pct"].median()), 2),
        "Avg Days": int(round(grp["holding_days"].mean())),
    }


def _render_freshness_tab(df: pd.DataFrame, strategy_version: str, dataset_label: str) -> None:
    """Render the Freshness Factor inner tab."""
    st.markdown("#### Freshness Factor — Time Since Prior 52-Week High")
    st.caption(
        "For each trade, measures the gap between the entry signal and the previous time "
        "the same stock crossed its 252-day high. Computed strictly point-in-time (no lookahead). "
        "Requires `--checkpoint freshness` to be run after regime tagging."
    )

    freshness_tagged = "freshness_bucket" in df.columns and df["freshness_bucket"].notna().any()

    if not freshness_tagged:
        st.warning(
            "No freshness data found for this dataset.\n\n"
            "Run:\n```\n"
            f"python 52WeekHigh/run_regime_analysis.py --checkpoint freshness "
            f"--strategy-version {strategy_version}\n```\n\n"
            "Then re-run `--checkpoint freshness` if you re-ran `--checkpoint tag`."
        )
        return

    # Coverage summary
    fresh_df = df[df["freshness_bucket"].notna()]
    cat_counts = fresh_df["freshness_category"].value_counts() if "freshness_category" in fresh_df.columns else {}
    n_gap      = int(cat_counts.get("gap_computed", 0))
    n_foh      = int(cat_counts.get("first_observed_high", 0))
    n_insuf    = int(cat_counts.get("insufficient_history", 0))

    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric("Total Freshness-Tagged", f"{len(fresh_df):,}")
    fc2.metric("Gap Computed",           f"{n_gap:,}")
    fc3.metric("First Observed High",    f"{n_foh:,}")
    fc4.metric("Insufficient History",   f"{n_insuf:,}")

    if strategy_version == "52wh_v1":
        st.info(
            "**Lookback caveat (2022-present dataset):** price cache starts 2021-01-01.  "
            "`first_observed_high` here may simply mean the last signal was before 2021 — "
            "not necessarily a multi-year base breakout.  "
            "The 2019-present dataset has a longer lookback and is more reliable for this analysis."
        )

    st.divider()

    # ── Gap distribution ───────────────────────────────────────────────────────
    gap_df = fresh_df[fresh_df["freshness_category"] == "gap_computed"]["freshness_gap_td"].dropna()
    if len(gap_df) > 0:
        st.markdown(f"#### Gap Distribution (trading days, n={len(gap_df):,} gap-computed trades)")

        pcts = [0, 5, 10, 25, 50, 75, 90, 95, 100]
        vals = [float(gap_df.quantile(p / 100)) for p in pcts]
        pct_rows = [
            {
                "Percentile": f"P{p}",
                "Gap (td)":   int(round(v)),
                "≈ Calendar": f"{round(int(round(v)) * 365 / 252)}d",
            }
            for p, v in zip(pcts, vals)
        ]
        col_dist, col_hist = st.columns([1, 2])
        with col_dist:
            st.dataframe(pd.DataFrame(pct_rows), hide_index=True, use_container_width=True)
        with col_hist:
            hist_vals = gap_df.clip(upper=gap_df.quantile(0.98))   # clip extreme outliers for display
            fig = go.Figure(go.Histogram(
                x=hist_vals, nbinsx=40,
                marker_color="#4a9edd",
                marker_line=dict(width=0.5, color="rgba(0,0,0,0.3)"),
            ))
            fig.add_vline(x=5,   line_dash="dot", line_color="#aaa", annotation_text="1wk")
            fig.add_vline(x=22,  line_dash="dot", line_color="#aaa", annotation_text="1m")
            fig.add_vline(x=130, line_dash="dot", line_color="#aaa", annotation_text="6m")
            fig.add_vline(x=252, line_dash="dot", line_color="#aaa", annotation_text="1yr")
            fig.add_vline(x=756, line_dash="dot", line_color="#aaa", annotation_text="3yr")
            fig.update_layout(
                title="Gap Distribution (clipped at P98)",
                xaxis_title="Trading days since prior 52wh signal",
                yaxis_title="Trades",
                height=260,
                margin=dict(t=40, b=30, l=40, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ── Bucket breakdown ───────────────────────────────────────────────────────
    st.markdown("#### Performance by Freshness Bucket")
    st.caption(
        "Bucket boundaries (trading days): "
        "< 1 wk = 1–4 · 1w–1m = 5–21 · 1–6m = 22–129 · "
        "6–12m = 130–251 · 1–3yr = 252–755 · 3yr+ = 756+. "
        "\\* = fewer than 30 trades — directional only."
    )

    bkt_rows = []
    for bkt in FRESHNESS_BUCKET_ORDER:
        grp = fresh_df[fresh_df["freshness_bucket"] == bkt]
        if len(grp) == 0:
            continue
        s = _freshness_stats(grp)
        bkt_rows.append({
            "Freshness Bucket": bkt,
            "n":        s["n"],
            "Win%":     s["Win%"],
            "Avg Ret%": s["Avg Ret%"],
            "Median%":  s["Median%"],
            "Avg Days": s["Avg Days"],
            "Note":     "* n<30" if s["n"] < MIN_COUNT else "",
        })

    if bkt_rows:
        bkt_tbl = pd.DataFrame(bkt_rows)
        st.dataframe(bkt_tbl, hide_index=True, use_container_width=False,
                     column_config=_FRESHNESS_NUM_CFG)

        # Bar chart: avg return by bucket (gap_computed buckets only)
        gap_rows = [r for r in bkt_rows
                    if r["Freshness Bucket"] not in ("insufficient_history", "first_observed_high")]
        if gap_rows:
            fig2 = go.Figure(go.Bar(
                x=[r["Freshness Bucket"] for r in gap_rows],
                y=[r["Avg Ret%"] for r in gap_rows],
                marker_color=[
                    "#2ecc71" if (r["Avg Ret%"] or 0) >= 0 else "#e74c3c"
                    for r in gap_rows
                ],
                text=[f"{r['Avg Ret%']:+.1f}%" for r in gap_rows],
                textposition="outside",
            ))
            fig2.update_layout(
                title="Avg Return by Freshness Bucket (gap-computed trades)",
                xaxis_title="Freshness bucket",
                yaxis_title="Avg Return %",
                height=300,
                margin=dict(t=50, b=10),
            )
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # ── first_observed_high vs rest ────────────────────────────────────────────
    st.markdown("#### First Observed High vs All Other Trades")
    st.caption(
        "The single comparison that most directly tests the 'breakout from long base' hypothesis. "
        "first\\_observed\\_high = stock crossed its 252-day high for the first time in the available "
        "price history — no prior signal found in the cache window."
    )

    foh_grp  = fresh_df[fresh_df["freshness_bucket"] == "first_observed_high"]
    rest_grp = fresh_df[fresh_df["freshness_bucket"] != "first_observed_high"]
    sf = _freshness_stats(foh_grp)
    sr = _freshness_stats(rest_grp)

    comp_rows = []
    for label_g, s in [("first_observed_high", sf), ("all other (gap known + insufficient)", sr)]:
        comp_rows.append({
            "Group":    label_g,
            "n":        s["n"],
            "Win%":     s["Win%"],
            "Avg Ret%": s["Avg Ret%"],
            "Median%":  s["Median%"],
            "Avg Days": s["Avg Days"],
            "Note":     "* n<30" if s["n"] < MIN_COUNT else "",
        })
    if comp_rows:
        st.dataframe(pd.DataFrame(comp_rows), hide_index=True, use_container_width=False,
                     column_config=_FRESHNESS_NUM_CFG)

    st.divider()

    # ── Freshness × regime cross-tab ───────────────────────────────────────────
    st.markdown("#### Freshness × Market Regime Cross-Tab")
    st.caption(
        "Does freshness add information WITHIN a single regime bucket? "
        "If rows within a regime group cluster near the group baseline (< 5pp spread), "
        "freshness is mostly redundant with regime. "
        "A consistent spread > 10pp across buckets suggests independent signal."
    )

    gap_only = fresh_df[fresh_df["freshness_category"] == "gap_computed"]

    for regime_val, regime_label in [("below_200dma", "Market BELOW 200-DMA"), ("above_200dma", "Market ABOVE 200-DMA")]:
        regime_grp = gap_only[gap_only["market_vs_200dma"] == regime_val]
        if len(regime_grp) < 5:
            continue
        baseline = _freshness_stats(regime_grp)
        st.markdown(
            f"**{regime_label}** — {baseline['n']} trades · "
            f"baseline avg {baseline['Avg Ret%']:+.2f}% · win {baseline['Win%']:.1f}%"
        )

        xt_rows = []
        for bkt, _, lbl in [("< 1 week", None, "< 1 week"),
                             ("1w – 1m", None, "1w – 1m"),
                             ("1 – 6 months", None, "1 – 6 months"),
                             ("6 – 12 months", None, "6 – 12 months"),
                             ("1 – 3 years", None, "1 – 3 years"),
                             ("3+ years", None, "3+ years")]:
            grp = regime_grp[regime_grp["freshness_bucket"] == bkt]
            s = _freshness_stats(grp)
            if s["n"] < 5:
                continue
            delta = round((s["Avg Ret%"] or 0) - (baseline["Avg Ret%"] or 0), 2)
            xt_rows.append({
                "Freshness Bucket": bkt,
                "n":        s["n"],
                "Win%":     s["Win%"],
                "Avg Ret%": s["Avg Ret%"],
                "vs baseline": f"{delta:+.2f}%",
                "Note":     "* n<30" if s["n"] < MIN_COUNT else "",
            })

        if xt_rows:
            st.dataframe(pd.DataFrame(xt_rows), hide_index=True, use_container_width=False,
                         column_config={
                             "Win%":     st.column_config.NumberColumn(format="%.1f%%"),
                             "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                         })

    with st.expander("How to interpret this analysis"):
        st.markdown(
            """
**Redundant with regime:** freshness is a proxy for regime if the cross-tab shows that
"short gap" trades cluster in bull/calm conditions and "long gap" trades cluster in bear/
stressed conditions. In that case, controlling for regime removes most of the freshness effect.

**Independent signal:** freshness adds value if, within a fixed regime bucket (e.g., all
"below\\_200dma" trades), the freshness breakdown still shows a meaningful performance split
(> 10pp avg-return spread, consistent direction, ≥ 30 trades per cell).

**first\\_observed\\_high caveat:** for the 2022-present dataset, the lookback starts
January 2021, so `first_observed_high` = "last signal before Jan 2021" — not necessarily
a multi-year base breakout. The 2019-present dataset is more reliable for this comparison
(lookback starts January 2018).
            """
        )


# ── Main render ───────────────────────────────────────────────────────────────

def render_tab() -> None:
    st.subheader("Regime Analysis — 52-Week High")
    st.caption("Market and sector regime as of each trade's entry date. Analysis only — trade outcomes unchanged.")

    # Dataset selector
    dataset_label = st.selectbox("Dataset", list(DATASETS.keys()), key="regime_dataset")
    strategy_version = DATASETS[dataset_label]

    df = _load_tagged_closed(strategy_version)

    if df.empty:
        sv_short = strategy_version
        st.warning(
            f"No regime-tagged trades found for **{sv_short}**.\n\n"
            "Run:\n```\n"
            f"python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version {sv_short}\n"
            "```"
        )
        return

    n = len(df)
    mkt_ok = df["market_vs_200dma"].notna().sum()
    sec_ok = df["official_vs_200dma"].notna().sum()
    syn_ok = df["synthetic_vs_200dma"].notna().sum()

    # Coverage metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Closed Trades", f"{n:,}")
    c2.metric("Market Tagged", "100%")
    c3.metric("Official Sector", f"{sec_ok/n*100:.0f}%",
              help="Only 7 industries map cleanly to an available NSE sectoral index")
    c4.metric("Synthetic Basket", f"{syn_ok/n*100:.0f}%",
              help="Equal-weighted industry baskets built from stock price data (≥10 stocks per industry)")

    market_ticker = df["market_index_used"].dropna().iloc[0] if mkt_ok > 0 else "—"
    st.caption(
        f"Market index: **{market_ticker}** | "
        "Official sector: Nifty Auto, Bank, IT, Pharma, FMCG, Metal, Realty, Media, Energy, PSU Bank | "
        "Synthetic: equal-weighted industry baskets from baseline CSV | "
        f"\\* = fewer than {MIN_COUNT} trades"
    )

    st.divider()

    inner = st.tabs(["Market Regime", "Sector Regime", "Top Combinations", "Explorer", "Rs.1,000 Simulation", "Freshness Factor"])

    # ── Market Regime ──────────────────────────────────────────────────────────
    with inner[0]:
        st.markdown("#### Market 200-DMA  (^CRSLDX Nifty 500)")
        res = _breakdown(df, "market_vs_200dma")
        if not res.empty:
            st.dataframe(res, hide_index=True, use_container_width=False, column_config=_NUM_CFG)

        st.markdown("#### Market 6-Month Return Quintile")
        res_q = _breakdown(df, "market_6m_quintile")
        if not res_q.empty:
            st.dataframe(res_q, hide_index=True, use_container_width=False, column_config=_NUM_CFG)
            fig = go.Figure(go.Bar(
                x=res_q["Regime"],
                y=res_q["Avg Ret%"],
                marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in res_q["Avg Ret%"]],
                text=[f"{v:.1f}%" for v in res_q["Avg Ret%"]],
                textposition="outside",
            ))
            fig.update_layout(
                title="Avg Return by Market 6M Quintile (entry-date regime)",
                xaxis_title="Market Quintile",
                yaxis_title="Avg Return %",
                height=300,
                margin=dict(t=50, b=10),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── Sector Regime ──────────────────────────────────────────────────────────
    with inner[1]:
        col_off, col_syn = st.columns(2)

        with col_off:
            st.markdown("#### Official Sector 200-DMA")
            st.caption(
                f"{sec_ok:,} / {n:,} trades ({sec_ok/n*100:.0f}%) — "
                "PHARMA, AUTO, IT, METALS, OIL & GAS, POWER, MEDIA"
            )
            res = _breakdown(df, "official_vs_200dma")
            if not res.empty:
                st.dataframe(res, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

            st.markdown("#### Official Sector 6M Quintile")
            res_q = _breakdown(df, "official_6m_quintile")
            if not res_q.empty:
                st.dataframe(res_q, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

        with col_syn:
            st.markdown("#### Synthetic Basket 200-DMA")
            st.caption(f"{syn_ok:,} / {n:,} trades ({syn_ok/n*100:.0f}%) — all industries ≥10 stocks")
            res = _breakdown(df, "synthetic_vs_200dma")
            if not res.empty:
                st.dataframe(res, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

            st.markdown("#### Synthetic Basket 6M Quintile")
            res_q = _breakdown(df, "synthetic_6m_quintile")
            if not res_q.empty:
                st.dataframe(res_q, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

    # ── Top Combinations ───────────────────────────────────────────────────────
    with inner[2]:
        active_cols = [
            c for c in [
                "market_vs_200dma", "market_6m_quintile",
                "official_vs_200dma", "official_6m_quintile",
                "synthetic_vs_200dma", "synthetic_6m_quintile",
            ]
            if df[c].notna().sum() >= MIN_COUNT
        ]

        st.markdown(f"#### Best 5 Pairs by Avg Return  (min {MIN_COUNT} trades)")
        tops = _top_combos(df, active_cols, top_n=5, worst=False)
        if not tops.empty:
            st.dataframe(tops, hide_index=True, use_container_width=True, column_config=_NUM_CFG)
        else:
            st.info(f"No pair meets the {MIN_COUNT}-trade minimum.")

        st.markdown(f"#### Worst 5 Pairs by Avg Return  (min {MIN_COUNT} trades)")
        bots = _top_combos(df, active_cols, top_n=5, worst=True)
        if not bots.empty:
            st.dataframe(bots, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

        st.caption("Equal-weight, unlimited capital, no transaction costs — not a real portfolio simulation.")

    # ── Explorer ───────────────────────────────────────────────────────────────
    with inner[3]:
        st.markdown("#### Filter Trades by Regime")

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            mkt_filter = st.selectbox(
                "Market 200-DMA", ["All", "above_200dma", "below_200dma"], key="reg_mkt_dma"
            )
            mkt_q_filter = st.selectbox(
                "Market 6M Quintile", ["All"] + QUINTILE_ORDER, key="reg_mkt_q"
            )
        with col_b:
            sec_filter = st.selectbox(
                "Official Sector 200-DMA", ["All", "above_200dma", "below_200dma"], key="reg_sec_dma"
            )
            syn_filter = st.selectbox(
                "Synthetic Basket 6M Quintile", ["All"] + QUINTILE_ORDER, key="reg_syn_q"
            )
        with col_c:
            tier_filter = st.selectbox(
                "Conviction Tier", ["All"] + TIER_ORDER, key="reg_tier",
                help="HIGH = bottom-2 market quintile + basket above 200-DMA. "
                     "AVOID = market strong_uptrend. STANDARD = everything else.",
            )

        view = df.copy()
        if mkt_filter != "All":
            view = view[view["market_vs_200dma"] == mkt_filter]
        if mkt_q_filter != "All":
            view = view[view["market_6m_quintile"] == mkt_q_filter]
        if sec_filter != "All":
            view = view[view["official_vs_200dma"] == sec_filter]
        if syn_filter != "All":
            view = view[view["synthetic_6m_quintile"] == syn_filter]
        if tier_filter != "All" and "conviction_tier" in view.columns:
            view = view[view["conviction_tier"] == tier_filter]

        s = _stats(view)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trades", f"{s['n']:,}")
        m2.metric("Win Rate", f"{s['Win%']}%" if s["Win%"] is not None else "—")
        m3.metric("Avg Return", f"{s['Avg Ret%']}%" if s["Avg Ret%"] is not None else "—")
        m4.metric("Avg Days", str(s["Avg Days"]) if s["Avg Days"] is not None else "—")
        if "conviction_tier" in view.columns and s["n"] > 0:
            sim = _sim_stats(view)
            m5.metric("Sim P&L (Rs.1k/trade)", _inr(sim["pnl"]))
        if s["n"] > 0 and s["n"] < MIN_COUNT:
            st.caption(f"\\* Fewer than {MIN_COUNT} trades — interpret with caution.")

        if not view.empty:
            disp_cols_src = [
                "ticker", "entry_date", "trade_year",
                "conviction_tier", "regime_score",
                "market_vs_200dma", "market_6m_quintile",
                "official_sector", "official_vs_200dma", "official_6m_quintile",
                "industry_group", "synthetic_6m_quintile",
                "return_pct", "holding_days",
            ]
            disp_cols_lbl = [
                "Ticker", "Entry Date", "Year",
                "Conviction", "Score",
                "Mkt 200-DMA", "Mkt 6M Q",
                "Sector", "Sec 200-DMA", "Sec 6M Q",
                "Industry", "Basket 6M Q",
                "Return %", "Days",
            ]
            present = [(s, l) for s, l in zip(disp_cols_src, disp_cols_lbl) if s in view.columns]
            display = view[[s for s, _ in present]].copy()
            display.columns = [l for _, l in present]
            st.dataframe(
                display.reset_index(drop=True),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Return %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Score":    st.column_config.NumberColumn(format="%d"),
                },
            )

    # ── Rs.1,000 Simulation ────────────────────────────────────────────────────
    with inner[4]:
        st.markdown(
            "#### Rs.1,000/Trade Flat Simulation — Illustrative Only\n\n"
            "> Equal Rs.1,000 per trade · no compounding · no reinvestment · "
            "no transaction costs · unlimited capital assumption. "
            "P&L = return\\_pct / 100 × Rs.1,000 per trade."
        )

        sv_label = strategy_version.replace("_", " ")
        st.caption(f"Dataset: **{dataset_label}** | {n:,} closed trades with regime tags")

        # ── Overall ───────────────────────────────────────────────────────────
        st.markdown("##### Overall")
        s_all = _sim_stats(df)
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Trades",         f"{s_all['n']:,}")
        c2.metric("Deployed",       _inr(s_all["deployed"]))
        c3.metric("Total P&L",      _inr(s_all["pnl"]))
        c4.metric("Final Value",    _inr(s_all["final"]))
        c5.metric("Return %",       f"{s_all['ret']:.1f}%")
        c6.metric("Win Rate",       f"{s_all['win_pct']:.1f}%")

        st.divider()

        # ── Year-by-year ──────────────────────────────────────────────────────
        st.markdown("##### Year-by-Year")
        yr_rows = []
        cumulative = 0.0
        for yr in sorted(df["trade_year"].dropna().unique()):
            g = df[df["trade_year"] == yr]
            s = _sim_stats(g)
            cumulative += s["pnl"]
            yr_rows.append({
                "Year":      int(yr),
                "Trades":    s["n"],
                "Deployed":  _inr(s["deployed"]),
                "P&L":       _inr(s["pnl"]),
                "Ret%":      round(s["ret"], 1),
                "Win%":      round(s["win_pct"], 1),
                "Cum P&L":   _inr(cumulative),
                "Note":      "directional only" if s["n"] < MIN_COUNT else "",
            })
        yr_df = pd.DataFrame(yr_rows)
        st.dataframe(
            yr_df, hide_index=True, use_container_width=False,
            column_config={
                "Ret%": st.column_config.NumberColumn("Ret%",  format="%.1f%%"),
                "Win%": st.column_config.NumberColumn("Win%",  format="%.1f%%"),
            },
        )

        st.divider()

        # ── Quintile breakdown ────────────────────────────────────────────────
        st.markdown("##### Market 6M Quintile Breakdown")
        tagged_q = df.dropna(subset=["market_6m_quintile"])
        q_rows = []
        for q in QUINTILE_ORDER:
            g = tagged_q[tagged_q["market_6m_quintile"] == q]
            s = _sim_stats(g)
            q_rows.append({
                "Quintile": q,
                "Trades":   s["n"],
                "Deployed": _inr(s["deployed"]),
                "P&L":      _inr(s["pnl"]),
                "Ret%":     round(s["ret"], 1),
                "Win%":     round(s["win_pct"], 1),
                "Note":     "directional only" if s["n"] < MIN_COUNT else "",
            })
        q_df = pd.DataFrame(q_rows)
        st.dataframe(
            q_df, hide_index=True, use_container_width=False,
            column_config={
                "Ret%": st.column_config.NumberColumn("Ret%", format="%.1f%%"),
                "Win%": st.column_config.NumberColumn("Win%", format="%.1f%%"),
            },
        )

        st.divider()

        # ── Conviction tier breakdown ─────────────────────────────────────────
        st.markdown("##### Conviction Tier Breakdown")
        if "conviction_tier" in df.columns:
            tier_rows = []
            for tier in TIER_ORDER:
                g = df[df["conviction_tier"] == tier]
                s = _sim_stats(g)
                tier_rows.append({
                    "Tier":      tier,
                    "Trades":    s["n"],
                    "Deployed":  _inr(s["deployed"]),
                    "P&L":       _inr(s["pnl"]),
                    "Ret%":      round(s["ret"], 1),
                    "Win%":      round(s["win_pct"], 1),
                    "Note":      "directional only" if s["n"] < MIN_COUNT else "",
                })
            tier_df = pd.DataFrame(tier_rows)
            st.dataframe(
                tier_df, hide_index=True, use_container_width=False,
                column_config={
                    "Ret%": st.column_config.NumberColumn("Ret%", format="%.1f%%"),
                    "Win%": st.column_config.NumberColumn("Win%", format="%.1f%%"),
                },
            )

        st.divider()

        # ── Score distribution ────────────────────────────────────────────────
        st.markdown("##### Score Distribution (non-AVOID trades)")
        if "regime_score" in df.columns:
            non_avoid = df[df["market_6m_quintile"] != "strong_uptrend"].dropna(subset=["regime_score"])
            sc_rows = []
            for sc in sorted(non_avoid["regime_score"].unique()):
                g = non_avoid[non_avoid["regime_score"] == sc]
                s = _sim_stats(g)
                sc_rows.append({
                    "Score":  int(sc),
                    "Trades": s["n"],
                    "P&L":    _inr(s["pnl"]),
                    "Ret%":   round(s["ret"], 1),
                    "Win%":   round(s["win_pct"], 1),
                    "Note":   "directional only" if s["n"] < MIN_COUNT else "",
                })
            sc_df = pd.DataFrame(sc_rows)
            st.dataframe(
                sc_df, hide_index=True, use_container_width=False,
                column_config={
                    "Ret%": st.column_config.NumberColumn("Ret%", format="%.1f%%"),
                    "Win%": st.column_config.NumberColumn("Win%", format="%.1f%%"),
                },
            )

        with st.expander("What this simulation does NOT tell you"):
            st.markdown(
                """
**1. No compounding.** Each Rs.1,000 is a fresh independent bet. In a real portfolio,
early profits would change the capital base for later trades.

**2. No position sizing.** Every trade gets exactly Rs.1,000 regardless of conviction,
volatility, or sector concentration.

**3. No transaction costs.** Each equity delivery trade incurs STT (0.1% round-trip),
brokerage, SEBI fees, and slippage — roughly Rs.5–15 per Rs.1,000 trade (0.5–1.5%
friction per round-trip). At 1,000+ trades this erodes Rs.5,000–15,000 from the
total P&L shown above.

**4. No capital constraint.** This assumes enough cash to fund all concurrent open
positions simultaneously. In practice, 52-week-high breakouts overlap heavily — at
peak you might have 20–50 open positions concurrently.

**5. Survivorship bias** (original 2022-present dataset only). The current-list backtest
excludes delisted or dropped stocks; their worst trades are missing from the record.
The survivorship-corrected dataset partially addresses this.

**What the Rs. figures ARE good for:** seeing regime differences in money terms rather
than just percentages. A HIGH-tier trade averaging +47% vs STANDARD averaging +26% looks
very different when expressed as Rs.470 vs Rs.260 expected value per Rs.1,000 deployed.
                """
            )

    # ── Freshness Factor ───────────────────────────────────────────────────────
    with inner[5]:
        _render_freshness_tab(df, strategy_version, dataset_label)
