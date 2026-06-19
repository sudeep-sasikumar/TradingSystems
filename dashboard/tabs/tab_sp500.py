"""
S&P 500 52-Week High System — Dashboard Tab (CP-S4 / S9)

Sections:
  1. Full-period stats (2006-present)
  2. Year-by-year table
  3. Equity curve (monthly bars + cumulative line)
  4. Trade log — filterable by year, exit_reason, direction
  5. Delisted exits breakdown
  6. Nifty 500 vs S&P 500 comparison (overlapping 2022-present window)

strategy_version: 'sp500_52wh_v1'
Disclaimer on every section:
  Illustrative, equal-weight, no capital constraints, no costs — not a real
  portfolio simulation. Time-varying S&P 500 membership used for entry signals.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

SV_SP500 = "sp500_52wh_v1"
SV_NIFTY = "52wh_v1"
CUR_YEAR = date.today().year


# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _load_sp500() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(closed, open_) for sp500_52wh_v1."""
    engine = get_engine()
    df = pd.read_sql(
        "SELECT * FROM trades WHERE source='backtest' AND strategy_version=:sv",
        engine, params={"sv": SV_SP500},
    )
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["exit_date"]  = df["exit_date"].apply(
        lambda x: pd.to_datetime(x).date() if pd.notna(x) and x else None
    )
    return df[df["status"] == "closed"].copy(), df[df["status"] == "open"].copy()


@st.cache_data(ttl=300)
def _load_nifty_for_comparison() -> pd.DataFrame:
    """Closed Nifty trades (52wh_v1) for the 2022-present comparison section."""
    engine = get_engine()
    df = pd.read_sql(
        "SELECT * FROM trades WHERE source='backtest' AND strategy_version=:sv "
        "AND status='closed'",
        engine, params={"sv": SV_NIFTY},
    )
    if df.empty:
        return pd.DataFrame()
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["exit_date"]  = pd.to_datetime(df["exit_date"]).dt.date
    return df


# ── Computation helpers ────────────────────────────────────────────────────────

def _stats(df: pd.DataFrame) -> dict:
    if df.empty or "return_pct" not in df.columns:
        return {}
    d = df[df["return_pct"].notna()]
    if d.empty:
        return {}
    w = d[d["return_pct"] > 0]
    return dict(
        total    = len(d),
        wins     = len(w),
        win_pct  = len(w) / len(d) * 100,
        avg_ret  = float(d["return_pct"].mean()),
        med_ret  = float(d["return_pct"].median()),
        best     = float(d["return_pct"].max()),
        worst    = float(d["return_pct"].min()),
        gross    = float(d["return_pct"].sum()),
        avg_days = float(d["holding_days"].mean()) if "holding_days" in d.columns else 0,
    )


def _year_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for yr in sorted(df["trade_year"].dropna().unique()):
        yd = df[df["trade_year"] == int(yr)]
        w  = yd[yd["return_pct"] > 0]
        label = f"{int(yr)} (YTD)" if int(yr) == CUR_YEAR else str(int(yr))
        rows.append({
            "Year":     label,
            "Trades":   len(yd),
            "Win %":    round(len(w) / len(yd) * 100, 1) if len(yd) else 0,
            "Avg %":    round(yd["return_pct"].mean(),   2),
            "Median %": round(yd["return_pct"].median(), 2),
            "Best %":   round(yd["return_pct"].max(),    2),
            "Worst %":  round(yd["return_pct"].min(),    2),
            "Gross %":  round(yd["return_pct"].sum(),    2),
            "Avg Days": round(yd["holding_days"].mean(), 1),
        })
    return pd.DataFrame(rows)


