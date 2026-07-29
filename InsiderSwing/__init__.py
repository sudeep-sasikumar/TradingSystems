"""
InsiderSwing — Insider-Trade Cluster Swing System.

WHAT THIS IS
------------
This module trades on SEC **Form 4** filings: legally required *public*
disclosures that corporate insiders (Section 16 officers, directors, and
10%-owners) must file within 2 business days of transacting in their own
company's stock.  Every input to this system is a public document published on
SEC EDGAR.

This is the "insider trading anomaly" documented in the academic literature —
Seyhun (1986, 1998), Lakonishok & Lee (2001), Jeng/Metrick/Zeckhauser (2003),
Cohen/Malloy/Pomorski (2012).  The finding is that *open-market cluster
purchases* by insiders predict abnormal returns over the following 3–6 months.

WHAT THIS IS NOT
----------------
This has nothing to do with trading on material non-public information.  The
module name is a description of the *data source* (insider filings), not of a
technique.  No non-public information is used, sourced, or inferred anywhere in
this package.

POINT-IN-TIME DISCIPLINE (the single most important rule here)
--------------------------------------------------------------
Form 4 rows carry two dates:

    transactionDate — when the insider actually traded (NOT knowable to us then)
    filingDate      — when the disclosure hit EDGAR (when it became public)

Every signal, score, and backtest decision in this package keys off
``filing_date``.  ``transaction_date`` is stored for analysis and reporting
only.  Using ``transaction_date`` for signal timing is the classic lookahead
bug in insider-signal research and it is structurally prevented here — see
``InsiderSwing/scoring.py`` and ``InsiderSwing/backtest/engine.py``.

Package layout
--------------
    config.py       — all tunables, env-driven
    models.py       — SQLAlchemy tables (own SQLite DB)
    db.py           — engine/session for data/insider_swing.db
    sources/        — data ingestion (FMP primary, SEC EDGAR fallback)
    universe.py     — point-in-time universe + liquidity filter
    filters.py      — Form 4 noise classification
    scoring.py      — 0–100 conviction score with component breakdown
    technical.py    — technical confirmation triggers
    risk.py         — position sizing, stops, slippage
    backtest/       — engine, walk-forward, metrics
    report.py       — saved markdown/HTML report per run
    scanner.py      — live daily scan
    run_insider.py  — CLI entry point

Import pattern (matches existing 52WeekHigh/ and 52WeekHighUS/ conventions):
each entry point puts both the project root and this directory on sys.path,
then uses flat absolute imports.
"""

__all__ = ["STRATEGY_VERSION"]

STRATEGY_VERSION = "insider_v1"
