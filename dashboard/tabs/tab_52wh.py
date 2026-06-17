"""
52-Week High System — Dashboard Tab (STUB — Checkpoint 3)

Reads directly from the shared SQLite DB (no separate data pipeline).
All sections below will be implemented after the backtest engine is built
and validated (Checkpoint 2), and the live scanner is wired up (Checkpoint 4).
"""
import streamlit as st


def render_tab() -> None:
    st.header("52-Week High Momentum System")

    # ── Backtest caveat (always visible, always honest) ───────────────────────
    st.warning(
        "**Backtest assumptions**: Equal-weight, unlimited capital, no position "
        "cap applied retroactively. No transaction costs, slippage, STT, or "
        "brokerage modeled. Universe = current Nifty 500 (survivorship bias — "
        "see README). **This is an illustrative simulation, not a real portfolio.**"
    )

    # ── Placeholder sections ───────────────────────────────────────────────────
    st.info(
        "Backtest not yet run. Execute:\n\n"
        "```\nvenv\\Scripts\\python.exe 52WeekHigh\\run_backtest.py --checkpoint backtest\n```\n\n"
        "to populate this view."
    )

    # TODO (Checkpoint 3): Implement the sections below after backtest validated
    #
    # SECTION 1 — Backtest: Full-period summary metrics
    # SECTION 2 — Backtest: Year-by-year breakdown table
    #   - Columns: year, trades, win_rate, avg_return, median_return,
    #              avg_holding_days, best_trade, worst_trade, cumulative_return
    #   - Label 2026 row as "2026 (YTD — partial)"
    # SECTION 3 — Backtest: Equity curve chart
    #   - Label: "Illustrative, equal-weight, no capital constraints
    #             — not a real portfolio simulation"
    # SECTION 4 — Backtest: Full sortable/filterable trade log
    # SECTION 5 — Live: Open positions (entry price, current price, trailing stop,
    #              unrealized return %, days held)
    # SECTION 6 — Live: Pending signals (awaiting accept/reject, time to expiry)
    # SECTION 7 — Live: Recent activity log (accepted / rejected / expired /
    #              stopped-out, most recent first)