def _equity_figure(df: pd.DataFrame, title_suffix: str = "") -> go.Figure:
    d = df[df["return_pct"].notna()].copy()
    d["exit_dt"] = pd.to_datetime(d["exit_date"])
    d["month"]   = d["exit_dt"].dt.to_period("M").dt.strftime("%Y-%m")
    m = d.groupby("month")["return_pct"].sum().reset_index()
    m.columns = ["month", "gross"]
    m["cum"] = m["gross"].cumsum()

    colors = ["#26a69a" if v >= 0 else "#ef5350" for v in m["gross"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=m["month"], y=m["gross"],
        name="Monthly gross (%pts)",
        marker_color=colors, opacity=0.75,
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
                f"Equity Curve{title_suffix} — "
                "<i>Illustrative, equal-weight, no capital constraints — "
                "not a real portfolio simulation</i>"
            ),
            font_size=13,
        ),
        xaxis=dict(title="", tickangle=-45, tickfont_size=10),
        yaxis=dict(title="Monthly gross (%pts)", side="left"),
        yaxis2=dict(
            title="Cumulative (%pts)", overlaying="y", side="right", showgrid=False
        ),
        legend=dict(orientation="h", y=1.10, x=0),
        hovermode="x unified",
        height=420,
        margin=dict(l=60, r=80, t=90, b=80),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def _metric_cols(s: dict, label: str) -> None:
    if not s:
        st.caption(f"{label}: no data")
        return
    st.caption(label)
    c1, c2, c3 = st.columns(3)
    c1.metric("Trades",        f"{s['total']:,}")
    c1.metric("Win rate",      f"{s['win_pct']:.1f}%")
    c1.metric("Avg return",    f"{s['avg_ret']:+.2f}%")
    c1.metric("Median return", f"{s['med_ret']:+.2f}%")
    c2.metric("Best trade",    f"{s['best']:+.2f}%")
    c2.metric("Worst trade",   f"{s['worst']:+.2f}%")
    c2.metric("Avg hold days", f"{s['avg_days']:.1f}")
    c2.metric("Gross (%pts)",  f"{s['gross']:+,.0f}")
    c3.metric("Winners",       f"{s['wins']:,}")
    c3.metric("Losers",        f"{s['total'] - s['wins']:,}")


# ── Comparison helpers ─────────────────────────────────────────────────────────

def _compare_metric(col, label: str, sp_val, nifty_val, fmt: str = "{:+.1f}%") -> None:
    col.metric(label, fmt.format(sp_val) if sp_val is not None else "—",
               delta=None)


def _compare_block(sp_s: dict, nifty_s: dict) -> None:
    """Side-by-side metrics: SP500 vs Nifty for same window."""
    metrics = [
        ("Trades",        "total",    "{:,}",    int),
        ("Win rate",      "win_pct",  "{:.1f}%", float),
        ("Avg return",    "avg_ret",  "{:+.2f}%",float),
        ("Median return", "med_ret",  "{:+.2f}%",float),
        ("Best trade",    "best",     "{:+.2f}%",float),
        ("Worst trade",   "worst",    "{:+.2f}%",float),
        ("Avg hold days", "avg_days", "{:.1f}d",  float),
        ("Gross (%pts)",  "gross",    "{:+,.0f}", float),
    ]
    header, sp_col, nifty_col = st.columns([2, 1, 1])
    header.markdown("**Metric**")
    sp_col.markdown("**S&P 500**")
    nifty_col.markdown("**Nifty 500**")

    for label, key, fmt, cast in metrics:
        h, sc, nc = st.columns([2, 1, 1])
        h.write(label)
        sp_v    = cast(sp_s[key])    if sp_s    and key in sp_s    else None
        nifty_v = cast(nifty_s[key]) if nifty_s and key in nifty_s else None
        sc.write(fmt.format(sp_v)    if sp_v    is not None else "—")
        nc.write(fmt.format(nifty_v) if nifty_v is not None else "—")


# ── Main render ────────────────────────────────────────────────────────────────

