"""
Regime Analysis Tab — Checkpoint 8.

Visualises market + sector regime tags for the survivorship-corrected backtest.
Data source: trade_regime_tags table (written by run_regime_analysis.py --checkpoint tag).
Analysis-only — no changes to trade outcomes or live scanner.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

STRATEGY_VERSION = "52wh_v1_survivorship_10y"
MIN_COUNT        = 30   # cells flagged when below this

QUINTILE_ORDER = [
    "strong_downtrend",
    "moderate_downtrend",
    "flat",
    "moderate_uptrend",
    "strong_uptrend",
]


@st.cache_data(ttl=300)
def _load_tagged_closed() -> pd.DataFrame:
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
                   r.synthetic_dist_200dma_pct, r.synthetic_6m_return_pct
            FROM trades t
            JOIN trade_regime_tags r ON t.id = r.trade_id
            WHERE t.strategy_version = :sv
              AND t.source = 'backtest'
              AND t.status = 'closed'
              AND t.return_pct IS NOT NULL
            ORDER BY t.entry_date
            """,
            engine,
            params={"sv": STRATEGY_VERSION},
        )
        return df
    except Exception:
        return pd.DataFrame()


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
    # Use QUINTILE_ORDER when applicable, otherwise sort alphabetically
    if all(v in QUINTILE_ORDER for v in vals):
        order = [v for v in QUINTILE_ORDER if v in vals]
    else:
        order = sorted(vals)
    for val in order:
        s = _stats(sub[sub[col] == val])
        s["Regime"] = val
        s["Flag"] = "*" if s["n"] < MIN_COUNT else ""
        rows.append(s)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)[["Regime", "n", "Win%", "Avg Ret%", "Median%", "Avg Days", "Flag"]]


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
    return result[[c1, c2, "n", "Win%", "Avg Ret%", "Median%", "Avg Days"]
                  if False else [c for c in [c1, c2, "n", "Win%", "Avg Ret%", "Median%", "Avg Days"]
                                 if c in result.columns]].head(top_n)


