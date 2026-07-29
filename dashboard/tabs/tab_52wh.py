"""
52-Week High System — Dashboard Tab (Checkpoint 3)

Backtest sections 1-4: fully implemented.
Live sections 5-7: placeholders — wired up at Checkpoint 4+.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

STRATEGY_VERSION = "52wh_v1"
CUR_YEAR = date.today().year

# Corporate action artifacts confirmed in diagnostic (2026-06-18).
# These 4 trades have exits triggered by yfinance data errors (wrong split ex-date
# or demerger with no yfinance representation), not real price moves.
KNOWN_ARTIFACTS = {
    ("CGCL.NS",       "2024-01-01"),  # 4x split — yfinance records wrong ex-date
    ("GPIL.NS",       "2024-01-01"),  # 5x split — yfinance records wrong ex-date
    ("MOTILALOFS.NS", "2024-01-01"),  # 4x split — yfinance records wrong ex-date
    ("VEDL.NS",       "2026-04-30"),  # demerger — not representable in yfinance
}


# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def _load_backtest() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (closed_df, open_df) for strategy_version='52wh_v1'."""
    engine = get_engine()
    df = pd.read_sql(
        "SELECT * FROM trades WHERE source='backtest' AND strategy_version=:sv",
        engine,
        params={"sv": STRATEGY_VERSION},
    )
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["exit_date"]  = df["exit_date"].apply(
        lambda x: pd.to_datetime(x).date() if pd.notna(x) and x else None
    )

    closed = df[df["status"] == "closed"].copy()
    open_  = df[df["status"] == "open"].copy()

    closed["is_artifact"] = closed.apply(
        lambda r: (r["ticker"], str(r["exit_date"])) in KNOWN_ARTIFACTS, axis=1
    )
    return closed, open_


@st.cache_data(ttl=60)
def _load_live() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (live_trades_df, pending_signals_df).
    live_trades_df includes conviction_tier / regime_score via LEFT JOIN to signals.
    """
    engine = get_engine()
    try:
        live = pd.read_sql(
            """
            SELECT t.*,
                   s.conviction_tier,
                   s.regime_score
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
    """Signals by tier x status, for the running acceptance tally."""
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


# ── Computation helpers ────────────────────────────────────────────────────────

def _stats(df: pd.DataFrame) -> dict:
    if df.empty or "return_pct" not in df.columns:
        return {}
    d = df[df["return_pct"].notna()]
    w = d[d["return_pct"] > 0]
    return dict(
        total=len(d),
        wins=len(w),
        win_pct=len(w) / len(d) * 100 if len(d) else 0,
        avg_ret=d["return_pct"].mean(),
        med_ret=d["return_pct"].median(),
        best=d["return_pct"].max(),
        worst=d["return_pct"].min(),
        gross=d["return_pct"].sum(),
        avg_days=d["holding_days"].mean() if "holding_days" in d.columns else 0,
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
            "Win %":    round(len(w) / len(yd) * 100, 1),
            "Avg %":    round(yd["return_pct"].mean(),   2),
            "Median %": round(yd["return_pct"].median(), 2),
            "Best %":   round(yd["return_pct"].max(),    2),
            "Worst %":  round(yd["return_pct"].min(),    2),
            "Gross %":  round(yd["return_pct"].sum(),    2),
            "Avg Days": round(yd["holding_days"].mean(), 1),
        })
    return pd.DataFrame(rows)


def _equity_figure(df: pd.DataFrame) -> go.Figure:
    """Monthly gross return bars + cumulative sum line (dual y-axis)."""
    d = df[df["return_pct"].notna()].copy()
    d["exit_dt"] = pd.to_datetime(d["exit_date"])
    d["month"]   = d["exit_dt"].dt.to_period("M").dt.strftime("%Y-%m")
    m = d.groupby("month")["return_pct"].sum().reset_index()
    m.columns = ["month", "gross"]
    m["cum"] = m["gross"].cumsum()

    bar_colors = ["#26a69a" if v >= 0 else "#ef5350" for v in m["gross"]]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=m["month"], y=m["gross"],
        name="Monthly gross (%pts)",
        marker_color=bar_colors, opacity=0.75,
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


