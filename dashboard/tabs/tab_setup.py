"""
Setup & Admin tab — populate all backtest and analysis data for a fresh deployment.

Run Steps 1-7 in order on a fresh VPS. Each step is safe to re-run.
Long-running steps (historic backtest: 20-45 min) block this browser session;
keep the tab open until complete. For unattended first-time setup, SSH into the
VPS and run the CLI commands shown in the Advanced section below.

Step 8 (Insider Swing) is deliberately separate from "Run Everything": its EDGAR
backfill alone takes longer than every other step combined, and bundling it would
turn a 2-hour setup into an overnight one with no way to tell what is still running.
"""
import subprocess
import sys
from pathlib import Path

import streamlit as st
from sqlalchemy import text

_DASH = Path(__file__).resolve().parent.parent  # dashboard/
_ROOT = _DASH.parent                             # project root
sys.path.insert(0, str(_ROOT))

from shared.db import get_engine

_PY             = sys.executable
_RUN_BACKTEST   = str(_ROOT / "52WeekHigh" / "run_backtest.py")
_RUN_HISTORIC   = str(_ROOT / "52WeekHigh" / "run_historic_backtest.py")
_RUN_REGIME     = str(_ROOT / "52WeekHigh" / "run_regime_analysis.py")
_RUN_SP500      = str(_ROOT / "SP500" / "run_sp500_backtest.py")
_RUN_SP500_SCAN = str(_ROOT / "SP500" / "scanner" / "scanner.py")
_RUN_INSIDER    = str(_ROOT / "InsiderSwing" / "run_insider.py")


