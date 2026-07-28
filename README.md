# TradingSystems

A momentum trading system for Indian equities (NSE / Nifty 500).

## Phases

| Phase | Strategy | Status |
|---|---|---|
| 1 | 52-Week High Breakout (`52WeekHigh/`) | In progress |
| — | S&P 500 52-Week High (`SP500/`, `52WeekHighUS/`) | In progress |
| — | Insider-Trade Cluster Swing (`InsiderSwing/`) | In progress |
| 2+ | Additional variants | Planned |

## Phase 1 — 52-Week High Strategy

### What it does
Triggers a trade signal when a stock's price reaches a new 252-trading-day high.
Trailing stop-loss at 20% below peak price. Exit on daily close breach of stop.

### Key design choices
- **Backtest**: uses close-based 252-day benchmark; all signals and exits on daily close.
- **Live scanner**: provisional alert on intraday-high-based benchmark; confirmed on EOD close.
- **Interactive Telegram alerts**: [Accept] / [Reject] inline buttons for each entry signal.
- **Streamlit dashboard**: backtest results, open positions, pending signals, activity log.

---

## Known Limitations (read before interpreting any results)

### Survivorship Bias
The Nifty 500 universe used for the backtest is the **current** constituent list,
fetched live from NSE archives. Stocks that were added to or removed from the index
between January 2022 and today are not accurately reflected. This creates survivorship
bias: the backtest over-represents stocks that survived and performed well enough to
remain in the index. Results will be more optimistic than a truly point-in-time
historical universe would show.

This is a known, accepted limitation. It is not papered over.

### No Transaction Costs
Backtest results do not include brokerage commissions, STT (Securities Transaction Tax),
exchange fees, GST, SEBI charges, or any other execution costs. Real-world returns
will be lower.

### No Slippage
All backtest entries and exits assume execution at the exact close price on signal day.
In practice, orders fill at different prices, especially for less-liquid names in the
Nifty 500 tail.

### Equal-Weight, Unlimited Capital
The backtest uses equal-weight position sizing with no capital constraint and no
position limit applied retroactively. This is a raw signal-quality test — it is not
a realistic portfolio simulation. The position limit (`MAX_CONCURRENT_POSITIONS`) is
a live-trading control only and is not applied to historical results.

### Single Historical Window
Backtest results cover January 2022 to the present. This window includes a specific
market regime (post-COVID recovery rally, then interest rate cycle, etc.). Performance
in this window is not predictive of future performance in different regimes.

---

## Insider-Trade Cluster Swing System (`InsiderSwing/`)

### What this is — and what it is not

This module trades on **SEC Form 4 filings**: legally required *public* disclosures
that corporate insiders (Section 16 officers, directors, and 10% owners) must file
within **2 business days** of transacting in their own company's stock.

It implements the **insider-purchase anomaly** documented in the academic
literature — Seyhun (1986, 1998), Lakonishok & Lee (2001), Jeng/Metrick/Zeckhauser
(2003), Cohen/Malloy/Pomorski (2012). The replicated finding is that *open-market
cluster purchases* by insiders precede abnormal returns over the following 3–6
months.

**This has nothing to do with trading on material non-public information.** The
module name describes the *data source*, not a technique. Every input is a public
SEC document retrieved from EDGAR. No non-public information is used, sourced, or
inferred anywhere in the package.

### The single most important rule: point-in-time discipline

Every Form 4 row carries two dates:

| Field | Meaning | Usable as a signal? |
|---|---|---|
| `transaction_date` | when the insider actually traded | **No** — unknowable at the time |
| `filing_date` | when the disclosure hit EDGAR | **Yes** — this is when it became public |

Every score, signal, and backtest decision keys on `filing_date`. `transaction_date`
is stored for analysis only. Using it for signal timing is the classic lookahead bug
in insider-signal research, and the schema, the scoring layer, and the engine are
all built to make it structurally impossible.

Entries are additionally scheduled `INSIDER_SIGNAL_LAG_DAYS` (default 1) trading
sessions *after* the filing date and filled at the following open — filings accepted
after 17:30 ET are not tradeable that day.

### How it works

```
Form 4 (EDGAR)  →  noise filter  →  conviction score  →  technical trigger  →  sized entry
```

1. **Ingest** — SEC EDGAR is the primary source (free, complete, and the only one
   carrying the Rule 10b5-1 checkbox). FMP is supported behind the same interface
   but its per-symbol history endpoints are plan-gated on lower tiers.
2. **Noise filter** — the raw feed is ~95% mechanical. Awards (`A`), option
   exercises (`M`), RSU tax withholding (`F`), gifts (`G`), dispositions to the
   issuer (`D`), derivatives, $0-price rows and confirmed 10b5-1 plan trades are all
   discarded. Only cash open-market purchases (`P`) and sales (`S`) survive.
   Discarded rows are **kept in the database** so the breakdown is auditable.
3. **Conviction score (0–100)** — cluster count (weighted heaviest), role seniority,
   buy size versus that insider's *own* trailing 2-year average, and novelty
   (first buy in 12 months). Down-weighted when earnings fall inside 2 weeks.
   Cluster *selling* is a caution filter, not a mirrored short signal — the
   literature does not support insider selling as a symmetric predictor.
4. **Technical confirmation** — the published edge plays out over 3–6 months, which
   is the wrong horizon for a swing trade. A signal must be confirmed by a DMA
   reclaim, range breakout, or RSI reset within `INSIDER_CONFIRMATION_WINDOW_DAYS`
   trading days, or it expires (and the expiry is logged).
5. **Risk** — ATR-based volatility sizing, stop at the tighter of an ATR multiple or
   the recent swing low, R-multiple target or trailing stop, and a hard time stop.

### Running it