# ── Metric display ─────────────────────────────────────────────────────────────

def _metric_block(s: dict, label: str) -> None:
    """3-column metric block for a stats dict."""
    if not s:
        st.caption(f"{label}: no data")
        return
    st.caption(label)
    c1, c2, c3 = st.columns(3)
    c1.metric("Trades",         f"{s['total']:,}")
    c1.metric("Win rate",       f"{s['win_pct']:.1f}%")
    c1.metric("Avg return",     f"{s['avg_ret']:+.2f}%")
    c1.metric("Median return",  f"{s['med_ret']:+.2f}%")
    c2.metric("Best trade",     f"{s['best']:+.2f}%")
    c2.metric("Worst trade",    f"{s['worst']:+.2f}%")
    c2.metric("Avg hold days",  f"{s['avg_days']:.1f}")
    c2.metric("Gross (%pts)",   f"{s['gross']:+,.0f}")
    c3.metric("Winners",        f"{s['wins']:,}")
    c3.metric("Losers",         f"{s['total'] - s['wins']:,}")


# ── Trade log column config ────────────────────────────────────────────────────

_TRADE_COL_CFG = {
    "ticker":       st.column_config.TextColumn("Ticker",     width=110),
    "company_name": st.column_config.TextColumn("Company",    width=200),
    "entry_date":   st.column_config.DateColumn("Entry",      format="YYYY-MM-DD"),
    "exit_date":    st.column_config.DateColumn("Exit",       format="YYYY-MM-DD"),
    "entry_price":  st.column_config.NumberColumn("Entry ₹",  format="%.2f"),
    "exit_price":   st.column_config.NumberColumn("Exit ₹",   format="%.2f"),
    "return_pct":   st.column_config.NumberColumn("Return %", format="%+.2f"),
    "holding_days": st.column_config.NumberColumn("Days",     format="%d"),
    "is_artifact":  st.column_config.CheckboxColumn("Corp. Action?", width=130),
}


# ── Main render ───────────────────────────────────────────────────────────────