def _insider_counts() -> dict:
    """
    Row counts from the InsiderSwing DB (a SEPARATE SQLite file from trading.db).

    Imported lazily and by explicit file path: InsiderSwing/ uses flat absolute
    imports and contains db.py / models.py / universe.py, which would shadow the
    dashboard's own modules if the package directory went on sys.path here.
    Returns zeros rather than raising when the module has not been built yet.
    """
    empty = {"filings": 0, "transactions": 0, "buys": 0, "scores": 0,
             "signals": 0, "trades": 0, "runs": 0, "last_filing": None, "available": False}
    try:
        import importlib.util

        saved = list(sys.path)
        try:
            for p in (str(_ROOT / "InsiderSwing"), str(_ROOT / "InsiderSwing" / "sources")):
                if p not in sys.path:
                    sys.path.append(p)
            spec = importlib.util.spec_from_file_location(
                "insiderswing_db_status", _ROOT / "InsiderSwing" / "db.py"
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["insiderswing_db_status"] = mod
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = saved

        def _one(sql: str):
            try:
                df = mod.read_sql(sql)
                return df.iloc[0, 0] if not df.empty else 0
            except Exception:
                return 0

        return {
            "filings": int(_one("SELECT COUNT(*) FROM ins_filings") or 0),
            "transactions": int(_one("SELECT COUNT(*) FROM ins_transactions") or 0),
            "buys": int(_one("SELECT COUNT(*) FROM ins_transactions "
                             "WHERE classification='open_market_buy'") or 0),
            "scores": int(_one("SELECT COUNT(*) FROM ins_scores") or 0),
            "signals": int(_one("SELECT COUNT(*) FROM ins_signals") or 0),
            "trades": int(_one("SELECT COUNT(*) FROM ins_trades") or 0),
            "runs": int(_one("SELECT COUNT(*) FROM ins_backtest_runs") or 0),
            "last_filing": _one("SELECT MAX(filing_date) FROM ins_filings") or None,
            "available": True,
        }
    except Exception:
        return empty


# ── Public entry point ─────────────────────────────────────────────────────────

def render_tab() -> None:
    st.header("Setup & Admin")
    st.caption(
        "Fresh deployment? Run Steps 1–3 in order. "
        "Each step is safe to re-run — it overwrites existing data for that strategy version."
    )

    _db_status()
    st.divider()

    # ── Step 1 ─────────────────────────────────────────────────────────────────
    st.subheader("Step 1 — Original Backtest (2022–present)")
    st.markdown(
        "Downloads ~500 Nifty 500 stock prices from yfinance (2021-present) and runs the "
        "52-week-high strategy backtest.  \n"
        "**strategy\\_version:** `52wh_v1` &nbsp;|&nbsp; "
        "**Est. runtime:** 5–15 min (first run) · ~1 min (cache hit)"
    )
    if st.button("▶  Run Original Backtest", key="btn_orig"):
        _run_step(
            label="Original Backtest (52wh_v1)",
            cmd=[_PY, _RUN_BACKTEST, "--checkpoint", "backtest"],
            timeout=2400,
        )
        st.info("Done — click **Refresh Status** at the top to see updated trade counts.")

    st.divider()

    # ── Step 2 ─────────────────────────────────────────────────────────────────
    st.subheader("Step 2 — Survivorship-Corrected Historic Backtest (2019–present)")
    st.markdown(
        "Builds the point-in-time Nifty 500 membership table from the committed reconstitution "
        "PDFs, then downloads price data back to Oct 2019 and runs the extended backtest.  \n"
        "**strategy\\_version:** `52wh_v1_survivorship_10y` &nbsp;|&nbsp; "
        "**Est. runtime:** 20–45 min (first run) · ~5 min (cache hit)  \n"
        "⚠️  Keep this browser tab open — the process runs synchronously."
    )
    if st.button("▶  Run Historic Backtest", key="btn_hist"):
        ok = _run_step(
            label="2a — Build index membership table",
            cmd=[_PY, _RUN_HISTORIC, "--checkpoint", "membership"],
            timeout=300,
        )
        if ok:
            _run_step(
                label="2b — Historic backtest (downloads prices 2018-present)",
                cmd=[_PY, _RUN_HISTORIC, "--checkpoint", "backtest"],
                timeout=7200,
            )
        st.info("Done — click **Refresh Status** at the top to see updated trade counts.")

    st.divider()

    # ── Step 3 ─────────────────────────────────────────────────────────────────
    st.subheader("Step 3 — Tag Regimes (both datasets)")
    st.markdown(
        "Downloads Nifty 500 index data (^CRSLDX), computes 200-DMA + 6M trailing quintile "
        "regime signals, and tags every backtest trade in both strategy versions.  \n"
        "Required for the Regime Analysis tab to show any data.  \n"
        "**Est. runtime:** ~2–5 min per dataset"
    )
    if st.button("▶  Tag Regimes (both datasets)", key="btn_regime"):
        _run_step(
            label="Regime tags — 52wh_v1 (original)",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "tag", "--strategy-version", "52wh_v1"],
            timeout=600,
        )
        _run_step(
            label="Regime tags — 52wh_v1_survivorship_10y (historic)",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "tag",
                 "--strategy-version", "52wh_v1_survivorship_10y"],
            timeout=600,
        )
        st.info("Done — click **Refresh Status** at the top to see updated tag counts.")

    st.divider()

    # ── Step 4 — SP500 membership ──────────────────────────────────────────────
    st.subheader("Step 4 — S&P 500 Historical Membership (CP-S2)")
    st.markdown(
        "Downloads the fja05680/sp500 CSV from GitHub (Wikipedia-sourced constituent "
        "changes since 1996) and populates the `sp500_membership` table.  \n"
        "**Est. runtime:** < 1 min  \n"
        "Required before Step 5."
    )
    if st.button("▶  Build S&P 500 Membership Table", key="btn_sp500_member"):
        _run_step(
            label="S&P 500 Membership (CP-S2)",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "membership"],
            timeout=120,
        )
        st.info("Done — click **Refresh Status** to see updated membership row count.")

    st.divider()

    # ── Step 5 — SP500 backtest ────────────────────────────────────────────────
    st.subheader("Step 5 — S&P 500 Backtest, 2006–present (CP-S3)")
    st.markdown(
        "Downloads adjusted daily close for all ~900 historical S&P 500 members "
        "(price data from 2005-01-01 for 252-day warm-up), runs the 52-week-high "
        "strategy with time-varying membership and explicit delisting handling, "
        "and saves results as `strategy_version=sp500_52wh_v1`.  \n"
        "**strategy\\_version:** `sp500_52wh_v1` &nbsp;|&nbsp; "
        "**Est. runtime:** 45–90 min on first run  \n"
        "⚠️  Keep this browser tab open — the process runs synchronously."
    )
    if st.button("▶  Run S&P 500 Backtest (Steps 4 + 5)", key="btn_sp500_backtest"):
        st.markdown("**5a — Building membership table (fast, < 1 min)**")
        ok = _run_step(
            label="S&P 500 Membership (CP-S2)",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "membership"],
            timeout=120,
        )
        if ok:
            st.markdown("**5b — Running backtest (45–90 min on first run)**")
            _run_step(
                label="S&P 500 Backtest 2006-present (CP-S3)",
                cmd=[_PY, _RUN_SP500, "--checkpoint", "backtest"],
                timeout=9000,
            )
        st.info("Done — click **Refresh Status** to see updated trade counts.")

    st.divider()

    # ── Step 6 — SP500 regime ──────────────────────────────────────────────────
    st.subheader("Step 6 — S&P 500 Regime Analysis (CP-S4)")
    st.markdown(
        "Downloads ^GSPC (S&P 500 index) and ^VIX daily closes from 2004 to today, "
        "computes the 200-DMA regime (*bull* / *bear*) and VIX tier (*calm* / *elevated* / *stressed*), "
        "and populates the `sp500_market_regime` table.  \n"
        "Required for the **Regime Analysis** sub-tab in the S&P 500 dashboard.  \n"
        "**Est. runtime:** ~1–2 min"
    )
    if st.button("▶  Build S&P 500 Regime Table", key="btn_sp500_regime"):
        _run_step(
            label="S&P 500 Regime (CP-S4) — ^GSPC + ^VIX 200-DMA",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "regime"],
            timeout=300,
        )
        st.info("Done — open the **S&P 500 → Regime Analysis** tab to see the breakdown.")

    st.divider()

    # ── Step 7 — All freshness ─────────────────────────────────────────────────
    st.subheader("Step 7 — Tag All Freshness Factor (NSE + S&P 500)")
    st.markdown(
        "Computes the trading-day gap between each trade's entry and the previous time the "
        "same stock made a new 52-week high — for all three backtest datasets at once.  \n"
        "- **NSE (original):** stores freshness in `trade_regime_tags` · run after Step 3  \n"
        "- **NSE (historic):** same table, historic dataset · run after Step 3  \n"
        "- **S&P 500:** stores freshness in `sp500_trade_freshness` · run after Step 5  \n"
        "Safe to re-run.  Re-run NSE steps if Step 3 (regime tags) is re-run.  \n"
        "**Est. runtime:** ~10–25 min total (reads parquet cache only, no network calls).  \n"
        "**After it completes:** click **Reload** in the **NSE Historic → Freshness Factor** "
        "section or **S&P 500 → Freshness Factor** tab to see updated data."
    )
    if st.button("▶  Tag All Freshness (NSE + S&P 500)", key="btn_freshness_all", type="primary"):
        _run_step(
            label="Freshness — NSE original (52wh_v1)",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "freshness",
                 "--strategy-version", "52wh_v1"],
            timeout=600,
        )
        _run_step(
            label="Freshness — NSE historic (52wh_v1_survivorship_10y)",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "freshness",
                 "--strategy-version", "52wh_v1_survivorship_10y"],
            timeout=600,
        )
        _run_step(
            label="Freshness — S&P 500 (sp500_52wh_v1)",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "freshness"],
            timeout=900,
        )
        st.info(
            "Done — click **Reload** in the freshness tabs to see results:  \n"
            "- **Nifty 500 — Historic** → scroll to Freshness Factor section  \n"
            "- **S&P 500** → Freshness Factor tab  \n"
            "- **Nifty 500 — Regime Analysis** → Freshness Factor tab (full cross-tab)"
        )

    st.divider()

    # ── Optional — Scanner test run ───────────────────────────────────────────
    st.subheader("Optional — Test S&P 500 Scanner")
    st.markdown(
        "Runs the S&P 500 EOD scanner **immediately** (bypasses the 21:30 UTC schedule).  \n"
        "The scanner normally runs automatically at **21:30 UTC Mon–Fri** inside Docker.  \n"
        "⚠️  Only run this after US market close \\(after 9 PM UTC\\) to see today's close prices\\."
    )
    if st.button("▶  Test S&P 500 Scanner (--run-now)", key="btn_sp500_scan_test"):
        _run_step(
            label="S&P 500 Scanner test run",
            cmd=[_PY, _RUN_SP500_SCAN, "--run-now"],
            timeout=900,
        )
        st.info("Done — any new signals will appear in Telegram within 60 seconds.")

    st.divider()

    # ── Step 8 — Insider Swing ─────────────────────────────────────────────────
    _insider_section()

    st.divider()

    # ── Run All ────────────────────────────────────────────────────────────────
    st.subheader("Run Everything (Steps 1 → 7)")
    st.warning(
        "Runs all Nifty + S&P 500 steps sequentially, including freshness tagging. "
        "**Total runtime: 100–165 min** on first run (price downloads for both universes). "
        "Do not close this browser tab. If you prefer, SSH into the VPS and use the CLI "
        "commands in the Advanced section below instead."
    )
    if st.button("▶  Run Everything (Steps 1–7)", key="btn_all", type="primary"):
        st.markdown("**Step 1 — Original Backtest**")
        _run_step(
            label="Original Backtest (52wh_v1)",
            cmd=[_PY, _RUN_BACKTEST, "--checkpoint", "backtest"],
            timeout=2400,
        )
        st.markdown("**Step 2a — Historic Membership Table**")
        ok = _run_step(
            label="Build index membership table",
            cmd=[_PY, _RUN_HISTORIC, "--checkpoint", "membership"],
            timeout=300,
        )
        st.markdown("**Step 2b — Historic Backtest**")
        if ok:
            _run_step(
                label="Historic Backtest (52wh_v1_survivorship_10y)",
                cmd=[_PY, _RUN_HISTORIC, "--checkpoint", "backtest"],
                timeout=7200,
            )
        st.markdown("**Step 3a — Regime Tags (original)**")
        _run_step(
            label="Regime tags — 52wh_v1",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "tag", "--strategy-version", "52wh_v1"],
            timeout=600,
        )
        st.markdown("**Step 3b — Regime Tags (historic)**")
        _run_step(
            label="Regime tags — 52wh_v1_survivorship_10y",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "tag",
                 "--strategy-version", "52wh_v1_survivorship_10y"],
            timeout=600,
        )
        st.markdown("**Step 4 — S&P 500 Membership**")
        sp500_ok = _run_step(
            label="S&P 500 Membership (CP-S2)",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "membership"],
            timeout=120,
        )
        st.markdown("**Step 5 — S&P 500 Backtest**")
        if sp500_ok:
            _run_step(
                label="S&P 500 Backtest 2006-present (CP-S3)",
                cmd=[_PY, _RUN_SP500, "--checkpoint", "backtest"],
                timeout=9000,
            )
        st.markdown("**Step 6 — S&P 500 Regime**")
        _run_step(
            label="S&P 500 Regime (CP-S4) — ^GSPC + ^VIX 200-DMA",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "regime"],
            timeout=300,
        )
        st.markdown("**Step 7a — NSE Freshness (original)**")
        _run_step(
            label="Freshness — 52wh_v1",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "freshness",
                 "--strategy-version", "52wh_v1"],
            timeout=600,
        )
        st.markdown("**Step 7b — NSE Freshness (historic)**")
        _run_step(
            label="Freshness — 52wh_v1_survivorship_10y",
            cmd=[_PY, _RUN_REGIME, "--checkpoint", "freshness",
                 "--strategy-version", "52wh_v1_survivorship_10y"],
            timeout=600,
        )
        st.markdown("**Step 7c — S&P 500 Freshness**")
        _run_step(
            label="S&P 500 Freshness Factor",
            cmd=[_PY, _RUN_SP500, "--checkpoint", "freshness"],
            timeout=900,
        )
        st.success(
            "All steps complete — click **Refresh Status** at the top, then **Reload** "
            "in the freshness tabs to see results."
        )

    st.divider()
    _advanced_section()


