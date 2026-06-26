"""
Nifty 500 — 52-Week High Momentum System (Comprehensive Tab)

Uses ONLY the survivorship-corrected 2019-present backtest (52wh_v1_survivorship_10y).
Sub-tabs: Overview | Backtest Trades | Live & Signals | Regime Analysis | Freshness Factor
"""
from __future__ import annotations

import itertools
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
    assign_bucket as freshness_assign_bucket,
    load_freshness_df,
)

SV = "52wh_v1_survivorship_10y"
CUR_YEAR = date.today().year
MIN_COUNT = 30
TRADE_SIZE = 1_000

QUINTILE_ORDER = [
    "strong_downtrend", "moderate_downtrend", "flat",
    "moderate_uptrend", "strong_uptrend",
]
TIER_ORDER = ["HIGH", "STANDARD", "AVOID"]


# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _load_backtest() -> pd.DataFrame:
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
        engine, params={"sv": SV},
    )
    return df


@st.cache_data(ttl=60)
def _load_live() -> tuple[pd.DataFrame, pd.DataFrame]:
    engine = get_engine()
    try:
        live = pd.read_sql(
            """
            SELECT t.*, s.conviction_tier, s.regime_score
            FROM trades t
            LEFT JOIN signals s ON t.signal_id = s.id
            WHERE t.source = 'live'
            ORDER BY t.entry_date DESC
            """,
            engine,
        )
        sigs = pd.read_sql(
            "SELECT * FROM signals WHERE status='pending' ORDER BY scan_timestamp DESC",
            engine,
        )
        return live, sigs
    except Exception:
        return pd.DataFrame(), pd.DataFrame()


