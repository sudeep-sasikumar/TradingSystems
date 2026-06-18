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
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

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