# ── Step 8 — Insider Swing pipeline ────────────────────────────────────────────

def _insider_section() -> None:
    """
    One button that runs the entire insider-cluster pipeline end to end.

    Deliberately NOT folded into "Run Everything": the EDGAR backfill alone is
    longer than every other step in this tab combined, and bundling it would
    turn a 2-hour setup into an overnight one with no way to tell which part is
    still running.
    """
    st.subheader("Step 8 — Insider-Trade Cluster Swing (full pipeline)")
    st.markdown(
        "Runs the whole insider system in order: **universe → ingest → score → backtest** "
        "(optionally the parameter-stability sweep too). Populates the **Insider Swing** tab.  \n"
        "**strategy\\_version:** `insider_v1` &nbsp;|&nbsp; "
        "**DB:** `data/insider_swing.db` (separate from `trading.db`)"
    )

    ins = _insider_counts()

    # Two hard prerequisites, checked up front rather than failing 40 minutes in.
    engine = get_engine()
    try:
        with engine.connect() as conn:
            n_members = int(conn.execute(text("SELECT COUNT(*) FROM sp500_membership")).scalar() or 0)
    except Exception:
        n_members = 0

    import os
    ua = os.getenv("INSIDER_SEC_USER_AGENT", "")
    ua_ok = bool(ua) and "@" in ua and "example.com" not in ua

    if n_members < 400:
        st.error(
            "**Blocked — run Step 4 first.** The insider universe is built from the "
            "point-in-time `sp500_membership` table (so delisted names are included and the "
            "backtest isn't survivorship-biased). That table is empty or incomplete."
        )
    if not ua_ok:
        st.error(
            "**Blocked — set `INSIDER_SEC_USER_AGENT` in `.env`.** The SEC requires a "
            "descriptive User-Agent containing a real contact address on every request "
            "(e.g. `Sudeep Sasikumar TradingSystems (you@example.org)`). Anonymous or "
            "placeholder requests are throttled and then blocked outright.  \n"
            "After editing `.env`: `docker compose up -d` to pick it up."
        )

    c1, c2, c3 = st.columns([1, 1, 2])
    start_date = c1.text_input("Ingest / backtest start", value="2016-01-01",
                               key="ins_start",
                               help="Ingest reaches back 2 extra years automatically — the "
                                    "relative-size score compares each buy to that insider's "
                                    "own trailing 2-year average.")
    quick = c2.checkbox("Quick test (12 tickers)", value=False, key="ins_quick",
                        help="Runs the whole pipeline on a small ticker set in ~10 min so you "
                             "can verify the plumbing before committing to the full backfill.")
    run_sweep = c3.checkbox("Also run the parameter-stability sweep", value=False,
                            key="ins_sweep",
                            help="80 full simulations. Adds 10-40 min on the full universe. "
                                 "Answers whether performance is a plateau or an overfit spike.")

    if quick:
        st.info(
            "**Quick test mode** — 12 liquid large caps, not a real result. Use it to confirm "
            "EDGAR access, the SEC User-Agent, and the report pipeline all work. Any backtest "
            "number it produces will be flagged as a thin sample, correctly."
        )
    else:
        st.warning(
            "**Full run: 3–8 hours**, dominated by the first EDGAR backfill "
            "(~600 issuers × 10 years of Form 4 documents at the SEC's 8 req/s ceiling). "
            "Every document is cached to disk gzipped and the DB constraints make re-runs "
            "idempotent, so this is **resumable** — if it dies, click again and it picks up "
            "where it left off.  \n"
            "For an unattended first run, prefer SSH + the CLI command in the Advanced "
            "section: a browser tab held open for 6 hours is the fragile part, not the job."
        )

    tickers_arg = ["--tickers", "INTC,F,KMI,OXY,T,PARA,WBA,MRNA,DVN,APA,HAL,NEM"] if quick else []
    ingest_start = _shift_year(start_date, -2)

    disabled = (n_members < 400) or (not ua_ok)
    if st.button("▶  Run Full Insider Pipeline", key="btn_insider", type="primary",
                 disabled=disabled):
        st.markdown("**8a — Universe & CIK resolution**")
        ok = _run_step(
            label="Insider universe + SEC CIK map",
            cmd=[_PY, _RUN_INSIDER, "--checkpoint", "universe", "--start", start_date],
            timeout=900,
        )

        if ok:
            st.markdown(f"**8b — Ingest Form 4 filings from {ingest_start}** "
                        "(the long one — resumable, disk-cached)")
            ok = _run_step(
                label=f"EDGAR Form 4 ingest ({ingest_start} → today)",
                cmd=[_PY, _RUN_INSIDER, "--checkpoint", "ingest",
                     "--start", ingest_start] + tickers_arg,
                timeout=28800,      # 8h — the backfill is genuinely this long
            )

        if ok:
            st.markdown("**8c — Conviction scoring**")
            ok = _run_step(
                label="Conviction scores (0-100, with audit breakdown)",
                cmd=[_PY, _RUN_INSIDER, "--checkpoint", "score",
                     "--start", start_date] + tickers_arg,
                timeout=3600,
            )

        if ok:
            st.markdown("**8d — Three-arm backtest + saved report**")
            ok = _run_step(
                label="Backtest (insider_only / tech_only / combined)",
                cmd=[_PY, _RUN_INSIDER, "--checkpoint", "backtest",
                     "--start", start_date, "--label", "vps_baseline"] + tickers_arg,
                timeout=14400,
            )

        if ok and run_sweep:
            st.markdown("**8e — Parameter stability sweep**")
            _run_step(
                label="Parameter sweep (80 cells)",
                cmd=[_PY, _RUN_INSIDER, "--checkpoint", "sweep",
                     "--start", start_date, "--label", "vps"] + tickers_arg,
                timeout=14400,
            )

        if ok:
            st.success(
                "Insider pipeline complete — open the **Insider Swing** tab. The verdict is "
                "printed at the top of the Backtest sub-tab and in the saved report under "
                "`data/reports/insider/`. A flat or negative result there is a real finding, "
                "not a failed run."
            )
        else:
            st.error("Pipeline stopped at the failed step above — later steps were skipped "
                     "because each one depends on the previous one's output.")

    # Current state
    if ins["available"] and ins["filings"]:
        st.markdown("**Current insider data**")
        i1, i2, i3, i4, i5 = st.columns(5)
        i1.metric("Form 4 filings", f"{ins['filings']:,}")
        i2.metric("Open-market buys", f"{ins['buys']:,}",
                  help=f"of {ins['transactions']:,} transaction lines — the rest are awards, "
                       "vesting, exercises and gifts, which carry no conviction information")
        i3.metric("Scores", f"{ins['scores']:,}")
        i4.metric("Signals", f"{ins['signals']:,}")
        i5.metric("Backtest runs", f"{ins['runs']:,}")
        if ins["last_filing"]:
            st.caption(f"Newest stored filing date: **{ins['last_filing']}**")