def render_tab() -> None:
    st.header("S&P 500 — 52-Week High Momentum System")

    closed, open_bt = _load_sp500()

    if closed.empty and open_bt.empty:
        st.warning(
            "**S&P 500 backtest has not been run yet.** "
            "Go to **Setup & Admin → Step 5** to download price data (~938 tickers, "
            "2005–present) and run the full backtest. First run takes 45–90 minutes."
        )
        return

    st.warning(
        "**Backtest assumptions** — Equal-weight, unlimited capital, no position cap. "
        "No transaction costs, commissions, slippage, or taxes modelled. "
        "Time-varying S&P 500 membership from fja05680 (Wikipedia-sourced). "
        "Delisted/acquired stocks exit at last available price (`exit_reason='delisted'`). "
        "**Illustrative only — not a real portfolio simulation.**"
    )

    # Top-level counts
    n_delisted  = int((closed["exit_reason"] == "delisted").sum()) if not closed.empty else 0
    n_stop      = int((closed["exit_reason"] == "trailing_stop").sum()) if not closed.empty else 0
    n_open      = len(open_bt)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Closed trades",  f"{len(closed):,}")
    m2.metric("Trailing-stop exits", f"{n_stop:,}")
    m3.metric("Delisted exits", f"{n_delisted:,}", help="Acquired/bankrupt during holding period")
    m4.metric("Still open",     f"{n_open:,}")

    st.divider()

    # ── Sub-tabs ───────────────────────────────────────────────────────────────
    backtest_tab, delist_tab, compare_tab = st.tabs([
        "Backtest Results",
        "Delisted Exits",
        "vs Nifty 500",
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # TAB 1 — BACKTEST RESULTS
    # ═══════════════════════════════════════════════════════════════════════════
    with backtest_tab:

        normal = closed[closed["exit_reason"] == "trailing_stop"].copy()
        all_s  = _stats(closed)
        norm_s = _stats(normal)

        # ── Section 1: Full-period stats ───────────────────────────────────
        st.subheader("Full Period — 2006 to present")

        col_all, col_norm = st.columns(2)
        with col_all:
            _metric_cols(all_s, "ALL closed trades (trailing-stop + delisted exits)")
        with col_norm:
            _metric_cols(norm_s, "Trailing-stop exits only (excludes delisted)")

        st.divider()

        # ── Section 2: Equity curve ────────────────────────────────────────
        if not closed.empty:
            st.plotly_chart(
                _equity_figure(closed, " — S&P 500, 2006–present"),
                use_container_width=True,
            )

        st.divider()

        # ── Section 3: Year-by-year ────────────────────────────────────────
        st.subheader("Year-by-Year Breakdown")
        st.caption("By year trade was **opened**. Closed trades only.")

        if not closed.empty:
            yt = _year_table(closed)
            st.dataframe(
                yt,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Year":     st.column_config.TextColumn("Year",    width=120),
                    "Trades":   st.column_config.NumberColumn("Trades", format="%d"),
                    "Win %":    st.column_config.NumberColumn("Win %",  format="%.1f%%"),
                    "Avg %":    st.column_config.NumberColumn("Avg %",  format="%+.2f%%"),
                    "Median %": st.column_config.NumberColumn("Median %", format="%+.2f%%"),
                    "Best %":   st.column_config.NumberColumn("Best %",   format="%+.2f%%"),
                    "Worst %":  st.column_config.NumberColumn("Worst %",  format="%+.2f%%"),
                    "Gross %":  st.column_config.NumberColumn("Gross %",  format="%+,.0f%%"),
                    "Avg Days": st.column_config.NumberColumn("Avg Days", format="%.0f"),
                },
                height=min(50 + 35 * len(yt), 750),
            )

        st.divider()

        # ── Section 4: Trade log ───────────────────────────────────────────
        st.subheader("Trade Log")

        if not closed.empty:
            col_f1, col_f2, col_f3 = st.columns(3)

            available_years = sorted(closed["trade_year"].dropna().unique(), reverse=True)
            year_opts = ["All years"] + [str(int(y)) for y in available_years]
            sel_year = col_f1.selectbox("Year", year_opts, key="sp500_year_filter")

            reason_opts = ["All", "trailing_stop", "delisted"]
            sel_reason = col_f2.selectbox("Exit reason", reason_opts, key="sp500_reason_filter")

            dir_opts = ["All", "Winners only", "Losers only"]
            sel_dir = col_f3.selectbox("Direction", dir_opts, key="sp500_dir_filter")

            view = closed.copy()
            if sel_year != "All years":
                view = view[view["trade_year"] == int(sel_year)]
            if sel_reason != "All":
                view = view[view["exit_reason"] == sel_reason]
            if sel_dir == "Winners only":
                view = view[view["return_pct"] > 0]
            elif sel_dir == "Losers only":
                view = view[view["return_pct"] <= 0]

            view_display = view[[
                "ticker", "entry_date", "exit_date", "entry_price",
                "exit_price", "return_pct", "holding_days", "exit_reason",
            ]].sort_values("entry_date", ascending=False)

            st.caption(f"Showing {len(view_display):,} trades")
            st.dataframe(
                view_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ticker":       st.column_config.TextColumn("Ticker",      width=90),
                    "entry_date":   st.column_config.DateColumn("Entry",       format="YYYY-MM-DD"),
                    "exit_date":    st.column_config.DateColumn("Exit",        format="YYYY-MM-DD"),
                    "entry_price":  st.column_config.NumberColumn("Entry $",   format="%.2f"),
                    "exit_price":   st.column_config.NumberColumn("Exit $",    format="%.2f"),
                    "return_pct":   st.column_config.NumberColumn("Return %",  format="%+.2f%%"),
                    "holding_days": st.column_config.NumberColumn("Days",      format="%d"),
                    "exit_reason":  st.column_config.TextColumn("Exit reason", width=130),
                },
                height=500,
            )

        # ── Open trades ────────────────────────────────────────────────────
        if not open_bt.empty:
            st.divider()
            st.subheader(f"Open Trades ({len(open_bt)})")
            st.caption(
                "Not yet stopped out. Unrealized returns excluded from closed-trade stats."
            )
            st.dataframe(
                open_bt[[
                    "ticker", "entry_date", "entry_price",
                    "highest_price_reached", "trailing_stop",
                ]].sort_values("entry_date"),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ticker":                st.column_config.TextColumn("Ticker",      width=90),
                    "entry_date":            st.column_config.DateColumn("Entry",       format="YYYY-MM-DD"),
                    "entry_price":           st.column_config.NumberColumn("Entry $",   format="%.2f"),
                    "highest_price_reached": st.column_config.NumberColumn("Peak $",    format="%.2f"),
                    "trailing_stop":         st.column_config.NumberColumn("Stop $",    format="%.2f"),
                },
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # TAB 2 — DELISTED EXITS
    # ═══════════════════════════════════════════════════════════════════════════
    with delist_tab:
        st.subheader("Delisted / Acquired Exits")
        st.markdown(
            "Trades that exited because the stock's price data ended "
            f"≥45 calendar days before today — indicating acquisition, "
            "bankruptcy, or delisting. Exit at **last available adjusted price**.  \n"
            "These may be slightly optimistic (real exit could have been at a gap-down "
            "on announcement day, not captured in daily data)."
        )

        if closed.empty or n_delisted == 0:
            st.info("No delisted exits in this backtest.")
        else:
            delist_df = closed[closed["exit_reason"] == "delisted"].copy()
            delist_s  = _stats(delist_df)

            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Delisted trades",  f"{len(delist_df):,}")
            d2.metric("Win rate",         f"{delist_s['win_pct']:.1f}%" if delist_s else "—")
            d3.metric("Avg return",       f"{delist_s['avg_ret']:+.2f}%" if delist_s else "—")
            d4.metric("Gross (%pts)",     f"{delist_s['gross']:+,.0f}" if delist_s else "—")

            st.divider()
            st.caption(f"{len(delist_df)} delisted exits — sorted by exit date")
            st.dataframe(
                delist_df[[
                    "ticker", "entry_date", "exit_date",
                    "entry_price", "exit_price", "return_pct", "holding_days",
                ]].sort_values("exit_date"),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ticker":       st.column_config.TextColumn("Ticker",     width=90),
                    "entry_date":   st.column_config.DateColumn("Entry",      format="YYYY-MM-DD"),
                    "exit_date":    st.column_config.DateColumn("Last price", format="YYYY-MM-DD"),
                    "entry_price":  st.column_config.NumberColumn("Entry $",  format="%.2f"),
                    "exit_price":   st.column_config.NumberColumn("Last $",   format="%.2f"),
                    "return_pct":   st.column_config.NumberColumn("Return %", format="%+.2f%%"),
                    "holding_days": st.column_config.NumberColumn("Days",     format="%d"),
                },
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # TAB 3 — VS NIFTY 500
    # ═══════════════════════════════════════════════════════════════════════════
    with compare_tab:
        st.subheader("S&P 500 vs Nifty 500 — Strategy Comparison")
        st.markdown(
            "Same 52-week high strategy rules applied to two different universes.  \n"
            "**Left panel**: S&P 500 full backtest period (2006–present).  \n"
            "**Right panel**: S&P 500 and Nifty 500 over the **overlapping window** "
            "(2022–present) — the only apples-to-apples comparison since the Nifty "
            "backtest starts in 2022."
        )
        st.caption(
            "Nifty returns are in INR (no currency adjustment). "
            "S&P 500 returns are in USD. Do not compare gross % directly across currencies."
        )

        nifty_closed = _load_nifty_for_comparison()

        # Full-period S&P 500 stats
        all_sp500_s = _stats(closed)

        # 2022-present overlap window
        overlap_start = date(2022, 1, 1)
        sp500_overlap = closed[closed["entry_date"] >= overlap_start] if not closed.empty else pd.DataFrame()
        nifty_overlap = nifty_closed[nifty_closed["entry_date"] >= overlap_start] if not nifty_closed.empty else pd.DataFrame()

        sp500_overlap_s = _stats(sp500_overlap)
        nifty_overlap_s = _stats(nifty_overlap)

        # ── Full-period S&P 500 ────────────────────────────────────────────
        st.markdown("#### S&P 500 Full Period (2006–present)")
        _metric_cols(all_sp500_s, "2006–present, 938 tickers, time-varying membership")

        st.divider()

        # ── Overlap comparison ─────────────────────────────────────────────
        today_str = date.today().strftime("%Y-%m-%d")
        st.markdown(f"#### Overlapping Window: 2022-01-01 → {today_str}")

        if sp500_overlap_s and nifty_overlap_s:
            _compare_block(sp500_overlap_s, nifty_overlap_s)
        else:
            left, right = st.columns(2)
            with left:
                _metric_cols(sp500_overlap_s, "S&P 500 (2022–present, USD)")
            with right:
                _metric_cols(nifty_overlap_s, "Nifty 500 (2022–present, INR)")

        st.divider()

        # ── Equity curves side by side ─────────────────────────────────────
        st.markdown("#### Equity Curves — Overlapping Window")
        st.caption(
            "Same y-axis scale: cumulative gross %pts (sum of individual trade returns, "
            "equal-weight per trade, NOT compounded). Not comparable in absolute "
            "terms across currencies."
        )

        ec1, ec2 = st.columns(2)
        with ec1:
            if not sp500_overlap.empty:
                st.plotly_chart(
                    _equity_figure(sp500_overlap, " — S&P 500, 2022–present (USD)"),
                    use_container_width=True,
                )
            else:
                st.caption("No S&P 500 data for overlap window.")

        with ec2:
            if not nifty_overlap.empty:
                st.plotly_chart(
                    _equity_figure(nifty_overlap, " — Nifty 500, 2022–present (INR)"),
                    use_container_width=True,
                )
            else:
                st.caption("No Nifty data for comparison.")

        st.divider()
        st.info(
            "**Key differences in the two backtests:**  \n"
            "- **Nifty 500**: survivorship bias (current constituent list only), "
            "2022–present, INR, 15% artifact threshold  \n"
            "- **S&P 500**: time-varying membership (no survivorship bias), "
            "2006–present, USD, 25% artifact threshold, delisting handling  \n"
            "A direct performance comparison requires currency adjustment and a "
            "matched time window — the overlap panel above is the closest proxy."
        )