def render_tab() -> None:
    st.subheader("Regime Analysis — 52-Week High (Survivorship-Corrected)")
    st.caption(
        "Market and sector regime tags as of each trade's entry date.  "
        "Analysis only — trade outcomes are unchanged."
    )

    df = _load_tagged_closed()

    if df.empty:
        st.warning(
            "No regime-tagged trades found. Run:\n\n"
            "```\npython 52WeekHigh/run_regime_analysis.py --checkpoint tag\n```"
        )
        return

    n = len(df)
    mkt_ok = df["market_vs_200dma"].notna().sum()
    sec_ok = df["official_vs_200dma"].notna().sum()
    syn_ok = df["synthetic_vs_200dma"].notna().sum()

    # ── Coverage row ───────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Closed Trades", f"{n:,}")
    c2.metric("Market Tagged", f"{mkt_ok/n*100:.0f}%")
    c3.metric("Sector Tagged", f"{sec_ok/n*100:.0f}%  (official)")
    c4.metric("Basket Tagged", f"{syn_ok/n*100:.0f}%  (synthetic)")

    market_ticker = df["market_index_used"].dropna().iloc[0] if mkt_ok > 0 else "—"
    st.caption(
        f"Market index: **{market_ticker}** | "
        f"Official sector: NSE sectoral indices (Auto, Bank, IT, Pharma, FMCG, Metal, Realty, "
        f"Media, Energy, PSU Bank) | "
        f"Synthetic: equal-weighted industry baskets from baseline CSV  |  "
        f"\\* = fewer than {MIN_COUNT} trades"
    )

    st.divider()

    # ── Tab layout ─────────────────────────────────────────────────────────────
    inner = st.tabs(["Market Regime", "Sector Regime", "Top Combinations", "Explorer"])

    # ── Market Regime ──────────────────────────────────────────────────────────
    with inner[0]:
        st.markdown("#### Market Regime (200-DMA)")
        res = _breakdown(df, "market_vs_200dma")
        if not res.empty:
            st.dataframe(
                res,
                hide_index=True,
                use_container_width=False,
                column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                               "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                               "Median%": st.column_config.NumberColumn(format="%.2f%%")},
            )

        st.markdown("#### Market 6-Month Trailing Return Quintile")
        res_q = _breakdown(df, "market_6m_quintile")
        if not res_q.empty:
            st.dataframe(
                res_q,
                hide_index=True,
                use_container_width=False,
                column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                               "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                               "Median%": st.column_config.NumberColumn(format="%.2f%%")},
            )

        # Bar chart: avg return by quintile
        if not res_q.empty:
            fig = go.Figure(go.Bar(
                x=res_q["Regime"],
                y=res_q["Avg Ret%"],
                marker_color=["#e74c3c" if v < 0 else "#2ecc71" for v in res_q["Avg Ret%"]],
                text=[f"{v:.1f}%" for v in res_q["Avg Ret%"]],
                textposition="outside",
            ))
            fig.update_layout(
                title="Avg Return by Market Quintile (entry regime)",
                xaxis_title="Market 6M Quintile",
                yaxis_title="Avg Return %",
                height=300,
                margin=dict(t=50, b=10),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── Sector Regime ──────────────────────────────────────────────────────────
    with inner[1]:
        st.markdown("#### Official Sector 200-DMA")
        st.caption(f"Coverage: {sec_ok:,} / {n:,} trades ({sec_ok/n*100:.1f}%)")
        res = _breakdown(df, "official_vs_200dma")
        if not res.empty:
            st.dataframe(res, hide_index=True, use_container_width=False,
                         column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Median%": st.column_config.NumberColumn(format="%.2f%%")})

        st.markdown("#### Official Sector 6M Quintile")
        res_q = _breakdown(df, "official_6m_quintile")
        if not res_q.empty:
            st.dataframe(res_q, hide_index=True, use_container_width=False,
                         column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Median%": st.column_config.NumberColumn(format="%.2f%%")})

        st.markdown("#### Synthetic Basket 200-DMA")
        st.caption(f"Coverage: {syn_ok:,} / {n:,} trades ({syn_ok/n*100:.1f}%)")
        res = _breakdown(df, "synthetic_vs_200dma")
        if not res.empty:
            st.dataframe(res, hide_index=True, use_container_width=False,
                         column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Median%": st.column_config.NumberColumn(format="%.2f%%")})

        st.markdown("#### Synthetic Basket 6M Quintile")
        res_q = _breakdown(df, "synthetic_6m_quintile")
        if not res_q.empty:
            st.dataframe(res_q, hide_index=True, use_container_width=False,
                         column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Median%": st.column_config.NumberColumn(format="%.2f%%")})

    # ── Top Combinations ───────────────────────────────────────────────────────
    with inner[2]:
        valid_cols = {
            "market_vs_200dma":      "Mkt 200-DMA",
            "market_6m_quintile":    "Mkt 6M Q",
            "official_vs_200dma":    "Sec 200-DMA (official)",
            "official_6m_quintile":  "Sec 6M Q (official)",
            "synthetic_vs_200dma":   "Basket 200-DMA (synth)",
            "synthetic_6m_quintile": "Basket 6M Q (synth)",
        }
        active_cols = [c for c in valid_cols if df[c].notna().sum() >= MIN_COUNT]

        st.markdown(f"#### Top 5 Pairs by Average Return  (min {MIN_COUNT} trades)")
        tops = _top_combos(df, active_cols, top_n=5, worst=False)
        if not tops.empty:
            st.dataframe(tops, hide_index=True, use_container_width=True,
                         column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Median%": st.column_config.NumberColumn(format="%.2f%%")})
        else:
            st.info(f"No pair meets the minimum {MIN_COUNT} trade threshold.")

        st.markdown(f"#### Bottom 5 Pairs by Average Return  (min {MIN_COUNT} trades)")
        bots = _top_combos(df, active_cols, top_n=5, worst=True)
        if not bots.empty:
            st.dataframe(bots, hide_index=True, use_container_width=True,
                         column_config={"Win%": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Median%": st.column_config.NumberColumn(format="%.2f%%")})

        st.caption("Equal-weight, unlimited capital, no transaction costs — not a real portfolio simulation.")

    # ── Explorer ───────────────────────────────────────────────────────────────
    with inner[3]:
        st.markdown("#### Filter Trades by Regime")

        col_a, col_b = st.columns(2)
        with col_a:
            mkt_filter = st.selectbox(
                "Market 200-DMA",
                ["All", "above_200dma", "below_200dma"],
                key="reg_mkt_dma",
            )
            mkt_q_filter = st.selectbox(
                "Market 6M Quintile",
                ["All"] + QUINTILE_ORDER,
                key="reg_mkt_q",
            )
        with col_b:
            sec_filter = st.selectbox(
                "Sector (official) 200-DMA",
                ["All", "above_200dma", "below_200dma"],
                key="reg_sec_dma",
            )
            syn_filter = st.selectbox(
                "Basket (synthetic) 6M Quintile",
                ["All"] + QUINTILE_ORDER,
                key="reg_syn_q",
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

        s = _stats(view)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades", f"{s['n']:,}")
        m2.metric("Win Rate", f"{s['Win%']}%" if s["Win%"] is not None else "—")
        m3.metric("Avg Return", f"{s['Avg Ret%']}%" if s["Avg Ret%"] is not None else "—")
        m4.metric("Avg Days", str(s["Avg Days"]) if s["Avg Days"] is not None else "—")
        if s["n"] < MIN_COUNT:
            st.caption(f"\\* Fewer than {MIN_COUNT} trades — interpret with caution.")

        if not view.empty:
            display = view[[
                "ticker", "entry_date", "trade_year",
                "market_vs_200dma", "market_6m_quintile",
                "official_sector", "official_6m_quintile",
                "synthetic_6m_quintile",
                "return_pct", "holding_days",
            ]].copy()
            display.columns = [
                "Ticker", "Entry Date", "Year",
                "Mkt 200-DMA", "Mkt 6M Q",
                "Sector", "Sec 6M Q",
                "Basket 6M Q",
                "Return %", "Days",
            ]
            st.dataframe(
                display.reset_index(drop=True),
                hide_index=True,
                use_container_width=True,
                column_config={"Return %": st.column_config.NumberColumn(format="%.1f%%")},
            )