def _shift_year(date_str: str, delta: int) -> str:
    """Shift a YYYY-MM-DD string by whole years; returns the input on bad format."""
    try:
        y, m, d = (int(x) for x in str(date_str).strip().split("-"))
        return f"{y + delta:04d}-{m:02d}-{d:02d}"
    except Exception:
        return date_str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _db_status() -> None:
    st.subheader("Current Database Status")

    col_refresh, _ = st.columns([1, 5])
    with col_refresh:
        if st.button("🔄  Refresh Status", key="btn_refresh"):
            st.rerun()

    engine = get_engine()

    def _q(sql: str) -> int:
        try:
            with engine.connect() as conn:
                return int(conn.execute(text(sql)).scalar() or 0)
        except Exception:
            return 0

    n_orig      = _q("SELECT COUNT(*) FROM trades WHERE strategy_version='52wh_v1' AND source='backtest'")
    n_hist      = _q("SELECT COUNT(*) FROM trades WHERE strategy_version='52wh_v1_survivorship_10y' AND source='backtest'")
    n_tags_orig = _q(
        "SELECT COUNT(*) FROM trade_regime_tags trt "
        "JOIN trades t ON trt.trade_id = t.id "
        "WHERE t.strategy_version = '52wh_v1'"
    )
    n_tags_hist = _q(
        "SELECT COUNT(*) FROM trade_regime_tags trt "
        "JOIN trades t ON trt.trade_id = t.id "
        "WHERE t.strategy_version = '52wh_v1_survivorship_10y'"
    )
    n_live         = _q("SELECT COUNT(*) FROM trades WHERE source='live'")
    n_membership   = _q("SELECT COUNT(*) FROM index_membership")
    n_sp500_member = _q("SELECT COUNT(*) FROM sp500_membership")
    n_sp500_trades = _q("SELECT COUNT(*) FROM trades WHERE strategy_version='sp500_52wh_v1' AND source='backtest'")

    st.markdown("**Nifty 500 (India)**")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Original Backtest",   f"{n_orig:,}",       help="strategy_version=52wh_v1, source=backtest")
    c2.metric("Historic Backtest",   f"{n_hist:,}",       help="strategy_version=52wh_v1_survivorship_10y")
    c3.metric("Regime Tags (Orig)",  f"{n_tags_orig:,}")
    c4.metric("Regime Tags (Hist)",  f"{n_tags_hist:,}")
    c5.metric("Live Trades",         f"{n_live:,}")
    c6.metric("Nifty Membership",    f"{n_membership:,}", help="Point-in-time Nifty 500 membership intervals")

    n_sp500_regime   = _q("SELECT COUNT(*) FROM sp500_market_regime")
    n_sp500_live     = _q("SELECT COUNT(*) FROM trades WHERE strategy_version='sp500_52wh_v1' AND source='live'")
    n_sp500_pend     = _q("SELECT COUNT(*) FROM signals WHERE strategy_version='sp500_52wh_v1' AND status='pending'")
    n_sp500_freshness = _q("SELECT COUNT(*) FROM sp500_trade_freshness")
    n_fresh_orig     = _q(
        "SELECT COUNT(*) FROM trade_regime_tags trt "
        "JOIN trades t ON trt.trade_id = t.id "
        "WHERE t.strategy_version = '52wh_v1' AND trt.freshness_category IS NOT NULL"
    )
    n_fresh_hist     = _q(
        "SELECT COUNT(*) FROM trade_regime_tags trt "
        "JOIN trades t ON trt.trade_id = t.id "
        "WHERE t.strategy_version = '52wh_v1_survivorship_10y' AND trt.freshness_category IS NOT NULL"
    )

    st.markdown("**S&P 500 (US)**")
    d1, d2, d3, d4, d5, d6 = st.columns(6)
    d1.metric("SP500 Membership",    f"{n_sp500_member:,}",   help="Historical constituent intervals")
    d2.metric("SP500 Backtest",      f"{n_sp500_trades:,}",   help="strategy_version=sp500_52wh_v1, source=backtest")
    d3.metric("SP500 Regime Days",   f"{n_sp500_regime:,}",   help="^GSPC 200-DMA + VIX daily rows")
    d4.metric("SP500 Freshness",     f"{n_sp500_freshness:,}", help="sp500_trade_freshness rows (Step 9)")
    d5.metric("SP500 Live Trades",   f"{n_sp500_live:,}",     help="Open + closed live trades")
    d6.metric("SP500 Pending Sigs",  f"{n_sp500_pend:,}",     help="Pending S&P 500 signals (awaiting Accept/Reject)")

    ins = _insider_counts()
    st.markdown("**Insider Swing (US Form 4)**")
    e1, e2, e3, e4, e5, e6 = st.columns(6)
    e1.metric("Form 4 Filings",   f"{ins['filings']:,}",      help="ins_filings — raw EDGAR corpus")
    e2.metric("Open-Market Buys", f"{ins['buys']:,}",         help="Survivors of the noise filter; the rest are awards/vesting/exercises/gifts")
    e3.metric("Conviction Scores", f"{ins['scores']:,}",      help="ins_scores — one per (ticker, filing date)")
    e4.metric("Insider Signals",  f"{ins['signals']:,}",      help="Includes expired and blocked signals, kept deliberately")
    e5.metric("Insider Trades",   f"{ins['trades']:,}",       help="ins_trades across all three backtest arms")
    e6.metric("Backtest Runs",    f"{ins['runs']:,}",         help="ins_backtest_runs")

    st.markdown("**Nifty Freshness**")
    nf1, nf2 = st.columns(2)
    nf1.metric("Nifty Fresh (Original)",  f"{n_fresh_orig:,}",
               help="trade_regime_tags rows with freshness_category set — 52wh_v1")
    nf2.metric("Nifty Fresh (Historic)",  f"{n_fresh_hist:,}",
               help="trade_regime_tags rows with freshness_category set — 52wh_v1_survivorship_10y")

    nifty_checks = {
        "Nifty original backtest":   n_orig > 500,
        "Nifty historic backtest":   n_hist > 500,
        "Nifty membership table":    n_membership > 0,
        "Nifty regime tags (orig)":  n_tags_orig > 500,
        "Nifty regime tags (hist)":  n_tags_hist > 500,
        "Nifty freshness (orig)":    n_fresh_orig > 500,
        "Nifty freshness (hist)":    n_fresh_hist > 500,
    }
    sp500_checks = {
        "S&P 500 membership table":  n_sp500_member > 400,
        "S&P 500 backtest trades":   n_sp500_trades > 500,
        "S&P 500 regime table":      n_sp500_regime > 1000,
        "S&P 500 freshness":         n_sp500_freshness > 500,
    }
    all_checks = {**nifty_checks, **sp500_checks}

    if all(all_checks.values()):
        st.success("All data populated — dashboard is fully operational.")
    else:
        missing = [k for k, ok in all_checks.items() if not ok]
        st.warning(f"Missing: {', '.join(missing)}.")

    # Insider Swing is reported separately, not folded into the check above: its
    # backfill takes hours and is optional, so a missing insider corpus should not
    # make an otherwise-complete Nifty/S&P 500 deployment look broken.
    if ins["runs"] > 0:
        st.success("Insider Swing populated — see the **Insider Swing** tab.")
    elif ins["filings"] > 0:
        st.info("**Insider Swing:** Form 4 data is ingested but no backtest has run yet. "
                "Use **Step 8** below.")
    else:
        st.info("**Insider Swing:** not populated. Use **Step 8** below "
                "(needs Step 4 first, plus `INSIDER_SEC_USER_AGENT` in `.env`).")

    # Contextual next-step guidance
    if n_sp500_trades > 500 and n_sp500_regime == 0:
        st.info(
            "**Next step (S&P 500):** Run **Step 6 — S&P 500 Regime** below. "
            "Your backtest is complete; regime tags enable the Regime Analysis sub-tab. (~1–2 min)"
        )
    if (n_sp500_trades > 500 and n_sp500_freshness == 0) or (n_tags_hist > 500 and n_fresh_hist == 0):
        st.info(
            "**Next step:** Run **Step 7 — Tag All Freshness (NSE + S&P 500)** below. "
            "Backtest and regime data are present; freshness enables the Freshness Factor sections. (~10–25 min)"
        )
    if n_orig > 500 and n_tags_orig == 0:
        st.info(
            "**Next step:** Run **Step 3 — Tag Regimes** below. "
            "Your backtest data is present; regime tags are what the Regime Analysis tab "
            "and live conviction tiers need. (~2–5 min per dataset)"
        )
    elif n_orig == 0:
        st.info("**Fresh deployment:** Start with **Step 1** to run the original backtest.")


