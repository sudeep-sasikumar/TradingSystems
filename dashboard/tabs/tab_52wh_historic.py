"""
52-Week High — Survivorship-Corrected Historic Backtest Tab

Covers Oct 2019 → present using actual Nifty 500 membership data.
All trades (closed + open) from strategy_version='52wh_v1_survivorship_10y'.
"""
import sys
from datetime import date
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
    load_freshness_df,
)

STRATEGY_VERSION = "52wh_v1_survivorship_10y"
CUR_YEAR = date.today().year


@st.cache_data(ttl=300)
def _load_trades() -> pd.DataFrame:
    engine = get_engine()
    df = pd.read_sql(
        """
        SELECT ticker, company_name, status, entry_date, entry_price,
               highest_price_reached, trailing_stop,
               exit_date, exit_price, return_pct, holding_days, trade_year
        FROM trades
        WHERE strategy_version = :sv AND source = 'backtest'
        ORDER BY entry_date DESC
        """,
        engine,
        params={"sv": STRATEGY_VERSION},
    )
    return df


_FRESH_NUM_CFG = {
    "Win%":     st.column_config.NumberColumn(format="%.1f%%"),
    "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
    "Median%":  st.column_config.NumberColumn(format="%.2f%%"),
}


def _render_nse_freshness_section() -> None:
    """NSE Freshness Factor section at the bottom of the Historic tab."""
    st.markdown("#### Freshness Factor — Time Since Prior 52-Week High (NSE)")
    st.caption(
        "For each trade, measures how many trading days elapsed since the same stock "
        "previously crossed its 252-day high — using only data available before entry (no lookahead).  \n"
        "Run **Setup & Admin → Step 7** to populate.  "
        "Full freshness × regime cross-tab is in the **Nifty 500 — Regime Analysis → Freshness Factor** tab."
    )

    col_reload, _ = st.columns([1, 6])
    with col_reload:
        if st.button("Reload", key="nse_fresh_reload"):
            st.cache_data.clear()
            st.rerun()

    # Load freshness for the survivorship-corrected dataset (more reliable lookback)
    fresh_df = pd.DataFrame()
    load_err = None
    try:
        fresh_df = load_freshness_df(STRATEGY_VERSION)
    except Exception as exc:
        load_err = exc

    if load_err is not None:
        st.error(f"Error loading freshness data: `{load_err}`")
        return

    if fresh_df.empty:
        st.info(
            "No freshness data yet for this dataset.  \n"
            "Go to **Setup & Admin → Step 7 — Tag All Freshness**, then click **Reload** above."
        )
        return

    # Coverage metrics
    cat = fresh_df["freshness_category"].value_counts()
    n_gap   = int(cat.get("gap_computed", 0))
    n_foh   = int(cat.get("first_observed_high", 0))
    n_insuf = int(cat.get("insufficient_history", 0))

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Total Tagged",          f"{len(fresh_df):,}")
    mc2.metric("Gap Computed",          f"{n_gap:,}")
    mc3.metric("First Observed High",   f"{n_foh:,}")
    mc4.metric("Insufficient History",  f"{n_insuf:,}")

    st.caption(
        "Price cache starts 2018-01-01 for this dataset.  "
        "`first_observed_high` = no prior 52-week high found in ~6 years of lookback."
    )

    # Bucket performance table
    bkt_rows = []
    for bkt in FRESHNESS_BUCKET_ORDER:
        grp = fresh_df[fresh_df["freshness_bucket"] == bkt]
        if len(grp) == 0:
            continue
        n    = len(grp)
        wins = (grp["return_pct"] > 0).sum()
        bkt_rows.append({
            "Freshness Bucket": bkt,
            "n":        n,
            "Win%":     round(wins / n * 100, 1),
            "Avg Ret%": round(float(grp["return_pct"].mean()), 2),
            "Median%":  round(float(grp["return_pct"].median()), 2),
            "Avg Days": int(round(grp["holding_days"].mean())),
            "Note":     "* n<30" if n < 30 else "",
        })

    if not bkt_rows:
        st.info("No bucket data yet.")
        return

    st.dataframe(pd.DataFrame(bkt_rows), hide_index=True, use_container_width=False,
                 column_config=_FRESH_NUM_CFG)

    # Bar chart — gap-computed buckets only
    gap_rows = [r for r in bkt_rows
                if r["Freshness Bucket"] not in ("insufficient_history", "first_observed_high")]
    if gap_rows:
        fig = go.Figure(go.Bar(
            x=[r["Freshness Bucket"] for r in gap_rows],
            y=[r["Avg Ret%"] for r in gap_rows],
            marker_color=["#2ecc71" if (r["Avg Ret%"] or 0) >= 0 else "#e74c3c" for r in gap_rows],
            text=[f"{r['Avg Ret%']:+.1f}%" for r in gap_rows],
            textposition="outside",
        ))
        fig.update_layout(
            title="Avg Return by Freshness Bucket (NSE, gap-computed trades)",
            xaxis_title="Freshness bucket",
            yaxis_title="Avg Return %",
            height=280,
            margin=dict(t=50, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)


def render_tab():
    st.subheader("52-Week High — Survivorship-Corrected Historic Backtest")
    st.caption(
        "Oct 2019 – present | Actual Nifty 500 membership gating on entry signals | "
        "Equal-weight, unlimited capital — **not a real portfolio simulation**"
    )

    df = _load_trades()

    if df.empty:
        st.warning(
            "No historic backtest data found. Run:\n\n"
            "```\npython 52WeekHigh/run_historic_backtest.py --checkpoint backtest\n```"
        )
        return

    closed = df[df["status"] == "closed"].copy()
    open_t = df[df["status"] == "open"].copy()

    # ── Summary metrics ────────────────────────────────────────────────────────
    wins = closed[closed["return_pct"] > 0]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Closed Trades", f"{len(closed):,}")
    c2.metric("Open Positions", f"{len(open_t):,}")
    if len(closed):
        c3.metric("Win Rate", f"{len(wins) / len(closed) * 100:.1f}%")
        c4.metric("Avg Return", f"{closed['return_pct'].mean():.1f}%")
        c5.metric("Median Return", f"{closed['return_pct'].median():.1f}%")
        c6.metric("Gross Return Sum", f"{closed['return_pct'].sum():,.0f}%")
    else:
        for col in (c3, c4, c5, c6):
            col.metric("—", "—")

    st.caption(
        f"Coverage: Oct 2019 – {date.today().strftime('%b %Y')} | "
        f"7 reconstitution events ingested | "
        f"Missing: Sep 2020, Mar/Sep 2021, Sep 2023, Sep 2024 (gaps treated as continuous membership)"
    )

    st.divider()

    # ── Year-by-year table ─────────────────────────────────────────────────────
    st.markdown("#### Year-by-Year Performance (closed trades, grouped by entry year)")

    yearly_rows = []
    for yr in sorted(closed["trade_year"].dropna().unique()):
        yr = int(yr)
        sub = closed[closed["trade_year"] == yr]
        w = sub[sub["return_pct"] > 0]
        label = f"{yr} (YTD)" if yr == CUR_YEAR else str(yr)
        yearly_rows.append({
            "Year":          label,
            "Trades":        len(sub),
            "Win %":         round(len(w) / len(sub) * 100, 1) if len(sub) else 0,
            "Avg Ret %":     round(sub["return_pct"].mean(), 1),
            "Median Ret %":  round(sub["return_pct"].median(), 1),
            "Best %":        round(sub["return_pct"].max(), 1),
            "Worst %":       round(sub["return_pct"].min(), 1),
            "Gross Sum %":   round(sub["return_pct"].sum(), 0),
        })

    if yearly_rows:
        st.dataframe(
            pd.DataFrame(yearly_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Win %":        st.column_config.NumberColumn(format="%.1f%%"),
                "Avg Ret %":    st.column_config.NumberColumn(format="%.1f%%"),
                "Median Ret %": st.column_config.NumberColumn(format="%.1f%%"),
                "Best %":       st.column_config.NumberColumn(format="%.1f%%"),
                "Worst %":      st.column_config.NumberColumn(format="%.1f%%"),
                "Gross Sum %":  st.column_config.NumberColumn(format="%.0f%%"),
            },
        )

    # ── Annual gross return chart ──────────────────────────────────────────────
    if yearly_rows:
        bars = pd.DataFrame(yearly_rows)[["Year", "Gross Sum %"]]
        colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in bars["Gross Sum %"]]
        fig = go.Figure(
            go.Bar(
                x=bars["Year"],
                y=bars["Gross Sum %"],
                marker_color=colors,
                text=[f"{v:,.0f}%" for v in bars["Gross Sum %"]],
                textposition="outside",
            )
        )
        fig.update_layout(
            title="Gross Return Sum by Entry Year (illustrative, equal-weight)",
            xaxis_title="Entry Year",
            yaxis_title="Gross Return Sum (%)",
            height=320,
            margin=dict(t=50, b=40),
        )
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ── Trade log ─────────────────────────────────────────────────────────────
    st.markdown("#### Trade Log")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        years = ["All"] + [str(int(y)) for y in sorted(df["trade_year"].dropna().unique())]
        year_sel = st.selectbox("Entry Year", years, key="hist_year")
    with col_b:
        status_sel = st.selectbox("Status", ["All", "Closed", "Open"], key="hist_status")
    with col_c:
        result_sel = st.selectbox("Result", ["All", "Win (>0%)", "Loss (≤0%)"], key="hist_result")

    view = df.copy()
    if year_sel != "All":
        view = view[view["trade_year"] == int(year_sel)]
    if status_sel == "Closed":
        view = view[view["status"] == "closed"]
    elif status_sel == "Open":
        view = view[view["status"] == "open"]
    if result_sel == "Win (>0%)":
        view = view[(view["status"] == "closed") & (view["return_pct"] > 0)]
    elif result_sel == "Loss (≤0%)":
        view = view[(view["status"] == "closed") & (view["return_pct"] <= 0)]

    display = view[[
        "ticker", "company_name", "status",
        "entry_date", "entry_price",
        "highest_price_reached", "trailing_stop",
        "exit_date", "exit_price",
        "return_pct", "holding_days",
    ]].copy()
    display.columns = [
        "Ticker", "Company", "Status",
        "Entry Date", "Entry ₹",
        "Peak ₹", "Stop ₹",
        "Exit Date", "Exit ₹",
        "Return %", "Days",
    ]
    display["Entry Date"] = display["Entry Date"].fillna("")
    display["Exit Date"]  = display["Exit Date"].fillna("")

    st.dataframe(
        display.reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Return %": st.column_config.NumberColumn(format="%.1f%%"),
            "Entry ₹":  st.column_config.NumberColumn(format="₹%.2f"),
            "Peak ₹":   st.column_config.NumberColumn(format="₹%.2f"),
            "Stop ₹":   st.column_config.NumberColumn(format="₹%.2f"),
            "Exit ₹":   st.column_config.NumberColumn(format="₹%.2f"),
        },
    )

    st.caption(
        f"Showing {len(view):,} of {len(df):,} trades  |  "
        "Illustrative, equal-weight, no capital constraints — not a real portfolio simulation"
    )

    st.divider()
    _render_nse_freshness_section()