def render_tab() -> None:
    st.header("52-Week High Momentum System")

    closed, open_bt = _load_backtest()

    if closed.empty:
        st.warning(
            "**Backtest has not been run on this server yet.** "
            "Click the button below to download Nifty 500 data and run the full backtest. "
            "This takes approximately 5–10 minutes."
        )
        if st.button("Run Backtest Now", type="primary", icon="▶"):
            import subprocess
            with st.spinner(
                "Running backtest — downloading ~500 tickers and computing signals... "
                "please wait (5–10 min)"
            ):
                result = subprocess.run(
                    [sys.executable,
                     str(_ROOT / "52WeekHigh" / "run_backtest.py"),
                     "--checkpoint", "backtest"],
                    capture_output=True, text=True, cwd=str(_ROOT),
                    timeout=1800,
                )
            if result.returncode == 0:
                st.success("Backtest complete! Loading results...")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error("Backtest failed. See details below.")
                st.code((result.stderr or result.stdout)[-3000:])
        return

    st.warning(
        "**Backtest assumptions** — Equal-weight, unlimited capital, no position "
        "cap applied retroactively. No transaction costs, slippage, STT, or "
        "brokerage modelled. Universe = current Nifty 500 (survivorship bias — "
        "stocks added / removed between 2022 and today are not reflected). "
        "**Illustrative, equal-weight, no capital constraints — "
        "not a real portfolio simulation.**"
    )

    bt_tab, live_tab = st.tabs(["Backtest Results", "Live Trading"])

    # ═════════════════════════════════════════════════════════════════════════
    # BACKTEST TAB
    # ═════════════════════════════════════════════════════════════════════════
    with bt_tab:

        n_art   = int(closed["is_artifact"].sum())
        clean   = closed[~closed["is_artifact"]]
        all_s   = _stats(closed)
        cln_s   = _stats(clean)

        # ── SECTION 1 — Combined stats ─────────────────────────────────────
        st.subheader("Full Period — 2022 to present")

        col_orig, col_clean = st.columns(2)
        with col_orig:
            _metric_block(all_s, "ALL trades (including known artifacts)")
        with col_clean:
            _metric_block(
                cln_s,
                f"Artifacts excluded ({n_art} trades: CGCL/GPIL/MOTILALOFS Jan-2024 "
                f"splits + VEDL Apr-2026 demerger)",
            )

        with st.expander("What are 'corporate action artifacts'?"):
            st.markdown(
                """
**4 trades are flagged as data artifacts** confirmed by the diagnostic run on 2026-06-18:

| Ticker | Exit date | Move | Root cause |
|---|---|---|---|
| CGCL | 2024-01-01 | -74.8% | 4x split ex-date ≈ Jan 1 — yfinance records it months later, auto_adjust fails |
| GPIL | 2024-01-01 | -79.5% | 5x split ex-date ≈ Jan 1 — same root cause |
| MOTILALOFS | 2024-01-01 | -74.6% | 4x split ex-date ≈ Jan 1 — same root cause |
| VEDL | 2026-04-30 | -64.9% | Demerger (VAML/TSPL/MEL/VISL spun off) — yfinance has no mechanism to represent this |

The actual investor received additional shares (splits) or spun-off shares (demerger) —
the backtest cannot track this so it records a large loss that never happened.

**These are yfinance data quality limitations, not strategy flaws.**
All other large moves in the backtest are confirmed genuine market events
(India 2024 election results, Adani-Hindenburg, SEBI actions, Sony-ZEEL merger collapse).
                """
            )

        # ── SECTION 2 — Year-by-year ───────────────────────────────────────
        st.divider()
        st.subheader("Year-by-Year Breakdown")
        st.caption("Grouped by the year each trade was opened.")

        excl_toggle = st.toggle("Exclude corporate action artifacts (4 trades)", value=False)
        yt_df = _year_table(clean if excl_toggle else closed)

        st.dataframe(
            yt_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Year":     st.column_config.TextColumn("Year",     width=130),
                "Trades":   st.column_config.NumberColumn("Trades", format="%d"),
                "Win %":    st.column_config.NumberColumn("Win %",  format="%.1f"),
                "Avg %":    st.column_config.NumberColumn("Avg %",  format="%+.2f"),
                "Median %": st.column_config.NumberColumn("Median %", format="%+.2f"),
                "Best %":   st.column_config.NumberColumn("Best %", format="%+.2f"),
                "Worst %":  st.column_config.NumberColumn("Worst %", format="%+.2f"),
                "Gross %":  st.column_config.NumberColumn("Gross %pts", format="%+.2f"),
                "Avg Days": st.column_config.NumberColumn("Avg Days", format="%.1f"),
            },
        )

        # ── SECTION 3 — Equity curve ───────────────────────────────────────
        st.divider()
        st.subheader("Equity Curve")
        eq_df = clean if excl_toggle else closed
        st.plotly_chart(_equity_figure(eq_df), use_container_width=True)

        # ── SECTION 4 — Trade log ──────────────────────────────────────────
        st.divider()
        st.subheader("Trade Log")

        fc1, fc2, fc3, fc4 = st.columns([2, 1, 1, 2])
        with fc1:
            all_years = sorted([int(y) for y in closed["trade_year"].dropna().unique()])
            yr_filter = st.multiselect("Filter by year (entry)", all_years, default=all_years)
        with fc2:
            wl_filter = st.selectbox("Win / Loss", ["All", "Winners", "Losers"])
        with fc3:
            hide_art  = st.checkbox("Hide artifacts", value=False)
        with fc4:
            sort_by = st.selectbox(
                "Sort by",
                ["return_pct", "entry_date", "holding_days", "ticker"],
                index=0,
                format_func=lambda x: {
                    "return_pct": "Return %",
                    "entry_date": "Entry date",
                    "holding_days": "Days held",
                    "ticker": "Ticker",
                }[x],
            )

        log = closed.copy()
        if yr_filter:
            log = log[log["trade_year"].isin(yr_filter)]
        if wl_filter == "Winners":
            log = log[log["return_pct"] > 0]
        elif wl_filter == "Losers":
            log = log[log["return_pct"] <= 0]
        if hide_art:
            log = log[~log["is_artifact"]]

        ascending = sort_by not in ("return_pct",)
        log = log.sort_values(sort_by, ascending=ascending)

        disp_cols = [
            "ticker", "company_name", "entry_date", "exit_date",
            "entry_price", "exit_price", "return_pct", "holding_days", "is_artifact",
        ]
        disp = log[[c for c in disp_cols if c in log.columns]]
        st.caption(f"{len(disp):,} trades shown (of {len(closed):,} total closed)")
        st.dataframe(
            disp,
            hide_index=True,
            use_container_width=True,
            column_config=_TRADE_COL_CFG,
        )

        # ── Open positions at backtest end ─────────────────────────────────
        if not open_bt.empty:
            st.divider()
            st.subheader(f"Open at Backtest End — {len(open_bt)} positions")
            st.caption(
                "These trades were not stopped out as of the last backtest date. "
                "Returns are unrealized and excluded from all stats above."
            )
            open_disp_cols = [
                "ticker", "company_name", "entry_date",
                "entry_price", "highest_price_reached", "trailing_stop",
            ]
            open_disp = open_bt[
                [c for c in open_disp_cols if c in open_bt.columns]
            ].sort_values("entry_date")
            st.dataframe(
                open_disp,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "ticker":               st.column_config.TextColumn("Ticker"),
                    "company_name":         st.column_config.TextColumn("Company"),
                    "entry_date":           st.column_config.DateColumn("Entry", format="YYYY-MM-DD"),
                    "entry_price":          st.column_config.NumberColumn("Entry ₹",   format="%.2f"),
                    "highest_price_reached":st.column_config.NumberColumn("Peak ₹",    format="%.2f"),
                    "trailing_stop":        st.column_config.NumberColumn("Stop ₹",    format="%.2f"),
                },
            )

    # ═════════════════════════════════════════════════════════════════════════
    # LIVE TRADING TAB
    # ═════════════════════════════════════════════════════════════════════════
    with live_tab:
        live_df, pending_df = _load_live()
        tally_df = _load_conviction_tally()

        # ── SECTION 5 — Open positions ────────────────────────────────────────
        st.subheader("Open Positions")
        if not live_df.empty:
            open_live = live_df[live_df["status"] == "open"].copy()
            if not open_live.empty:
                open_cols = [
                    "ticker", "company_name", "entry_date",
                    "entry_price", "highest_price_reached", "trailing_stop",
                    "conviction_tier", "regime_score",
                ]
                open_disp = open_live[[c for c in open_cols if c in open_live.columns]]
                _tier_note = {
                    "HIGH":     "HIGH CONVICTION",
                    "STANDARD": "Standard",
                    "AVOID":    "AVOID ⚠",
                }
                if "conviction_tier" in open_disp.columns:
                    open_disp = open_disp.copy()
                    open_disp["conviction_tier"] = open_disp["conviction_tier"].map(
                        lambda x: _tier_note.get(x, x or "—")
                    )
                st.dataframe(
                    open_disp,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "ticker":                 st.column_config.TextColumn("Ticker"),
                        "company_name":           st.column_config.TextColumn("Company"),
                        "entry_date":             st.column_config.DateColumn("Entry", format="YYYY-MM-DD"),
                        "entry_price":            st.column_config.NumberColumn("Entry Rs.", format="%.2f"),
                        "highest_price_reached":  st.column_config.NumberColumn("Peak Rs.", format="%.2f"),
                        "trailing_stop":          st.column_config.NumberColumn("Stop Rs.", format="%.2f"),
                        "conviction_tier":        st.column_config.TextColumn("Conviction", width=160),
                        "regime_score":           st.column_config.NumberColumn("Score", format="%d"),
                    },
                )
            else:
                st.caption("No open live positions.")
        else:
            st.caption("No live trade data yet.")

        st.divider()

        # ── SECTION 6 — Pending signals ───────────────────────────────────────
        st.subheader("Pending Signals (awaiting Accept / Reject)")
        if not pending_df.empty:
            sig_cols = [
                "ticker", "company_name", "signal_date", "signal_price",
                "benchmark_252d", "signal_type", "conviction_tier", "regime_score",
                "scan_timestamp",
            ]
            sig_disp = pending_df[[c for c in sig_cols if c in pending_df.columns]].copy()
            if "conviction_tier" in sig_disp.columns:
                _tier_note = {"HIGH": "HIGH CONVICTION", "STANDARD": "Standard", "AVOID": "AVOID ⚠"}
                sig_disp["conviction_tier"] = sig_disp["conviction_tier"].map(
                    lambda x: _tier_note.get(x, x or "—")
                )
            st.dataframe(
                sig_disp,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "ticker":          st.column_config.TextColumn("Ticker"),
                    "company_name":    st.column_config.TextColumn("Company"),
                    "signal_date":     st.column_config.TextColumn("Date"),
                    "signal_price":    st.column_config.NumberColumn("Signal Rs.", format="%.2f"),
                    "benchmark_252d":  st.column_config.NumberColumn("252d High", format="%.2f"),
                    "signal_type":     st.column_config.TextColumn("Type"),
                    "conviction_tier": st.column_config.TextColumn("Conviction", width=160),
                    "regime_score":    st.column_config.NumberColumn("Score", format="%d"),
                    "scan_timestamp":  st.column_config.TextColumn("Scanned"),
                },
            )
        else:
            st.caption("No pending signals.")

        st.divider()

        # ── SECTION 7 — Conviction Tier Tally ────────────────────────────────
        st.subheader("Conviction Tier — Running Tally")
        st.caption(
            "Signals generated since Checkpoint 8b (conviction tier tracking). "
            "Your historical accept/reject pattern per tier."
        )
        if tally_df.empty:
            st.info(
                "No conviction-tagged signals yet. "
                "Conviction tiers are recorded on new signals generated by the live scanner."
            )
        else:
            # Pivot to tier × status table
            pivot = tally_df.pivot_table(
                index="conviction_tier", columns="status", values="n", aggfunc="sum", fill_value=0
            ).reset_index()
            pivot.columns.name = None

            for col in ["pending", "accepted", "rejected", "expired"]:
                if col not in pivot.columns:
                    pivot[col] = 0

            pivot["Total"] = pivot[["pending", "accepted", "rejected", "expired"]].sum(axis=1)
            pivot["Accept%"] = (
                pivot["accepted"] / pivot["Total"].replace(0, 1) * 100
            ).round(1)

            tier_order = {"HIGH": 0, "STANDARD": 1, "AVOID": 2}
            pivot["_ord"] = pivot["conviction_tier"].map(lambda x: tier_order.get(x, 9))
            pivot = pivot.sort_values("_ord").drop(columns=["_ord"])
            pivot = pivot.rename(columns={
                "conviction_tier": "Tier",
                "pending":  "Pending",
                "accepted": "Accepted",
                "rejected": "Rejected",
                "expired":  "Expired",
            })
            disp_cols = ["Tier", "Total", "Pending", "Accepted", "Rejected", "Expired", "Accept%"]
            st.dataframe(
                pivot[[c for c in disp_cols if c in pivot.columns]],
                hide_index=True,
                use_container_width=False,
                column_config={
                    "Accept%": st.column_config.NumberColumn("Accept%", format="%.1f%%"),
                },
            )

        st.divider()

        # ── SECTION 8 — Recent activity ───────────────────────────────────────
        st.subheader("Recent Activity")
        if not live_df.empty:
            recent = live_df.sort_values("entry_date", ascending=False).head(20)
            st.dataframe(recent, hide_index=True, use_container_width=True)
        else:
            st.caption("No activity yet.")
