"""
Setup & Admin tab — populate all backtest and analysis data for a fresh deployment.

Run Steps 1-3 in order on a fresh VPS. Each step is safe to re-run.
Long-running steps (historic backtest: 20-45 min) block this browser session;
keep the tab open until complete. For unattended first-time setup, SSH into the
VPS and run the CLI commands shown in the Advanced section below.
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

    # ── Run All ────────────────────────────────────────────────────────────────
    st.subheader("Run All Steps (1 → 2 → 3 → 4 → 5)")
    st.warning(
        "Runs all Nifty + S&P 500 steps sequentially. **Total runtime: 90–150 min** "
        "on first run (price downloads for both universes). "
        "Do not close this browser tab. If you prefer, SSH into the VPS and use the CLI "
        "commands in the Advanced section below instead."
    )
    if st.button("▶  Run All Steps", key="btn_all", type="primary"):
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
        st.success("All steps complete — click **Refresh Status** at the top to verify.")

    st.divider()
    _advanced_section()


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

    st.markdown("**S&P 500 (US)**")
    d1, d2 = st.columns(2)
    d1.metric("SP500 Membership Rows",  f"{n_sp500_member:,}", help="Historical constituent intervals (sp500_membership)")
    d2.metric("SP500 Backtest Trades",  f"{n_sp500_trades:,}", help="strategy_version=sp500_52wh_v1, source=backtest")

    nifty_checks = {
        "Nifty original backtest":  n_orig > 500,
        "Nifty historic backtest":  n_hist > 500,
        "Nifty membership table":   n_membership > 0,
        "Nifty regime tags (orig)": n_tags_orig > 500,
        "Nifty regime tags (hist)": n_tags_hist > 500,
    }
    sp500_checks = {
        "S&P 500 membership table": n_sp500_member > 400,
        "S&P 500 backtest trades":  n_sp500_trades > 500,
    }
    all_checks = {**nifty_checks, **sp500_checks}

    if all(all_checks.values()):
        st.success("All data populated — dashboard is fully operational.")
    else:
        missing = [k for k, ok in all_checks.items() if not ok]
        st.warning(f"Missing: {', '.join(missing)}.")

    # Contextual next-step guidance
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
# Step 1: original Nifty backtest
docker compose exec dashboard python 52WeekHigh/run_backtest.py --checkpoint backtest

# Step 2: Nifty historic backtest
docker compose exec dashboard python 52WeekHigh/run_historic_backtest.py --checkpoint membership
docker compose exec dashboard python 52WeekHigh/run_historic_backtest.py --checkpoint backtest

# Step 3: regime tags
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1_survivorship_10y

# Step 4: S&P 500 membership table
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint membership

# Step 5: S&P 500 backtest (45-90 min, ~900 tickers x 20 years of prices)
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint backtest
```

**Force re-download all price data (clears cache):**

```bash
docker compose exec dashboard python 52WeekHigh/run_backtest.py --checkpoint backtest --force-refresh
docker compose exec dashboard python 52WeekHigh/run_historic_backtest.py --checkpoint backtest --force-refresh
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1 --force-refresh
docker compose exec dashboard python 52WeekHigh/run_regime_analysis.py --checkpoint tag --strategy-version 52wh_v1_survivorship_10y --force-refresh
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint membership --force-refresh
docker compose exec dashboard python SP500/run_sp500_backtest.py --checkpoint backtest --force-refresh
```

**Deploy latest code after `git pull`:**

```bash
git pull
docker compose up --build -d
docker compose ps   # verify all three services are running
```
""")