def _run_step(label: str, cmd: list, timeout: int) -> bool:
    """Run a subprocess and stream status. Returns True on success."""
    with st.status(f"{label} — running…", expanded=True) as status:
        st.write(f"`{' '.join(str(c) for c in cmd)}`")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(_ROOT),
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            combined = (result.stdout or "") + (result.stderr or "")
            tail = combined[-5000:] if len(combined) > 5000 else combined
            if result.returncode == 0:
                status.update(label=f"✅  {label} — Done", state="complete", expanded=False)
            else:
                status.update(
                    label=f"❌  {label} — Failed (exit {result.returncode})",
                    state="error",
                    expanded=True,
                )
            st.code(tail)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            status.update(
                label=f"⏱  {label} — Timed out ({timeout // 60} min)",
                state="error",
            )
            st.error(
                f"Process exceeded the {timeout // 60}-minute timeout. "
                "SSH into the VPS and run the command directly (see Advanced section)."
            )
            return False
        except Exception as exc:
            status.update(label=f"❌  {label} — Error", state="error")
            st.error(str(exc))
            return False


def _advanced_section() -> None:
    with st.expander("Advanced — CLI Commands & Force Refresh"):
        st.markdown("""
**Preferred approach for first-time VPS setup (run inside the container):**

```bash
# Step 1: original Nifty backtest (~5-15 min)
docker compose exec dashboard python 52WeekHigh/run_backtest.py --checkpoint backtest

# Step 2: Nifty historic backtest (~20-45 min first run)
docker compose exec dashboard python 52WeekHigh/run_historic_backtest.py --checkpoint membership
docker compose exec dashboard python 52WeekHigh/run_historic_backtest.py --checkpoint backtest

# Step 3: Nifty regime tags (~2-5 min per dataset)
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1_survivorship_10y

# Step 4: S&P 500 membership table (< 1 min)
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint membership

# Step 5: S&P 500 backtest (45-90 min, ~900 tickers x 20 years of prices)
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint backtest

# Step 6: S&P 500 regime analysis (~1-2 min, downloads ^GSPC + ^VIX)
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint regime

# Step 7: Tag All Freshness (NSE + S&P 500, ~10-25 min, after Steps 3 and 5)
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint freshness --strategy-version 52wh_v1
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint freshness --strategy-version 52wh_v1_survivorship_10y
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint freshness

# Optional: S&P 500 scanner test run (after US market close)
docker compose exec sp500_scanner python SP500/scanner/scanner.py --run-now

# Step 8: Insider Swing full pipeline (3-8 h, dominated by the EDGAR backfill)
# PREFER THIS OVER THE BUTTON for the first full run — a browser tab held open
# for six hours is the fragile part, not the job. Every fetched document is
# cached to disk and the DB constraints make re-runs idempotent, so it resumes.
docker compose exec dashboard python InsiderSwing/run_insider.py --checkpoint universe
docker compose exec dashboard python InsiderSwing/run_insider.py --checkpoint ingest --start 2014-01-01
docker compose exec dashboard python InsiderSwing/run_insider.py --checkpoint score --start 2016-01-01
docker compose exec dashboard python InsiderSwing/run_insider.py --checkpoint backtest --start 2016-01-01 --label vps_baseline
docker compose exec dashboard python InsiderSwing/run_insider.py --checkpoint sweep --start 2016-01-01

# Survive an SSH disconnect during the multi-hour backfill:
docker compose exec -d dashboard python InsiderSwing/run_insider.py --checkpoint ingest --start 2014-01-01
docker compose logs -f dashboard
```

**Insider Swing prerequisites (both are enforced by the Step 8 button):**
```bash
# 1. sp500_membership must be populated (Step 4) — it is the point-in-time,
#    survivorship-corrected universe the insider system builds on.
# 2. .env must set a real SEC contact address; the SEC blocks anonymous requests:
#      INSIDER_SEC_USER_AGENT=Your Name TradingSystems (you@yourdomain.com)
docker compose up -d          # reload .env after editing

# Insider scanner service (fires 23:15 UTC Mon-Fri = 19:15 ET, after EDGAR's
# 17:30 ET same-day filing cutoff, so the day's Form 4 cohort is complete):
docker compose ps insider_scanner
docker compose logs -f insider_scanner
docker compose exec insider_scanner python InsiderSwing/scanner.py --run-now

# Re-run the noise filter over the stored corpus without re-fetching from EDGAR:
docker compose exec dashboard python InsiderSwing/run_insider.py --checkpoint reclassify
```

**S&P 500 scanner continuous service (runs in `sp500_scanner` Docker container):**
```bash
# The sp500_scanner service fires at 21:30 UTC Mon-Fri automatically.
# Check it's running:
docker compose ps sp500_scanner
docker compose logs -f sp500_scanner

# Restart if needed:
docker compose restart sp500_scanner
```

**Force re-download all price data (clears cache):**

```bash
docker compose exec dashboard python 52WeekHigh/run_backtest.py --checkpoint backtest --force-refresh
docker compose exec dashboard python 52WeekHigh/run_historic_backtest.py --checkpoint backtest --force-refresh
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1 --force-refresh
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1_survivorship_10y --force-refresh
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint membership --force-refresh
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint backtest --force-refresh
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint regime
```

**Deploy latest code after `git pull`:**

```bash
git pull
docker compose up --build -d
docker compose ps   # verify all five services are running
                    # scanner · bot · sp500_scanner · insider_scanner · dashboard
```
""")