@st.cache_data(ttl=60)
def _load_conviction_tally() -> pd.DataFrame:
    engine = get_engine()
    try:
        return pd.read_sql(
            """
            SELECT conviction_tier, status, COUNT(*) AS n
            FROM signals
            WHERE strategy_version = '52wh_v1'
              AND conviction_tier IS NOT NULL
            GROUP BY conviction_tier, status
            ORDER BY conviction_tier, status
            """,
            engine,
        )
    except Exception:
        return pd.DataFrame()


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
                   r.official_sector, r.official_vs_200dma, r.official_6m_quintile,
                   r.official_dist_200dma_pct, r.official_6m_return_pct,
                   r.industry_group, r.synthetic_basket_size,
                   r.synthetic_vs_200dma, r.synthetic_6m_quintile,
                   r.synthetic_dist_200dma_pct, r.synthetic_6m_return_pct,
                   r.freshness_category, r.freshness_gap_td,
                   r.freshness_gap_cal, r.freshness_prior_date
            FROM trades t
            JOIN trade_regime_tags r ON t.id = r.trade_id
            WHERE t.strategy_version = :sv
              AND t.source = 'backtest'
              AND t.status = 'closed'
              AND t.return_pct IS NOT NULL
            ORDER BY t.entry_date
            """,
            engine, params={"sv": SV},
        )
        if df.empty:
            return df
        df = _assign_conviction(df)
        if "freshness_category" in df.columns:
            df["freshness_bucket"] = df.apply(
                lambda r: freshness_assign_bucket(
                    r["freshness_category"] or "insufficient_history",
                    r.get("freshness_gap_td"),
                ) if pd.notna(r.get("freshness_category")) else None,
                axis=1,
            )
        return df
    except Exception:
        return pd.DataFrame()


# ── Computation helpers ────────────────────────────────────────────────────────

def _assign_conviction(df: pd.DataFrame) -> pd.DataFrame:
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

    is_avoid   = df["market_6m_quintile"] == "strong_uptrend"
    is_bottom2 = df["market_6m_quintile"].isin(["strong_downtrend", "moderate_downtrend"])
    is_bsk_abv = df["synthetic_vs_200dma"] == "above_200dma"

    tier = pd.Series("STANDARD", index=df.index)
    tier[is_bottom2 & is_bsk_abv] = "HIGH"
    tier[is_avoid] = "AVOID"
    df["conviction_tier"] = tier
    return df


def _stats(df: pd.DataFrame) -> dict:
    n = len(df)
    empty = {"n": 0, "wins": 0, "Win%": None, "Avg Ret%": None,
             "Median%": None, "best": None, "worst": None, "gross": 0, "Avg Days": None}
    if n == 0 or "return_pct" not in df.columns:
        return empty
    d = df[df["return_pct"].notna()]
    if d.empty:
        return empty
    wins = (d["return_pct"] > 0).sum()
    return {
        "n":        len(d),
        "wins":     int(wins),
        "Win%":     round(wins / len(d) * 100, 1),
        "Avg Ret%": round(float(d["return_pct"].mean()), 2),
        "Median%":  round(float(d["return_pct"].median()), 2),
        "best":     round(float(d["return_pct"].max()), 2),
        "worst":    round(float(d["return_pct"].min()), 2),
        "gross":    round(float(d["return_pct"].sum()), 0),
        "Avg Days": int(round(d["holding_days"].mean())) if "holding_days" in d.columns else None,
    }


def _year_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for yr in sorted(df["trade_year"].dropna().unique()):
        yr = int(yr)
        yd = df[df["trade_year"] == yr]
        w  = yd[yd["return_pct"] > 0]
        label = f"{yr} (YTD)" if yr == CUR_YEAR else str(yr)
        rows.append({
            "Year":     label,
            "Trades":   len(yd),
            "Win %":    round(len(w) / len(yd) * 100, 1) if len(yd) else 0,
            "Avg %":    round(yd["return_pct"].mean(),   2),
            "Median %": round(yd["return_pct"].median(), 2),
            "Best %":   round(yd["return_pct"].max(),    2),
            "Worst %":  round(yd["return_pct"].min(),    2),
            "Gross %":  round(yd["return_pct"].sum(),    0),
            "Avg Days": round(yd["holding_days"].mean(), 1),
        })
    return pd.DataFrame(rows)


def _equity_figure(df: pd.DataFrame) -> go.Figure:
    d = df[df["return_pct"].notna()].copy()
    d["exit_dt"] = pd.to_datetime(d["exit_date"])
    d["month"]   = d["exit_dt"].dt.to_period("M").dt.strftime("%Y-%m")
    m = d.groupby("month")["return_pct"].sum().reset_index()
    m.columns = ["month", "gross"]
    m["cum"] = m["gross"].cumsum()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=m["month"], y=m["gross"],
        name="Monthly gross (%pts)",
        marker_color=["#26a69a" if v >= 0 else "#ef5350" for v in m["gross"]],
        opacity=0.75,
        hovertemplate="%{x}<br>Monthly: %{y:+.1f}%pts<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=m["month"], y=m["cum"],
        name="Cumulative (%pts)",
        line=dict(color="#1565c0", width=2.5),
        hovertemplate="%{x}<br>Cumulative: %{y:+,.0f}%pts<extra></extra>",
        yaxis="y2",
    ))
    fig.update_layout(
        title=dict(
            text=(
                "Equity Curve (by exit date) — "
                "<i>Illustrative, equal-weight, no capital constraints — "
                "not a real portfolio simulation</i>"
            ),
            font_size=13,
        ),
        xaxis=dict(title="", tickangle=-45, tickfont_size=10),
        yaxis=dict(title="Monthly gross (%pts)", side="left"),
        yaxis2=dict(title="Cumulative (%pts)", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.10, x=0),
        hovermode="x unified",
        height=420,
        margin=dict(l=60, r=80, t=90, b=80),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def _breakdown(df: pd.DataFrame, col: str) -> pd.DataFrame:
    sub = df.dropna(subset=[col])
    vals = sub[col].unique()
    order = [v for v in QUINTILE_ORDER if v in vals] if all(v in QUINTILE_ORDER for v in vals) else sorted(vals)
    rows = []
    for val in order:
        s = _stats(sub[sub[col] == val])
        rows.append({
            "Regime":   val,
            "n":        s["n"],
            "Win%":     s["Win%"],
            "Avg Ret%": s["Avg Ret%"],
            "Median%":  s["Median%"],
            "Avg Days": s["Avg Days"],
            "!":        "*" if s["n"] < MIN_COUNT else "",
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _top_combos(df: pd.DataFrame, cols: list, top_n: int = 5, worst: bool = False) -> pd.DataFrame:
    c1 = c2 = None
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
    if not rows or c1 is None:
        return pd.DataFrame()
    result = pd.DataFrame(rows).sort_values("Avg Ret%", ascending=worst)
    all_cols = list(dict.fromkeys([c1, c2, "n", "Win%", "Avg Ret%", "Median%", "Avg Days"]))
    return result[[c for c in all_cols if c in result.columns]].head(top_n)


def _sim_stats(grp: pd.DataFrame) -> dict:
    n = len(grp)
    if n == 0:
        return dict(n=0, deployed=0, pnl=0.0, final=0.0, ret=0.0, win_pct=0.0)
    deployed = n * TRADE_SIZE
    pnl      = float((grp["return_pct"] / 100 * TRADE_SIZE).sum())
    wins     = int((grp["return_pct"] > 0).sum())
    return dict(n=n, deployed=deployed, pnl=pnl, final=deployed + pnl,
                ret=pnl / deployed * 100, win_pct=wins / n * 100)


def _inr(v: float) -> str:
    sign  = "-" if v < 0 else ""
    abs_v = abs(v)
    return f"{sign}Rs.{abs_v / 1_00_000:.2f}L" if abs_v >= 1_00_000 else f"{sign}Rs.{abs_v:,.0f}"


_NUM_CFG = {
    "Win%":     st.column_config.NumberColumn(format="%.1f%%"),
    "Avg Ret%": st.column_config.NumberColumn(format="%.2f%%"),
    "Median%":  st.column_config.NumberColumn(format="%.2f%%"),
}

_YEAR_COL_CFG = {
    "Win %":    st.column_config.NumberColumn(format="%.1f%%"),
    "Avg %":    st.column_config.NumberColumn(format="%+.2f%%"),
    "Median %": st.column_config.NumberColumn(format="%+.2f%%"),
    "Best %":   st.column_config.NumberColumn(format="%+.2f%%"),
    "Worst %":  st.column_config.NumberColumn(format="%+.2f%%"),
    "Gross %":  st.column_config.NumberColumn(format="%+,.0f%%"),
    "Avg Days": st.column_config.NumberColumn(format="%.1f"),
}


# ── Sub-tab renderers ──────────────────────────────────────────────────────────

def _render_overview(closed: pd.DataFrame) -> None:
    s = _stats(closed)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Closed Trades", f"{s['n']:,}")
    c2.metric("Win Rate",      f"{s['Win%']:.1f}%" if s["Win%"] is not None else "—")
    c3.metric("Avg Return",    f"{s['Avg Ret%']:+.2f}%" if s["Avg Ret%"] is not None else "—")
    c4.metric("Median Return", f"{s['Median%']:+.2f}%" if s["Median%"] is not None else "—")
    c5.metric("Best Trade",    f"{s['best']:+.2f}%" if s["best"] is not None else "—")
    c6.metric("Gross (%pts)",  f"{s['gross']:+,.0f}" if s["gross"] else "—")

    st.caption(
        "Oct 2019 – present | Survivorship-corrected (actual Nifty 500 membership at entry) | "
        "7 reconstitution events ingested | "
        "**Illustrative, equal-weight, no capital constraints — not a real portfolio simulation**"
    )

    st.divider()
    st.markdown("#### Year-by-Year Performance")

    yt = _year_table(closed)
    if not yt.empty:
        col_tbl, col_bar = st.columns([2, 3])
        with col_tbl:
            st.dataframe(yt, hide_index=True, use_container_width=True, column_config=_YEAR_COL_CFG)
        with col_bar:
            fig = go.Figure(go.Bar(
                x=yt["Year"], y=yt["Gross %"],
                marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in yt["Gross %"]],
                text=[f"{v:+,.0f}%" for v in yt["Gross %"]],
                textposition="outside",
            ))
            fig.update_layout(
                title="Annual Gross Return Sum (equal-weight, illustrative)",
                xaxis_title="Entry Year", yaxis_title="Gross %pts",
                height=360, margin=dict(t=50, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.markdown("#### Equity Curve")
    st.plotly_chart(_equity_figure(closed), use_container_width=True)


def _render_trades(df: pd.DataFrame) -> None:
    closed = df[df["status"] == "closed"].copy()
    open_t = df[df["status"] == "open"].copy()

    st.markdown(f"#### Closed Trades ({len(closed):,})")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        years    = ["All"] + [str(int(y)) for y in sorted(df["trade_year"].dropna().unique())]
        year_sel = st.selectbox("Entry Year", years, key="nifty_trd_year")
    with col_b:
        result_sel = st.selectbox(
            "Result", ["All", "Winners (>0%)", "Losers (≤0%)"], key="nifty_trd_result"
        )
    with col_c:
        sort_sel = st.selectbox(
            "Sort by",
            ["return_pct", "entry_date", "holding_days", "ticker"],
            format_func=lambda x: {
                "return_pct":   "Return %",
                "entry_date":   "Entry date",
                "holding_days": "Days held",
                "ticker":       "Ticker",
            }[x],
            key="nifty_trd_sort",
        )

    view = closed.copy()
    if year_sel != "All":
        view = view[view["trade_year"] == int(year_sel)]
    if result_sel == "Winners (>0%)":
        view = view[view["return_pct"] > 0]
    elif result_sel == "Losers (≤0%)":
        view = view[view["return_pct"] <= 0]
    view = view.sort_values(sort_sel, ascending=(sort_sel != "return_pct"))

    disp = view[[
        "ticker", "company_name", "entry_date", "exit_date",
        "entry_price", "exit_price", "return_pct", "holding_days",
    ]].copy()
    disp.columns = ["Ticker", "Company", "Entry Date", "Exit Date", "Entry ₹", "Exit ₹", "Return %", "Days"]

    st.caption(f"Showing {len(view):,} of {len(closed):,} closed trades")
    st.dataframe(
        disp.reset_index(drop=True),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Return %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Entry ₹":  st.column_config.NumberColumn(format="₹%.2f"),
            "Exit ₹":   st.column_config.NumberColumn(format="₹%.2f"),
        },
    )

    if not open_t.empty:
        st.divider()
        st.markdown(f"#### Open at Backtest End — {len(open_t)} positions")
        st.caption(
            "Trades not yet stopped out as of the last backtest date. "
            "Unrealized returns excluded from all performance statistics."
        )
        open_disp = open_t[[
            "ticker", "company_name", "entry_date",
            "entry_price", "highest_price_reached", "trailing_stop",
        ]].sort_values("entry_date").reset_index(drop=True)
        st.dataframe(
            open_disp,
            hide_index=True,
            use_container_width=True,
            column_config={
                "ticker":                st.column_config.TextColumn("Ticker"),
                "company_name":          st.column_config.TextColumn("Company"),
                "entry_date":            st.column_config.TextColumn("Entry Date"),
                "entry_price":           st.column_config.NumberColumn("Entry ₹",   format="₹%.2f"),
                "highest_price_reached": st.column_config.NumberColumn("Peak ₹",    format="₹%.2f"),
                "trailing_stop":         st.column_config.NumberColumn("Stop ₹",    format="₹%.2f"),
            },
        )


def _render_live(live_df: pd.DataFrame, pending_df: pd.DataFrame, tally_df: pd.DataFrame) -> None:
    _tier_labels = {"HIGH": "HIGH CONVICTION", "STANDARD": "Standard", "AVOID": "AVOID ⚠"}

    st.markdown("#### Open Live Positions")
    open_live = live_df[live_df["status"] == "open"].copy() if not live_df.empty else pd.DataFrame()
    if not open_live.empty:
        if "conviction_tier" in open_live.columns:
            open_live["conviction_tier"] = open_live["conviction_tier"].map(
                lambda x: _tier_labels.get(x, x or "—")
            )
        cols = [
            "ticker", "company_name", "entry_date", "entry_price",
            "highest_price_reached", "trailing_stop", "conviction_tier", "regime_score",
        ]
        st.dataframe(
            open_live[[c for c in cols if c in open_live.columns]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "ticker":                st.column_config.TextColumn("Ticker"),
                "company_name":          st.column_config.TextColumn("Company"),
                "entry_date":            st.column_config.DateColumn("Entry", format="YYYY-MM-DD"),
                "entry_price":           st.column_config.NumberColumn("Entry ₹",    format="%.2f"),
                "highest_price_reached": st.column_config.NumberColumn("Peak ₹",     format="%.2f"),
                "trailing_stop":         st.column_config.NumberColumn("Stop ₹",     format="%.2f"),
                "conviction_tier":       st.column_config.TextColumn("Conviction",   width=160),
                "regime_score":          st.column_config.NumberColumn("Score",       format="%d"),
            },
        )
    else:
        st.caption("No open live positions.")

    st.divider()
    st.markdown("#### Pending Signals (awaiting Accept / Reject)")
    if not pending_df.empty:
        sig_cols = [
            "ticker", "company_name", "signal_date", "signal_price",
            "benchmark_252d", "signal_type", "conviction_tier", "regime_score", "scan_timestamp",
        ]
        sig_disp = pending_df[[c for c in sig_cols if c in pending_df.columns]].copy()
        if "conviction_tier" in sig_disp.columns:
            sig_disp["conviction_tier"] = sig_disp["conviction_tier"].map(
                lambda x: _tier_labels.get(x, x or "—")
            )
        st.dataframe(
            sig_disp,
            hide_index=True,
            use_container_width=True,
            column_config={
                "ticker":          st.column_config.TextColumn("Ticker"),
                "company_name":    st.column_config.TextColumn("Company"),
                "signal_date":     st.column_config.TextColumn("Date"),
                "signal_price":    st.column_config.NumberColumn("Signal ₹",  format="%.2f"),
                "benchmark_252d":  st.column_config.NumberColumn("252d High", format="%.2f"),
                "signal_type":     st.column_config.TextColumn("Type"),
                "conviction_tier": st.column_config.TextColumn("Conviction",  width=160),
                "regime_score":    st.column_config.NumberColumn("Score",      format="%d"),
                "scan_timestamp":  st.column_config.TextColumn("Scanned"),
            },
        )
    else:
        st.caption("No pending signals.")

    st.divider()
    st.markdown("#### Conviction Tier — Running Tally")
    st.caption("Your historical accept/reject pattern per tier since conviction tracking began.")

    if tally_df.empty:
        st.info("No conviction-tagged signals yet.")
    else:
        pivot = tally_df.pivot_table(
            index="conviction_tier", columns="status", values="n", aggfunc="sum", fill_value=0
        ).reset_index()
        pivot.columns.name = None
        for col in ["pending", "accepted", "rejected", "expired"]:
            if col not in pivot.columns:
                pivot[col] = 0
        pivot["Total"]   = pivot[["pending", "accepted", "rejected", "expired"]].sum(axis=1)
        pivot["Accept%"] = (pivot["accepted"] / pivot["Total"].replace(0, 1) * 100).round(1)
        tier_ord = {"HIGH": 0, "STANDARD": 1, "AVOID": 2}
        pivot["_o"] = pivot["conviction_tier"].map(lambda x: tier_ord.get(x, 9))
        pivot = pivot.sort_values("_o").drop(columns=["_o"])
        pivot = pivot.rename(columns={
            "conviction_tier": "Tier", "pending": "Pending",
            "accepted": "Accepted", "rejected": "Rejected", "expired": "Expired",
        })
        disp_cols = ["Tier", "Total", "Pending", "Accepted", "Rejected", "Expired", "Accept%"]
        st.dataframe(
            pivot[[c for c in disp_cols if c in pivot.columns]],
            hide_index=True,
            use_container_width=False,
            column_config={"Accept%": st.column_config.NumberColumn("Accept%", format="%.1f%%")},
        )

    st.divider()
    st.markdown("#### Recent Live Activity")
    if not live_df.empty:
        recent = live_df.sort_values("entry_date", ascending=False).head(20)
        st.dataframe(recent, hide_index=True, use_container_width=True)
    else:
        st.caption("No live trade activity yet.")


def _render_regime(df: pd.DataFrame) -> None:
    if df.empty:
        st.warning(
            f"No regime-tagged trades found for **{SV}**.  \n\n"
            "Run: `python 52WeekHigh/run_regime_analysis.py --checkpoint tag "
            f"--strategy-version {SV}`  \n\n"
            "Or use **Setup & Admin → Step 3**."
        )
        return

    n      = len(df)
    mkt_ok = df["market_vs_200dma"].notna().sum()
    sec_ok = df["official_vs_200dma"].notna().sum()
    syn_ok = df["synthetic_vs_200dma"].notna().sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Closed Trades",    f"{n:,}")
    c2.metric("Market Tagged",    "100%")
    c3.metric("Official Sector",  f"{sec_ok/n*100:.0f}%",
              help="Only industries with a matching NSE sectoral index are tagged")
    c4.metric("Synthetic Basket", f"{syn_ok/n*100:.0f}%",
              help="Equal-weighted industry baskets built from ≥10 stocks")

    market_ticker = df["market_index_used"].dropna().iloc[0] if mkt_ok > 0 else "—"
    st.caption(
        f"Market index: **{market_ticker}** | "
        "Official sector: Nifty Auto, Bank, IT, Pharma, FMCG, Metal, Realty, Energy, etc. | "
        f"\\* = fewer than {MIN_COUNT} trades"
    )
    st.divider()

    inner = st.tabs([
        "Market Regime", "Sector Regime", "Top Combinations", "Explorer", "Rs.1,000 Simulation"
    ])

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
                x=res_q["Regime"], y=res_q["Avg Ret%"],
                marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in res_q["Avg Ret%"]],
                text=[f"{v:.1f}%" for v in res_q["Avg Ret%"]], textposition="outside",
            ))
            fig.update_layout(
                title="Avg Return by Market 6M Quintile (entry-date regime)",
                xaxis_title="Market Quintile", yaxis_title="Avg Return %",
                height=300, margin=dict(t=50, b=10),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── Sector Regime ──────────────────────────────────────────────────────────
    with inner[1]:
        col_off, col_syn = st.columns(2)
        with col_off:
            st.markdown("#### Official Sector 200-DMA")
            st.caption(f"{sec_ok:,}/{n:,} — PHARMA, AUTO, IT, METALS, OIL & GAS, POWER, MEDIA")
            res = _breakdown(df, "official_vs_200dma")
            if not res.empty:
                st.dataframe(res, hide_index=True, use_container_width=True, column_config=_NUM_CFG)
            st.markdown("#### Official Sector 6M Quintile")
            res_q = _breakdown(df, "official_6m_quintile")
            if not res_q.empty:
                st.dataframe(res_q, hide_index=True, use_container_width=True, column_config=_NUM_CFG)
        with col_syn:
            st.markdown("#### Synthetic Basket 200-DMA")
            st.caption(f"{syn_ok:,}/{n:,} — all industries with ≥10 stocks")
            res = _breakdown(df, "synthetic_vs_200dma")
            if not res.empty:
                st.dataframe(res, hide_index=True, use_container_width=True, column_config=_NUM_CFG)
            st.markdown("#### Synthetic Basket 6M Quintile")
            res_q = _breakdown(df, "synthetic_6m_quintile")
            if not res_q.empty:
                st.dataframe(res_q, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

    # ── Top Combinations ───────────────────────────────────────────────────────
    with inner[2]:
        active_cols = [c for c in [
            "market_vs_200dma", "market_6m_quintile",
            "official_vs_200dma", "official_6m_quintile",
            "synthetic_vs_200dma", "synthetic_6m_quintile",
        ] if df[c].notna().sum() >= MIN_COUNT]

        st.markdown(f"#### Best 5 Pairs by Avg Return (min {MIN_COUNT} trades per cell)")
        tops = _top_combos(df, active_cols, top_n=5, worst=False)
        if not tops.empty:
            st.dataframe(tops, hide_index=True, use_container_width=True, column_config=_NUM_CFG)
        else:
            st.info(f"No pair meets the {MIN_COUNT}-trade minimum.")

        st.markdown(f"#### Worst 5 Pairs by Avg Return (min {MIN_COUNT} trades per cell)")
        bots = _top_combos(df, active_cols, top_n=5, worst=True)
        if not bots.empty:
            st.dataframe(bots, hide_index=True, use_container_width=True, column_config=_NUM_CFG)

    # ── Explorer ───────────────────────────────────────────────────────────────
    with inner[3]:
        st.markdown("#### Filter Trades by Regime")
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            mkt_filter   = st.selectbox("Market 200-DMA", ["All", "above_200dma", "below_200dma"], key="nifty_reg_mkt_dma")
            mkt_q_filter = st.selectbox("Market 6M Quintile", ["All"] + QUINTILE_ORDER, key="nifty_reg_mkt_q")
        with col_b:
            sec_filter = st.selectbox("Official Sector 200-DMA", ["All", "above_200dma", "below_200dma"], key="nifty_reg_sec_dma")
            syn_filter = st.selectbox("Synthetic Basket 6M Quintile", ["All"] + QUINTILE_ORDER, key="nifty_reg_syn_q")
        with col_c:
            tier_filter = st.selectbox(
                "Conviction Tier", ["All"] + TIER_ORDER, key="nifty_reg_tier",
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
        m1.metric("Trades",   f"{s['n']:,}")
        m2.metric("Win Rate", f"{s['Win%']:.1f}%" if s["Win%"] is not None else "—")
        m3.metric("Avg Ret",  f"{s['Avg Ret%']:+.2f}%" if s["Avg Ret%"] is not None else "—")
        m4.metric("Avg Days", str(s["Avg Days"]) if s["Avg Days"] is not None else "—")
        if s["n"] > 0:
            sim = _sim_stats(view)
            m5.metric("Sim P&L (Rs.1k/trade)", _inr(sim["pnl"]))

        if not view.empty:
            src_cols = [
                "ticker", "entry_date", "trade_year", "conviction_tier", "regime_score",
                "market_vs_200dma", "market_6m_quintile",
                "official_sector", "official_vs_200dma",
                "industry_group", "synthetic_6m_quintile",
                "return_pct", "holding_days",
            ]
            lbl_cols = [
                "Ticker", "Entry Date", "Year", "Conviction", "Score",
                "Mkt 200-DMA", "Mkt 6M Q",
                "Sector", "Sec 200-DMA",
                "Industry", "Basket 6M Q",
                "Return %", "Days",
            ]
            present = [(s, l) for s, l in zip(src_cols, lbl_cols) if s in view.columns]
            display = view[[x for x, _ in present]].copy()
            display.columns = [l for _, l in present]
            st.dataframe(
                display.reset_index(drop=True),
                hide_index=True, use_container_width=True,
                column_config={
                    "Return %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Score":    st.column_config.NumberColumn(format="%d"),
                },
            )

    # ── Rs.1,000 Simulation ────────────────────────────────────────────────────
    with inner[4]:
        st.markdown(
            "#### Rs.1,000/Trade Flat Simulation — Illustrative Only\n\n"
            "> Equal Rs.1,000 per trade · no compounding · no transaction costs · unlimited capital"
        )

        s_all = _sim_stats(df)
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Trades",       f"{s_all['n']:,}")
        c2.metric("Deployed",     _inr(s_all["deployed"]))
        c3.metric("Total P&L",    _inr(s_all["pnl"]))
        c4.metric("Final Value",  _inr(s_all["final"]))
        c5.metric("Return %",     f"{s_all['ret']:.1f}%")
        c6.metric("Win Rate",     f"{s_all['win_pct']:.1f}%")

        st.divider()
        st.markdown("##### Conviction Tier Breakdown")
        if "conviction_tier" in df.columns:
            tier_rows = []
            for tier in TIER_ORDER:
                g = df[df["conviction_tier"] == tier]
                s = _sim_stats(g)
                tier_rows.append({
                    "Tier": tier, "Trades": s["n"],
                    "Deployed": _inr(s["deployed"]), "P&L": _inr(s["pnl"]),
                    "Ret%": round(s["ret"], 1), "Win%": round(s["win_pct"], 1),
                    "Note": "directional only" if s["n"] < MIN_COUNT else "",
                })
            st.dataframe(
                pd.DataFrame(tier_rows), hide_index=True, use_container_width=False,
                column_config={
                    "Ret%": st.column_config.NumberColumn("Ret%", format="%.1f%%"),
                    "Win%": st.column_config.NumberColumn("Win%", format="%.1f%%"),
                },
            )

        st.divider()
        st.markdown("##### Market 6M Quintile Breakdown")
        tagged_q = df.dropna(subset=["market_6m_quintile"])
        q_rows = []
        for q in QUINTILE_ORDER:
            g = tagged_q[tagged_q["market_6m_quintile"] == q]
            s = _sim_stats(g)
            q_rows.append({
                "Quintile": q, "Trades": s["n"],
                "Deployed": _inr(s["deployed"]), "P&L": _inr(s["pnl"]),
                "Ret%": round(s["ret"], 1), "Win%": round(s["win_pct"], 1),
                "Note": "directional only" if s["n"] < MIN_COUNT else "",
            })
        st.dataframe(
            pd.DataFrame(q_rows), hide_index=True, use_container_width=False,
            column_config={
                "Ret%": st.column_config.NumberColumn("Ret%", format="%.1f%%"),
                "Win%": st.column_config.NumberColumn("Win%", format="%.1f%%"),
            },
        )

        st.divider()
        st.markdown("##### Year-by-Year")
        yr_rows = []
        cumulative = 0.0
        for yr in sorted(df["trade_year"].dropna().unique()):
            g = df[df["trade_year"] == yr]
            s = _sim_stats(g)
            cumulative += s["pnl"]
            yr_rows.append({
                "Year":     int(yr),
                "Trades":   s["n"],
                "Deployed": _inr(s["deployed"]),
                "P&L":      _inr(s["pnl"]),
                "Ret%":     round(s["ret"], 1),
                "Win%":     round(s["win_pct"], 1),
                "Cum P&L":  _inr(cumulative),
                "Note":     "directional only" if s["n"] < MIN_COUNT else "",
            })
        st.dataframe(
            pd.DataFrame(yr_rows), hide_index=True, use_container_width=False,
            column_config={
                "Ret%": st.column_config.NumberColumn("Ret%", format="%.1f%%"),
                "Win%": st.column_config.NumberColumn("Win%", format="%.1f%%"),
            },
        )


def _render_freshness(tagged_df: pd.DataFrame) -> None:
    st.markdown("#### Freshness Factor — Time Since Prior 52-Week High")
    st.caption(
        "For each trade, measures the gap in trading days between this entry signal and "
        "the previous time this stock crossed its 252-day high, using only data strictly "
        "before entry (no lookahead). Requires regime tagging + Step 7 freshness tag."
    )

    col_reload, _ = st.columns([1, 6])
    with col_reload:
        if st.button("Reload", key="nifty_fresh_reload"):
            st.cache_data.clear()
            st.rerun()

    # Try regime-embedded freshness first; fall back to direct loader
    fresh_df = pd.DataFrame()
    if "freshness_bucket" in tagged_df.columns and tagged_df["freshness_bucket"].notna().any():
        fresh_df = tagged_df[tagged_df["freshness_bucket"].notna()].copy()
    else:
        err = None
        try:
            fresh_df = load_freshness_df(SV)
        except Exception as exc:
            err = exc
        if err is not None:
            st.error(f"Error loading freshness data: `{err}`")
            return
        if fresh_df.empty:
            st.warning(
                "No freshness data found.  \n\n"
                "Run **Setup & Admin → Step 7 — Tag All Freshness**, then click **Reload** above."
            )
            return

    # Coverage
    cat_counts = fresh_df["freshness_category"].value_counts() if "freshness_category" in fresh_df.columns else {}
    n_gap   = int(cat_counts.get("gap_computed", 0))
    n_foh   = int(cat_counts.get("first_observed_high", 0))
    n_insuf = int(cat_counts.get("insufficient_history", 0))

    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric("Total Tagged",         f"{len(fresh_df):,}")
    fc2.metric("Gap Computed",         f"{n_gap:,}")
    fc3.metric("First Observed High",  f"{n_foh:,}")
    fc4.metric("Insufficient History", f"{n_insuf:,}")
    st.caption("Price cache starts 2018-01-01 for this dataset (6 years of lookback).")

    st.divider()

    # Gap distribution
    if "freshness_category" in fresh_df.columns:
        gap_series = fresh_df[fresh_df["freshness_category"] == "gap_computed"]["freshness_gap_td"].dropna()
    else:
        gap_series = pd.Series(dtype=float)

    if len(gap_series) > 0:
        st.markdown(f"#### Gap Distribution (n={len(gap_series):,} gap-computed trades)")
        pcts = [0, 5, 10, 25, 50, 75, 90, 95, 100]
        vals = [float(gap_series.quantile(p / 100)) for p in pcts]
        pct_rows = [
            {"Percentile": f"P{p}", "Gap (td)": int(round(v)),
             "≈ Calendar": f"{round(int(round(v)) * 365 / 252)}d"}
            for p, v in zip(pcts, vals)
        ]
        col_dist, col_hist = st.columns([1, 2])
        with col_dist:
            st.dataframe(pd.DataFrame(pct_rows), hide_index=True, use_container_width=True)
        with col_hist:
            hist_vals = gap_series.clip(upper=gap_series.quantile(0.98))
            fig = go.Figure(go.Histogram(
                x=hist_vals, nbinsx=40,
                marker_color="#4a9edd",
                marker_line=dict(width=0.5, color="rgba(0,0,0,0.3)"),
            ))
            for x, lbl in [(5, "1wk"), (22, "1m"), (130, "6m"), (252, "1yr"), (756, "3yr")]:
                fig.add_vline(x=x, line_dash="dot", line_color="#aaa", annotation_text=lbl)
            fig.update_layout(
                title="Gap Distribution (clipped at P98)",
                xaxis_title="Trading days since prior 52wh signal",
                yaxis_title="Trades",
                height=260, margin=dict(t=40, b=30, l=40, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)
        st.divider()

    # Bucket performance
    st.markdown("#### Performance by Freshness Bucket")
    st.caption(
        "Buckets (trading days): < 1 wk = 1–4 · 1w–1m = 5–21 · 1–6m = 22–129 · "
        "6–12m = 130–251 · 1–3yr = 252–755 · 3yr+ = 756+.  * = fewer than 30 trades."
    )

    bkt_rows = []
    for bkt in FRESHNESS_BUCKET_ORDER:
        grp = fresh_df[fresh_df["freshness_bucket"] == bkt]
        if len(grp) == 0:
            continue
        s = _stats(grp)
        bkt_rows.append({
            "Freshness Bucket": bkt, "n": s["n"],
            "Win%": s["Win%"], "Avg Ret%": s["Avg Ret%"],
            "Median%": s["Median%"], "Avg Days": s["Avg Days"],
            "Note": "* n<30" if s["n"] < MIN_COUNT else "",
        })

    if bkt_rows:
        st.dataframe(pd.DataFrame(bkt_rows), hide_index=True, use_container_width=False,
                     column_config=_NUM_CFG)
        gap_rows = [r for r in bkt_rows
                    if r["Freshness Bucket"] not in ("insufficient_history", "first_observed_high")]
        if gap_rows:
            fig2 = go.Figure(go.Bar(
                x=[r["Freshness Bucket"] for r in gap_rows],
                y=[r["Avg Ret%"] for r in gap_rows],
                marker_color=["#2ecc71" if (r["Avg Ret%"] or 0) >= 0 else "#e74c3c" for r in gap_rows],
                text=[f"{r['Avg Ret%']:+.1f}%" for r in gap_rows],
                textposition="outside",
            ))
            fig2.update_layout(
                title="Avg Return by Freshness Bucket (gap-computed)",
                xaxis_title="Freshness bucket", yaxis_title="Avg Return %",
                height=300, margin=dict(t=50, b=10),
            )
            st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # first_observed_high vs rest
    st.markdown("#### First Observed High vs All Other Trades")
    foh_grp  = fresh_df[fresh_df["freshness_bucket"] == "first_observed_high"]
    rest_grp = fresh_df[fresh_df["freshness_bucket"] != "first_observed_high"]
    comp_rows = []
    for lbl, grp in [("first_observed_high", foh_grp),
                     ("all other (gap known + insufficient)", rest_grp)]:
        s = _stats(grp)
        comp_rows.append({
            "Group": lbl, "n": s["n"],
            "Win%": s["Win%"], "Avg Ret%": s["Avg Ret%"],
            "Median%": s["Median%"], "Avg Days": s["Avg Days"],
            "Note": "* n<30" if s["n"] < MIN_COUNT else "",
        })
    if comp_rows:
        st.dataframe(pd.DataFrame(comp_rows), hide_index=True, use_container_width=False,
                     column_config=_NUM_CFG)

    # Freshness × market regime cross-tab (if regime data present)
    if "market_vs_200dma" in fresh_df.columns and fresh_df["market_vs_200dma"].notna().any():
        st.divider()
        st.markdown("#### Freshness × Market Regime Cross-Tab")
        if "freshness_category" in fresh_df.columns:
            gap_only = fresh_df[fresh_df["freshness_category"] == "gap_computed"]
        else:
            gap_only = pd.DataFrame()

        for regime_val, regime_label in [
            ("below_200dma", "Market BELOW 200-DMA"),
            ("above_200dma", "Market ABOVE 200-DMA"),
        ]:
            regime_grp = gap_only[gap_only["market_vs_200dma"] == regime_val]
            if len(regime_grp) < 5:
                continue
            bl = _stats(regime_grp)
            st.markdown(
                f"**{regime_label}** — {bl['n']} trades · "
                f"baseline avg {bl['Avg Ret%']:+.2f}% · win {bl['Win%']:.1f}%"
            )
            xt_rows = []
            for bkt in FRESHNESS_BUCKET_ORDER:
                if bkt in ("insufficient_history", "first_observed_high"):
                    continue
                grp = regime_grp[regime_grp["freshness_bucket"] == bkt]
                s = _stats(grp)
                if s["n"] < 5:
                    continue
                delta = round((s["Avg Ret%"] or 0) - (bl["Avg Ret%"] or 0), 2)
                xt_rows.append({
                    "Freshness Bucket": bkt, "n": s["n"],
                    "Win%": s["Win%"], "Avg Ret%": s["Avg Ret%"],
                    "vs baseline": f"{delta:+.2f}%",
                    "Note": "* n<30" if s["n"] < MIN_COUNT else "",
                })
            if xt_rows:
                st.dataframe(pd.DataFrame(xt_rows), hide_index=True, use_container_width=False,
                             column_config=_NUM_CFG)


# ── Main render ────────────────────────────────────────────────────────────────

def render_tab() -> None:
    st.header("Nifty 500 — 52-Week High Momentum System")
    st.caption(
        "Survivorship-corrected backtest · Oct 2019 – present · "
        "Actual Nifty 500 membership at entry · strategy_version = `52wh_v1_survivorship_10y`"
    )

    df = _load_backtest()

    if df.empty:
        st.warning(
            "**No backtest data found.** Go to **Setup & Admin → Step 2** "
            "to run the survivorship-corrected historic backtest (2019–present)."
        )
        return

    closed    = df[df["status"] == "closed"].copy()
    tagged_df = _load_tagged_closed()

    t_overview, t_trades, t_live, t_regime, t_fresh = st.tabs([
        "Overview",
        "Backtest Trades",
        "Live & Signals",
        "Regime Analysis",
        "Freshness Factor",
    ])

    with t_overview:
        _render_overview(closed)

    with t_trades:
        _render_trades(df)

    with t_live:
        live_df, pending_df = _load_live()
        tally_df = _load_conviction_tally()
        _render_live(live_df, pending_df, tally_df)

    with t_regime:
        _render_regime(tagged_df)

    with t_fresh:
        _render_freshness(tagged_df)
