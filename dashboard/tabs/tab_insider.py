"""
Dashboard tab — Insider-Trade Cluster Swing System.

Five sub-tabs:
    Watchlist       live qualifying conviction scores + confound flags
    Positions       open and closed live positions
    Backtest        the saved report for a chosen run, rendered inline
    Signal Audit    every signal ever raised, including expired and blocked ones
    Data Health     corpus coverage, noise-filter breakdown, scan-run log

Reads the InsiderSwing SQLite DB (data/insider_swing.db) — a different file
from trading.db, so this tab degrades to an explanatory message rather than an
exception when the insider module has not been populated yet.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

_DASH = Path(__file__).resolve().parent.parent      # dashboard/
_ROOT = _DASH.parent                                # project root
for _p in (str(_ROOT), str(_ROOT / "InsiderSwing"), str(_ROOT / "InsiderSwing" / "sources")):
    if _p not in sys.path:
        sys.path.append(_p)          # append: must not shadow dashboard/ or shared/


# ──────────────────────────────────────────────────────────────────────────────
#  Data access
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=120)
def _q(sql: str, params: dict | None = None) -> pd.DataFrame:
    from db import read_sql
    return read_sql(sql, params or {})


def _table_exists(name: str) -> bool:
    try:
        df = _q("SELECT name FROM sqlite_master WHERE type='table' AND name=:n", {"n": name})
        return not df.empty
    except Exception:
        return False


ROLE_LABELS = {
    "ceo_cfo": "CEO / CFO",
    "officer": "Other officer",
    "director": "Director",
    "ten_pct": "10% owner",
    "other": "Other",
}


def _explain(components_json: str | None) -> str:
    """Human-readable 'why this scored what it scored'."""
    if not components_json:
        return "_No breakdown stored._"
    try:
        d = json.loads(components_json)
    except Exception:
        return "_Breakdown could not be parsed._"

    lines = [
        f"- Cluster credit: **{d.get('cluster_credit')}**  ",
        f"- Role credit: **{d.get('role_credit')}**  ",
        f"- Size credit: **{d.get('size_credit')}** ({d.get('size_credit_basis')})  ",
        f"- Novelty fraction: **{d.get('novelty_fraction')}**  ",
        f"- Earnings penalty applied: **{d.get('earnings_penalty')}**",
        "",
        "**Insiders in the window**",
    ]
    for ins in d.get("insiders", []):
        bits = [
            f"`{ins.get('filing_date')}`",
            f"**{ins.get('name')}**",
            f"({ROLE_LABELS.get(ins.get('role'), ins.get('role'))})",
            f"${(ins.get('buy_value') or 0):,.0f}",
        ]
        if ins.get("size_ratio"):
            bits.append(f"— {ins['size_ratio']:.1f}× their own trailing average")
        if ins.get("first_buy_in_lookback"):
            bits.append("— **first buy in 12 months**")
        lines.append("- " + " ".join(bits))
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
#  Sub-tabs
# ──────────────────────────────────────────────────────────────────────────────

def _render_watchlist() -> None:
    st.subheader("Live watchlist")
    st.caption(
        "Symbols whose insider conviction score cleared the threshold, keyed on the "
        "**filing date** — the date the disclosure became public, never the transaction date."
    )

    signals = _q(
        "SELECT id, ticker, signal_date, score, cluster_count, role_bucket_top, status, "
        "       sell_pressure_flag, earnings_proximity_flag, trigger_date, trigger_type, "
        "       expiry_date, block_reason, components_json "
        "FROM ins_signals WHERE source='live' ORDER BY signal_date DESC, score DESC LIMIT 400"
    )
    if signals.empty:
        st.info("No live signals yet. The scanner writes them after its first successful run.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pending", int((signals["status"] == "pending").sum()))
    c2.metric("Confirmed", int((signals["status"] == "confirmed").sum()))
    c3.metric("Expired (no trigger)", int((signals["status"] == "expired").sum()))
    c4.metric("Blocked", int((signals["status"] == "blocked").sum()))

    statuses = st.multiselect(
        "Status", sorted(signals["status"].dropna().unique()),
        default=[s for s in ("pending", "confirmed") if s in set(signals["status"])],
    )
    view = signals[signals["status"].isin(statuses)] if statuses else signals

    display = view.copy()
    display["role"] = display["role_bucket_top"].map(ROLE_LABELS).fillna(display["role_bucket_top"])
    display["earnings flag"] = display["earnings_proximity_flag"].map({1: "⚠️", 0: ""})
    display["sell pressure"] = display["sell_pressure_flag"].map({1: "🚩", 0: ""})

    st.dataframe(
        display[["signal_date", "ticker", "score", "cluster_count", "role", "status",
                 "trigger_date", "trigger_type", "earnings flag", "sell pressure",
                 "expiry_date", "block_reason"]],
        use_container_width=True, hide_index=True,
    )

    st.markdown("---")
    st.markdown("#### Score audit")
    st.caption("Every score can be explained after the fact — pick one to see exactly "
               "which insiders and which components produced it.")
    if not view.empty:
        options = view.apply(
            lambda r: f"{r['signal_date']} · {r['ticker']} · {r['score']:.0f}/100", axis=1
        ).tolist()
        pick = st.selectbox("Signal", options, index=0)
        row = view.iloc[options.index(pick)]
        st.markdown(_explain(row["components_json"]))


def _render_positions() -> None:
    st.subheader("Positions")
    positions = _q("SELECT * FROM ins_positions ORDER BY entry_date DESC")
    if positions.empty:
        st.info("No positions recorded. Positions are created when a confirmed signal "
                "is accepted from the Telegram alert.")
        return

    open_pos = positions[positions["status"] == "open"]
    closed = positions[positions["status"] == "closed"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Open", len(open_pos))
    c2.metric("Closed", len(closed))
    if not closed.empty:
        pnl = pd.to_numeric(closed["realized_pnl"], errors="coerce").sum()
        c3.metric("Realised P&L", f"${pnl:,.0f}")

    st.markdown("#### Open")
    if open_pos.empty:
        st.caption("None.")
    else:
        st.dataframe(
            open_pos[["ticker", "entry_date", "entry_price", "qty", "initial_stop",
                      "trailing_stop", "target_price", "time_stop_date", "score_at_entry",
                      "cluster_count"]],
            use_container_width=True, hide_index=True,
        )

    st.markdown("#### Closed")
    if closed.empty:
        st.caption("None.")
    else:
        st.dataframe(
            closed[["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
                    "exit_reason", "realized_pnl", "r_multiple", "score_at_entry"]],
            use_container_width=True, hide_index=True,
        )


def _render_backtest() -> None:
    st.subheader("Backtest")
    runs = _q("SELECT id, label, param_key, start_date, end_date, finished_at, status, "
              "       universe_with_price, price_coverage_pct, signals_generated, "
              "       signals_expired, report_path, notes, metrics_json "
              "FROM ins_backtest_runs ORDER BY id DESC")
    if runs.empty:
        st.info("No backtest runs yet.\n\n"
                "```bash\npython InsiderSwing/run_insider.py --checkpoint backtest\n```")
        return

    labels = runs.apply(
        lambda r: f"#{r['id']} · {r['label']} · {r['start_date']}→{r['end_date']} · {r['param_key']}",
        axis=1,
    ).tolist()
    pick = st.selectbox("Run", labels, index=0)
    run = runs.iloc[labels.index(pick)]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Universe priced", int(run["universe_with_price"] or 0))
    c2.metric("Price coverage", f"{run['price_coverage_pct'] or 0:.1f}%")
    c3.metric("Signals", int(run["signals_generated"] or 0))
    c4.metric("Expired", int(run["signals_expired"] or 0))

    # Headline per-arm table straight from the stored metrics blob.
    try:
        metrics = json.loads(run["metrics_json"] or "{}")
    except Exception:
        metrics = {}

    if metrics.get("arms"):
        rows = []
        for arm, a in metrics["arms"].items():
            t, e = a.get("trade_stats", {}), a.get("equity_stats", {})
            rows.append({
                "arm": arm, "trades": t.get("trades"), "win %": t.get("win_rate_pct"),
                "expectancy (R)": t.get("expectancy_r"), "profit factor": t.get("profit_factor"),
                "CAGR %": e.get("cagr_pct"), "Sharpe": e.get("sharpe"),
                "max DD %": e.get("max_drawdown_pct"), "thin sample": t.get("thin_sample"),
            })
        st.markdown("#### Arms")
        st.caption("`insider_only` = the raw factor · `tech_only` = the timing rule with no "
                   "insider filter · `combined` = the live strategy. Reporting only the last "
                   "one would make it impossible to tell which half is doing the work.")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        checks = metrics.get("statistical_checks", {})
        if checks:
            st.markdown("#### Statistical honesty checks")
            for arm, verdicts in checks.items():
                for v in verdicts:
                    (st.warning if ("THIN" in v or "not statistically" in v) else st.success)(
                        f"**{arm}** — {v}"
                    )

    trades = _q("SELECT * FROM ins_trades WHERE run_id=:rid", {"rid": int(run["id"])})
    if not trades.empty:
        st.markdown("#### Trade log")
        arms = st.multiselect("Arm", sorted(trades["arm"].unique()),
                              default=["combined"] if "combined" in set(trades["arm"]) else None)
        view = trades[trades["arm"].isin(arms)] if arms else trades
        st.dataframe(
            view[["arm", "ticker", "signal_date", "entry_date", "exit_date", "entry_price",
                  "exit_price", "exit_reason", "net_pnl", "r_multiple", "holding_days",
                  "cluster_bucket", "role_bucket", "trigger_type", "earnings_flag"]],
            use_container_width=True, hide_index=True,
        )

    if run["notes"]:
        st.warning(run["notes"])

    path = run["report_path"]
    if path and Path(str(path)).exists():
        with st.expander("Full saved report"):
            st.markdown(Path(str(path)).read_text(encoding="utf-8"))
    elif path:
        st.caption(f"Saved report not found on disk: `{path}`")


def _render_audit() -> None:
    st.subheader("Signal audit log")
    st.caption(
        "Every signal ever raised, including the ones that never traded. Expired signals "
        "are kept deliberately: they are what quantifies how much edge is lost by requiring "
        "a technical confirmation rather than taking the insider signal alone."
    )
    df = _q("SELECT id, run_id, source, ticker, signal_date, param_key, score, cluster_count, "
            "       role_bucket_top, status, block_reason, trigger_date, trigger_type, "
            "       expiry_date, created_at "
            "FROM ins_signals ORDER BY signal_date DESC LIMIT 3000")
    if df.empty:
        st.info("No signals recorded yet.")
        return

    c1, c2 = st.columns(2)
    src = c1.multiselect("Source", sorted(df["source"].dropna().unique()))
    sts = c2.multiselect("Status", sorted(df["status"].dropna().unique()))
    view = df
    if src:
        view = view[view["source"].isin(src)]
    if sts:
        view = view[view["status"].isin(sts)]

    st.dataframe(view, use_container_width=True, hide_index=True)
    st.download_button("Download CSV", view.to_csv(index=False).encode(),
                       "insider_signal_audit.csv", "text/csv")


def _render_health() -> None:
    st.subheader("Data health")

    summary = _q(
        "SELECT COUNT(*) AS filings, MIN(filing_date) AS first_filing, "
        "       MAX(filing_date) AS last_filing, "
        "       SUM(CASE WHEN aff_10b5_one IS NOT NULL THEN 1 ELSE 0 END) AS plan_flag_known "
        "FROM ins_filings"
    )
    txn = _q("SELECT COUNT(*) AS n, COUNT(DISTINCT ticker) AS tickers FROM ins_transactions")

    if summary.empty or int(summary.iloc[0]["filings"] or 0) == 0:
        st.info("No Form 4 data ingested yet.\n\n"
                "```bash\npython InsiderSwing/run_insider.py --checkpoint ingest --start 2014-01-01\n```")
        return

    s, t = summary.iloc[0], txn.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Form 4 filings", f"{int(s['filings']):,}")
    c2.metric("Transaction lines", f"{int(t['n']):,}")
    c3.metric("Distinct tickers", f"{int(t['tickers']):,}")
    c4.metric("Coverage", f"{s['first_filing']} → {s['last_filing']}")

    known = int(s["plan_flag_known"] or 0)
    total = int(s["filings"] or 1)
    st.caption(
        f"Rule 10b5-1 checkbox present on **{known:,} / {total:,}** filings "
        f"({100.0 * known / total:.1f}%). The checkbox only exists on Form 4s from 2022 "
        "onward; earlier filings are detected only via a footnote mention, so some "
        "pre-scheduled plan trades survive the noise filter in the older part of the sample."
    )

    st.markdown("#### Noise filter breakdown")
    st.caption("The raw Form 4 feed is overwhelmingly mechanical — awards, RSU vesting, "
               "tax withholding, option exercises and gifts. Only discretionary open-market "
               "trades carry information about what the insider thinks the stock is worth.")
    breakdown = _q(
        "SELECT classification, COALESCE(exclude_reason, '—') AS reason, COUNT(*) AS n "
        "FROM ins_transactions GROUP BY 1, 2 ORDER BY n DESC"
    )
    if not breakdown.empty:
        breakdown["share %"] = (100.0 * breakdown["n"] / breakdown["n"].sum()).round(2)
        st.dataframe(breakdown, use_container_width=True, hide_index=True)

    st.markdown("#### Scan runs")
    scans = _q("SELECT id, trigger_type, start_time, end_time, status, universe_scanned, "
               "       filings_ingested, scores_computed, new_signals, triggers_fired, "
               "       signals_expired, exits_recorded, latest_filing_date, error_message "
               "FROM ins_scan_runs ORDER BY id DESC LIMIT 40")
    if scans.empty:
        st.caption("No scanner runs recorded yet.")
    else:
        stale = scans[scans["status"] == "stale_data"]
        if not stale.empty:
            st.warning("The scanner has flagged stale data. EDGAR should carry filings from "
                       "the last few business days at all times — prolonged silence usually "
                       "means a broken fetch, not a quiet market.")
        st.dataframe(scans, use_container_width=True, hide_index=True)

    st.markdown("#### Ingest runs")
    ing = _q("SELECT id, source, mode, start_date, end_date, status, filings_seen, "
             "       filings_inserted, txns_inserted, fetch_failures, error_message "
             "FROM ins_ingest_runs ORDER BY id DESC LIMIT 25")
    if not ing.empty:
        st.dataframe(ing, use_container_width=True, hide_index=True)


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

def render_tab() -> None:
    st.header("Insider-Trade Cluster Swing System")
    st.caption(
        "Trades on **SEC Form 4 filings** — legally required *public* disclosures that "
        "corporate insiders must file within 2 business days of transacting in their own "
        "company's stock. This is the documented insider-purchase anomaly (Seyhun; "
        "Lakonishok & Lee), not trading on material non-public information. Every input is "
        "a public SEC document."
    )

    if not _table_exists("ins_filings"):
        st.warning(
            "The InsiderSwing database has not been created yet. Build it in order:\n\n"
            "```bash\n"
            "python InsiderSwing/run_insider.py --checkpoint universe\n"
            "python InsiderSwing/run_insider.py --checkpoint ingest --start 2014-01-01\n"
            "python InsiderSwing/run_insider.py --checkpoint score\n"
            "python InsiderSwing/run_insider.py --checkpoint backtest\n"
            "```\n\n"
            "Ingest the full universe well before the backtest start date — the relative-size "
            "score component compares each buy against that insider's own trailing 2-year "
            "average, which needs 2 years of prior history to mean anything."
        )
        return

    tabs = st.tabs(["Watchlist", "Positions", "Backtest", "Signal Audit", "Data Health"])
    with tabs[0]:
        _render_watchlist()
    with tabs[1]:
        _render_positions()
    with tabs[2]:
        _render_backtest()
    with tabs[3]:
        _render_audit()
    with tabs[4]:
        _render_health()