Order matters — the data layer must exist before anything downstream means anything.

```bash
# 0. one-time: set INSIDER_SEC_USER_AGENT in .env (SEC blocks anonymous requests)

python InsiderSwing/run_insider.py --checkpoint universe
python InsiderSwing/run_insider.py --checkpoint ingest --start 2014-01-01
python InsiderSwing/run_insider.py --checkpoint score
python InsiderSwing/run_insider.py --checkpoint backtest --label baseline
python InsiderSwing/run_insider.py --checkpoint sweep
```

**Ingest from ~2 years before your backtest start.** The relative-size score
component compares each buy against that insider's own trailing 2-year average; with
less history it falls back to half credit, which weakens the signal for no reason.

A first full-universe ingest fetches hundreds of thousands of documents from EDGAR
at ~8 req/s and takes hours. Everything is cached to disk gzipped and the DB
uniqueness constraints make re-runs idempotent, so it is resumable and a repeat run
costs nothing.

### Re-running the parameter stability sweep

```bash
python InsiderSwing/run_insider.py --checkpoint sweep --start 2016-01-01
```

Sweeps conviction threshold × cluster window × confirmation window. **The output is
not "which cell is best."** The question it answers is whether performance is smooth
across neighbouring cells (a plateau — the effect is robust) or concentrated in one
isolated cell (a spike — the setting was fitted to noise). `assess_stability()`
prints that verdict in those words, including "OVERFIT RISK" when it applies.

Adjust the grid in `InsiderSwing/backtest/walkforward.py`
(`DEFAULT_THRESHOLDS`, `DEFAULT_CLUSTER_WINDOWS`, `DEFAULT_CONFIRMATION_WINDOWS`).

### Known limitations (read before interpreting any result)

**Filing lag.** Up to 2 business days pass between the trade and the disclosure, and
this system can only act on the disclosure. That lag is modelled, but the first
mover has always already moved.

**Small samples.** Qualifying cluster events are genuinely rare. Arms and buckets
below `INSIDER_THIN_SAMPLE` (default 30) trades are flagged, and no conclusion
should be drawn from them regardless of how good the number looks. All Sharpe and
CAGR figures are reported as bootstrap confidence intervals, not point estimates.

**Earnings confound is approximate.** The calendar source returns *actual reported*
earnings dates, not the date scheduled at filing time. The flag is therefore used
only as a score multiplier and a reporting bucket — never as an entry or exit
condition — so the approximation cannot leak into simulated returns.

**Rule 10b5-1 coverage is partial.** The Form 4 checkbox only exists on filings from
2022 onward; earlier filings are caught only when a footnote mentions the plan.
Pre-2022 rows with unknown plan status are kept by default (dropping them would
delete most of the history), so some mechanical plan trades survive the filter in
the older part of the sample. Set `INSIDER_EXCLUDE_UNKNOWN_10B5_1=1` to drop them.

**Price coverage on delisted names.** The universe is point-in-time correct and
includes companies later removed from the index, but price history for some is no
longer retrievable. Those trades cannot be simulated, so a residual survivorship
bias remains in the *price* layer even though the *membership* layer is corrected.
The exact coverage percentage is printed in every report.

**Market-cap segmentation uses current shares outstanding.** No historical share
count is available; the bucket drifts for companies with heavy buybacks. Reporting
only, never used for entry.

**Slippage is a model, not a measurement.** Square-root market impact
(`base_spread + coef × √participation`) is more honest than a flat bps assumption
for small caps, but real fills in thin names can be worse.

**The extra universe CSV is not survivorship corrected.** If you supply
`data/cache/insider_extra_universe.csv`, those names have no point-in-time
membership history and the report says so explicitly rather than blending them in.

### Reading the backtest report

Three arms are always reported together, and reporting only the last one would be
meaningless:

| Arm | What it isolates |
|---|---|
| `insider_only` | The raw factor — enter after the filing lag, no price condition |
| `tech_only` | The timing rule's base rate, with **no** insider filter |
| `combined` | Insider signal **and** technical confirmation — the live strategy |

If `combined` beats buy-and-hold but not `tech_only`, the insider data is
contributing nothing. If it loses to `insider_only`, the timing overlay is
destroying edge rather than adding it. Both are real possible outcomes and the
report states them plainly — the same way the Nifty and S&P 500 breakout systems
were reported as marginal after costs rather than tuned until they looked good.

Reports are written to `data/reports/insider/` as Markdown, HTML and JSON, and
rendered inline in the dashboard's **Insider Swing** tab.

---

## Deployment

### Prerequisites
- Docker + Docker Compose
- A Telegram bot token (create via @BotFather)
- Your Telegram chat ID

### Quick Start (Hostinger VPS or any Linux server)

```bash
git clone https://github.com/sudeep-sasikumar/TradingSystems.git
cd TradingSystems
cp .env.example .env
# Edit .env with your BOT_TOKEN, CHAT_ID, MAX_CONCURRENT_POSITIONS
nano .env

docker compose up -d
```

Dashboard available at `http://<your-server-ip>:8502`

### Local Development (Windows)

```powershell
# One-time setup
D:\Python313\python.exe -m venv "E:\Trading Systems\venv"
cd "E:\Trading Systems"
venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run backtest
python 52WeekHigh\run_backtest.py --checkpoint universe
python 52WeekHigh\run_backtest.py --checkpoint backtest

# Run dashboard
streamlit run dashboard\app.py
```

---

## Project Structure

```
52WeekHigh/          Phase 1 strategy — backtest, scanner, bot
dashboard/           Shared Streamlit dashboard (all phases)
shared/              Shared DB models and utilities
data/                Local data (SQLite DB + NSE cache) — not committed
docker/              Dockerfiles for each service
```
