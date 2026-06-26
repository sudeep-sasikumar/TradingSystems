# Project Status — TradingSystems

> **For new sessions**: Read `CLAUDE.md` first (full spec + design decisions), then this file for build state.

---

## Current State

- **Date last updated**: 2026-06-26
- **Active phase**: Phase 1 (Nifty complete) + S&P 500 system (CP-S7 complete) + Freshness (Nifty + SP500 complete) + **52WeekHighUS US Breakout System (Session 1 complete)**
- **Completed checkpoints**: 0, 1, 2, 3, 4, 5, 6, 7, 8, 8b, 8c + CP-S1–S7 + Freshness + **52WHU-S1**
- **Next (52WeekHighUS)**: Session 2 — backtest engine (3-version comparison, Part I of spec)
- **Next action (VPS)**: git pull → docker compose up --build -d → click "Run Everything (Steps 1–9)" in Setup & Admin tab

---

## Checkpoint Log

### ✅ Checkpoint 0 — Scaffolding & Design Decisions
All design questions confirmed with user. Full project structure created.

**Files created:**
- `CLAUDE.md` — full project guide (read this first in new sessions)
- `PROJECT_STATUS.md` — this file
- `.gitignore`, `.env.example`, `requirements.txt`, `README.md`
- `shared/__init__.py`, `shared/models.py`, `shared/db.py`
- `52WeekHigh/backtest/__init__.py`, `52WeekHigh/backtest/universe.py`
- `52WeekHigh/run_backtest.py`
- `52WeekHigh/scanner/__init__.py`, `52WeekHigh/scanner/scanner.py` (stub)
- `52WeekHigh/bot/__init__.py`, `52WeekHigh/bot/bot.py` (stub)
- `dashboard/app.py`, `dashboard/tabs/__init__.py`, `dashboard/tabs/tab_52wh.py` (stub)
- `docker/Dockerfile.scanner`, `docker/Dockerfile.bot`, `docker/Dockerfile.dashboard` (stubs)
- `docker-compose.yml` (stub — flesh out at Checkpoint 6)
- venv created at `E:\Trading Systems\venv` (base: `D:\Python313`)

**Design decisions confirmed:**
- Entry benchmark: intraday-high-based for live scanner; close-based for backtest
- Entry price: signal price at scan time (not when Accept pressed)
- PENDING signals suppress re-entry for same stock
- Port: 8502

---

### ✅ Checkpoint 1 — Universe Fetch + Caching
504 Nifty 500 tickers fetched, cached at `data/cache/nifty500.csv`. User confirmed.

---

### ✅ Checkpoint 2 — Backtest Engine
Full backtest run: 1,052 closed trades + 152 still open (not stopped out).
Results saved to `data/trading.db`.

**Corporate action investigation (blocker resolved before CP3):**
- Ran `diag_corp_actions.py` — full diagnostic across all 1,052 trades
- 4 confirmed artifacts: CGCL/GPIL/MOTILALOFS (splits, yfinance wrong ex-date = Jan 1, 2024)
  and VEDL (demerger, Apr 30, 2026 — yfinance has no data)
- All other large moves (Jun 4, 2024 India election; Adani-Hindenburg; IIFL RBI action) confirmed genuine
- `KNOWN_ARTIFACTS` set added to `dashboard/tabs/tab_52wh.py`; both original and cleaned stats
  shown side-by-side in dashboard

---

### ✅ Checkpoint 3 — Dashboard (Backtest View Only)
Streamlit dashboard live at http://localhost:8502.

**Files implemented:**
- `dashboard/tabs/tab_52wh.py` — full implementation
- `dashboard/app.py` — unchanged (already had correct shell)

**Sections delivered:**
1. Full-period combined stats — two columns: all trades vs. artifacts excluded
2. Year-by-year table with toggle to exclude artifacts
3. Equity curve — monthly bar chart + cumulative line (dual y-axis, labeled "Illustrative…")
4. Trade log — filterable by year / win/loss / artifact flag, sortable
5. Open positions table (152 backtest trades still running)
6-7. Live trading placeholders (wired at Checkpoint 4+)

---

### ✅ Checkpoint 4 — Live Scanner (No Telegram Yet)

**File delivered**: `52WeekHigh/scanner/scanner.py`

**What it does:**
- APScheduler with two jobs (all UTC Mon–Fri):
  - Hourly scan: 03:00–10:00 UTC (`CronTrigger`), guarded internally to 09:15–15:30 IST
  - EOD pass: 10:05 UTC = 15:35 IST
- Daily OHLCV cache per ticker in `data/cache/highs/` (HIGH) and `data/cache/prices/` (CLOSE), 23h TTL
- Signal benchmark: `high.shift(1).rolling(252).max()` — intraday price vs. 252-day HIGH benchmark
- EOD close confirmation: `close.shift(1).rolling(252).max()` — determines "eod_confirmed" vs "provisional_unconfirmed"
- Re-entry suppression: queries open live trades + pending signals; REJECTED/EXPIRED don't suppress
- Position cap: cap-reached note appended to log; signals never silently suppressed
- Trailing stop EOD check: CLOSE ≤ max_close × 0.80 → marks trade closed with exit_reason='trailing_stop'
- `_notify_*` functions are console-logging stubs (Telegram wired at CP5)
- `--run-now` and `--eod-now` flags for standalone testing

**Standalone test commands:**
```powershell
# Single scan (bypasses market-hours guard)
venv\Scripts\python.exe 52WeekHigh\scanner\scanner.py --run-now
# Single EOD pass
venv\Scripts\python.exe 52WeekHigh\scanner\scanner.py --eod-now
```

**Risk flag**: yfinance intraday reliability for 500 tickers — untested at scale. Retry/backoff built in (tenacity, 3 attempts, exponential back-off). Escalate to user if failure rate is unacceptable (upgrade path: Zerodha Kite Connect).

---

### ✅ Checkpoint 5 — Telegram Bot (Accept/Reject)

**File delivered**: `52WeekHigh/bot/bot.py`

**Architecture**: PTB v21 Application with long-polling; PTB JobQueue for background jobs; bot polls DB for signals — scanner never calls Telegram directly.

**DB migration on startup**: adds `eod_notified INTEGER` to signals, `exit_notified INTEGER` to trades (ALTER TABLE, idempotent).

**Background jobs (PTB JobQueue):**
- `poll_signals` every 60s — sends unsent pending signals (telegram_message_id IS NULL) with [✅ Accept] [❌ Reject] inline keyboard
- `poll_eod` every 300s — sends EOD confirmation follow-ups for eod_confirmed / provisional_unconfirmed signals
- `poll_exits` every 300s — sends exit notifications for closed live trades (exit_notified=0)
- `poll_expiry` every 300s — expires signals >24h pending, sends expiry note
- `eod_summary` daily at 10:15 UTC Mon–Fri — signals today, accepted/rejected/expired, open positions with trailing stops

**Callback handler (Accept/Reject):**
- `accept:{id}` → Signal.status='accepted', creates Trade(source='live', entry_price=signal_price, trailing_stop=price×0.80)
- `reject:{id}` → Signal.status='rejected' (stock eligible for future signals)
- Both edit the original Telegram message to show result

**Commands**: `/start`, `/status` (open positions + today's pending), `/positions` (full open list with stops)

**Message design per spec:**
- "Signal price: ₹XXX — actual fill price may differ."
- Cap warning: "CAP REACHED — X/X positions open (signal still recorded)" — never suppressed
- All messages use MarkdownV2 with proper escaping

**Also in this checkpoint**: Fixed `datetime.utcnow()` deprecation in `scanner.py` → `datetime.now(timezone.utc)`; added `timezone` to scanner's imports.

---

### ✅ Checkpoint 6 — Docker + Deployment

**Files delivered**:
- `docker/Dockerfile.scanner` — python:3.13-slim, PYTHONUNBUFFERED, requirements layer cached
- `docker/Dockerfile.bot` — same base, long-poll CMD
- `docker/Dockerfile.dashboard` — adds EXPOSE 8502, HEALTHCHECK on `/_stcore/health`, `--server.headless=true`
- `docker-compose.yml` — all three services with bind mount `./data:/app/data`, log rotation (10MB×3), healthcheck on dashboard
- `.dockerignore` — excludes venv/, data/, .env, __pycache__, diag script

**Restart policies**:
- `bot`: `restart: always` — must stay alive for Telegram callbacks
- `scanner`: `restart: unless-stopped` — stateless, safe to restart
- `dashboard`: `restart: unless-stopped`

**Also in this checkpoint**:
- Signal type label renamed: "Intraday Provisional" → "Provisional" in Telegram messages and scanner logs
- `pyarrow>=14.0.0` added to requirements.txt (was transitively installed but not pinned)

**Deploy to Hostinger VPS (one-time setup)**:
```bash
# On VPS
git clone https://github.com/sudeep-sasikumar/TradingSystems.git
cd TradingSystems
cp .env.example .env        # fill in BOT_TOKEN, CHAT_ID, etc.
mkdir -p data/cache
docker compose up --build -d
docker compose ps           # verify all three services running
# Then open http://<VPS_IP>:8502 → Setup & Admin tab → Run All Steps
```

**Day-to-day (deploy latest code after CI builds new images)**:
```bash
docker compose pull
docker compose up -d
docker compose ps
```

**Tail logs**:
```bash
docker compose logs -f bot        # Telegram bot
docker compose logs -f scanner    # hourly scanner
docker compose logs -f dashboard  # Streamlit
```

---

---

### ✅ Checkpoint 7 — Survivorship-Corrected Historic Backtest (complete)

**Goal**: Run 52wk-high strategy over ~7-year window (Oct 2019 – present) using
ACTUAL historical Nifty 500 membership, not today's list projected backward.
Tagged `strategy_version = "52wh_v1_survivorship_10y"` per user spec.

**Files created/modified:**
- `shared/models.py` — added `IndexMembership` table
- `52WeekHigh/historic_universe/build_membership.py` — baseline CSV + PDF parser + reconstruction
- `52WeekHigh/historic_universe/historic_engine.py` — extended backtest engine (time-varying universe)
- `52WeekHigh/run_historic_backtest.py` — CLI entry point
- `data/reconstitution_pdfs/nifty500_baseline_20200725.csv` — July 2020 Wayback Machine snapshot (501 stocks)
- `data/reconstitution_pdfs/*.pdf` — 11 PDFs downloaded (NOT committed to git)

**Membership table:** 654 intervals, 153 exact dates (from 7 reconstitution PDFs), 501 inferred
**Missing reconstitutions (data gaps):** Sep 2020, Mar 2021, Sep 2021, Sep 2023, Sep 2024
(stocks in these periods treated as continuously present since baseline — mild survivorship bias remains)

**Backtest results (Oct 2019 – Jun 2026, strategy_version=52wh_v1_survivorship_10y):**

| Metric | Value |
|---|---|
| Closed trades | 1,725 |
| Win rate | 49.9% |
| Avg return / trade | +21.63% |
| Median return | -0.2% |
| Avg holding days | 228.5 |
| Best trade | +660.33% |
| Worst trade | -67.63% |

Year-by-year win rates: 2020 65.7%, 2021 45.9%, 2022 42.9%, 2023 73.2%, 2024 35.4%, 2025 23.4%

**Phase 3 — Dashboard (not started)**: Add new tab section showing year-by-year breakdown
with constituent count, equity curve, and side-by-side comparison vs original 2022-2026 results.
Do NOT start until user confirms data quality is acceptable.

---

### ✅ Checkpoint 8 — Regime Tagging and Analysis (complete)

**Goal**: Tag every trade in the survivorship-corrected backtest with market + sector regime
indicators as of its entry date; run cross-tab analysis to identify which regime environments
produce the best 52-week-high momentum outcomes.

**Files created/modified:**
- `shared/models.py` — added `TradeRegimeTag` table (FK to trades.id; never modifies trade outcomes)
- `52WeekHigh/analysis/__init__.py` — package marker
- `52WeekHigh/analysis/regime_data.py` — download + cache index data; compute 200-DMA + 6M quintile
- `52WeekHigh/analysis/regime_tagger.py` — tag all trades, write to trade_regime_tags
- `52WeekHigh/analysis/regime_analysis.py` — cross-tab analysis, top/bottom combinations
- `52WeekHigh/run_regime_analysis.py` — CLI: `--checkpoint tag|analyze|all`
- `dashboard/tabs/tab_regime.py` — new "Regime Analysis" dashboard tab
- `dashboard/app.py` — added third tab

**Market index used**: `^CRSLDX` (Nifty 500 on Yahoo Finance, 2079 rows from 2018-01-01)
**Sectoral indices** (all 10/10 available): Auto, Bank, IT, Pharma, FMCG, Metal, Realty, Media, Energy, PSU Bank
**Synthetic baskets**: 14 equal-weighted industry baskets (from baseline CSV Industry col, ≥10 stocks)

**Coverage (1,725 closed trades):**
| Regime Layer | Coverage |
|---|---|
| Market (^CRSLDX) | 1,725 / 1,725 (100%) |
| Official sector | 471 / 1,725 (27%) — 7 industries map to available NSE sectoral index |
| Synthetic basket | 1,501 / 1,725 (87%) |

**Key findings (data-only, no editorialising):**
| Regime | Trades | Win% | Avg Ret% |
|---|---|---|---|
| Market BELOW 200-DMA | 188 | 60.6% | +35.93% |
| Market ABOVE 200-DMA | 1,537 | 48.6% | +19.88% |
| Market strong_downtrend (6M Q) | 191 | 64.9% | +44.04% |
| Market flat (6M Q) | 254 | 33.9% | +12.42% |

**Top pair (≥30 trades)**: `market_6m_quintile=strong_downtrend` + `official_vs_200dma=above_200dma` → 57 trades, 78.9% win, +73.31% avg return
**Top triple**: `market_6m_quintile=strong_downtrend` + `official_vs_200dma=above_200dma` + `synthetic_vs_200dma=above_200dma` → 54 trades, 81.5% win, +75.84% avg return

**To run:**
```powershell
# Tag all trades (downloads index data on first run, cached thereafter)
venv\Scripts\python.exe 52WeekHigh\run_regime_analysis.py --checkpoint tag

# Print full analysis tables to stdout
venv\Scripts\python.exe 52WeekHigh\run_regime_analysis.py --checkpoint analyze

# Force re-download of index data
venv\Scripts\python.exe 52WeekHigh\run_regime_analysis.py --checkpoint tag --force-refresh
```

---

---

### ✅ Checkpoint 8b — Live Conviction Tier + Capital Simulation Dashboard (complete)

**Goal**: Attach a conviction tier to every live scanner signal, surface it in Telegram
alerts and the dashboard, and add an Rs.1,000/trade simulation view to the Regime Analysis tab.

**Files created/modified:**
- `shared/models.py` — added `conviction_tier TEXT` and `regime_score INTEGER` to Signal table
- `52WeekHigh/analysis/conviction.py` — new: `get_signal_conviction(ticker, signal_date)` using
  cached market regime + synthetic basket data; 23h cache TTL
- `52WeekHigh/scanner/scanner.py` — calls conviction module per new signal; stores tier + score;
  logs conviction tier in `_notify_signal()`
- `52WeekHigh/bot/bot.py` — DB migration for new columns; `_fmt_conviction()` adds tier-specific
  Telegram text to every signal message; advisory only, never blocks
- `dashboard/tabs/tab_52wh.py` — live positions and pending signals show conviction tier column;
  running acceptance tally (generated / accepted / rejected / expired per tier)
- `dashboard/tabs/tab_regime.py` — Explorer conviction tier filter; new 5th inner tab
  "Rs.1,000 Simulation" with overall / year-by-year / quintile / tier / score breakdowns

**Conviction tier rules (data-supported, 3 tiers only):**

| Tier | Condition | Backtest avg return |
|---|---|---|
| HIGH | market 6M in bottom-2 quintiles AND synthetic basket above 200-DMA | +40–60% (dataset-dependent) |
| AVOID | market 6M in strong_uptrend quintile | +9% (original) / +22% (survivorship-corrected) |
| STANDARD | everything else | +24–26% |

**AVOID is advisory-only**: the "AVOID" label is strongest in the original 2022-present
backtest (+9%). In the longer survivorship-corrected 2019 dataset, strong_uptrend actually
performs at 22% — roughly average. AVOID signals are still sent to Telegram with a caution
note; the user makes the final Accept/Reject call.

**DB migration**: handled in `bot.py._migrate_db()` — idempotent ALTER TABLE statements.
New databases get columns from `models.py` schema directly.

**Finer scoring note**: A numeric score beyond 3 tiers is not justified by current sample
sizes. Revisit when 12-18 months of live signals are available with ≥50 trades per tier.
See comment in `analysis/conviction.py` and `shared/models.py`.

**Capital simulation results** (Rs.1,000/trade, illustrative):

| Dataset | Trades | Deployed | P&L | Return% |
|---|---|---|---|---|
| 2022-present (original) | 1,052 | Rs.10.52L | Rs.2.87L | 27.3% |
| 2019-present (corrected) | 1,725 | Rs.17.25L | Rs.3.73L | 21.6% |

HIGH CONVICTION (score ≥2): 47.0% / 31.2% return in the two datasets respectively.

---

---

### ✅ Checkpoint 8c — VPS Deployment Fix + Setup Tab (complete)

**Problem**: `docker-compose.yml` used `image: ghcr.io/sudeep-sasikumar/tradingsystems-*:latest`
with no CI pipeline to build those images. `git pull` on the VPS pulled new code but never
rebuilt containers, so the VPS always ran stale images. Also used named Docker volume which
masked committed PDF files inside containers.

**Fix**:
- `docker-compose.yml`: replaced all `image:` pulls with `build: context: . / dockerfile: docker/Dockerfile.*`.
  Now `docker compose up --build -d` always builds from source. Named volume replaced with
  `./data:/app/data` bind mount — committed PDFs and baseline CSV accessible immediately.
- `dashboard/tabs/tab_setup.py` (new): Setup & Admin tab with:
  - Live DB status metrics (trade counts, regime tag counts, membership rows)
  - "Refresh Status" button
  - Step 1: Run original backtest (2022-present, 52wh_v1)
  - Step 2: Run survivorship-corrected backtest (membership table → historic backtest)
  - Step 3: Tag regimes for both strategy versions
  - "Run All Steps" button (primary, sequential, ~30-60 min first run)
  - Advanced expander with CLI equivalents for unattended VPS setup
- `dashboard/app.py`: added "Setup & Admin" as 4th tab
- `52WeekHigh/analysis/capital_simulation.py`: committed (was untracked)

**VPS update command** (after CI finishes building new images):
```bash
docker compose pull && docker compose up -d
# Then: open http://<VPS_IP>:8502 → Setup & Admin tab → Run All Steps (if fresh VPS)
```

**Volume note**: docker-compose.yml now uses `./data:/app/data` bind mount instead of the
prior named volume `trading_data`. This makes committed PDFs/baseline CSV accessible inside
containers. If VPS had data in the named volume, migrate with:
```bash
docker run --rm -v trading_data:/from -v $(pwd)/data:/to alpine sh -c "cp -r /from/. /to/"
```

---

---

### ✅ CP-S1 — S&P 500 Research (complete, previous session)
Time-varying membership sourced from fja05680/sp500 (GitHub, Wikipedia-sourced change history).
Two files: `sp500_ticker_start_end.csv` (1,202 unique tickers, 1996–2026-06-02) and
`sp500_changes_since_2019.csv` (event log). COVERAGE_START = 2006-01-01, LOOKBACK_START = 2005-01-01.

---

### ✅ CP-S2 — S&P 500 Constituent Ingestion (complete, previous session)
`shared/models.py`: added `Sp500Membership` table.
`SP500/backtest/universe.py`: downloads + parses both fja05680 CSVs, merges, persists.
1,255 membership intervals, 1,202 unique tickers, 503 current members.
CLI: `python SP500/run_sp500_backtest.py --checkpoint membership`

---

### ✅ CP-S3 — S&P 500 Backtest (complete, previous session)
`SP500/backtest/engine.py`: full simulation with time-varying membership + delisting detection.
`SP500/run_sp500_backtest.py`: CLI entry point.
Results on VPS (per user-provided CSV): 3,707 total (3,460 closed + 247 open), 46.2% win rate,
avg return +13.66% per closed trade, top winner V +741% (2011-2020).
`dashboard/tabs/tab_sp500.py`: new S&P 500 tab (Backtest Results / Delisted Exits / vs Nifty 500).
`dashboard/app.py`: 5-tab layout (Nifty Live / Nifty Historic / Nifty Regime / S&P 500 / Setup).

---

### ✅ CP-S4 — S&P 500 Regime Analysis (complete, 2026-06-19)
`shared/models.py`: added `Sp500MarketRegime` table (daily ^GSPC + ^VIX regime signals).
`SP500/backtest/regime.py`: downloads ^GSPC + ^VIX from 2004-06-01, computes 200-DMA regime
  (bull/bear) and VIX tier (calm/elevated/stressed), saves 5,146 rows to sp500_market_regime.
`SP500/run_sp500_backtest.py`: added `--checkpoint regime` (CP-S4).
`dashboard/tabs/tab_sp500.py`: new "Regime Analysis" 4th sub-tab with:
  - 200-DMA breakdown table + win rate / avg return bar charts
  - VIX tier breakdown table + bar charts
  - Combined 200-DMA × VIX matrix (trades | win% | avg%)
  - ^GSPC vs 200-DMA price chart with bear shading
  - ^VIX history chart with tier threshold lines
`dashboard/tabs/tab_setup.py`: Step 6 button for S&P 500 regime; SP500 Regime Days metric.

**VPS: Run Setup & Admin → Step 6 (< 2 min) to populate regime data.**

---

---

### ✅ CP-S5 — S&P 500 Daily EOD Scanner (complete, 2026-06-19)

**Files created:**
- `SP500/scanner/__init__.py` — package marker
- `SP500/scanner/scanner.py` — S&P 500 EOD daily scanner

**Files modified:**
- `52WeekHigh/bot/bot.py` — extended to handle both Nifty (52wh_v1) and S&P 500 (sp500_52wh_v1) signals
- `dashboard/tabs/tab_setup.py` — Step 7 test button + SP500 live metrics

**Scanner behavior:**
- Fires at **21:30 UTC Mon–Fri** (5:30 PM EDT / 4:30 PM EST)
- Universe: current S&P 500 members from `sp500_membership` (503 tickers)
- Signal: today's close > 252-day rolling max of prior closes (close-based only, always `eod_confirmed`)
- Price cache: `data/cache/prices_sp500_live/` (TTL 23h, downloads from 2024-01-01)
- Env: `SP500_MAX_CONCURRENT_POSITIONS` (default: 20)
- CLI: `python SP500/scanner/scanner.py --run-now` for immediate test

**Bot changes (strategy-aware, Nifty behavior unchanged):**
- `_fmt_signal()`: S&P 500 gets `[S&P500]` prefix and `$` currency; Nifty stays `₹`
- `_accept_signal()`: uses `sig.strategy_version` (was hard-coded to `52wh_v1`)
- `_job_poll_signals/exits/expiry`: handle all strategy versions
- `/positions` command: shows both Nifty (INR) and S&P 500 (USD) positions
- `_job_eod_summary`: shows signal counts for both strategies

**Flow:** Scanner writes `Signal(strategy_version='sp500_52wh_v1', status='pending')` to DB
→ Bot polls every 60s, sends Telegram with `[S&P500]` prefix and Accept/Reject buttons
→ Accept creates `Trade(strategy_version='sp500_52wh_v1', source='live')`
→ Scanner checks trailing stops at next 21:30 UTC run

**VPS deployment note:** The `sp500_scanner` Docker service is added in CP-S7.
Deploy with: `git pull && docker compose up --build -d`

---

---

### ✅ CP-S6 — S&P 500 Conviction Tiers (complete, 2026-06-19)

**Goal**: Attach a conviction tier to every S&P 500 live signal at scan time.
Advisory only — never blocks a signal; user makes the final Accept/Reject call.

**Files created:**
- `SP500/analysis/__init__.py` — package marker
- `SP500/analysis/sp500_conviction.py` — conviction lookup + tier assignment

**Files modified:**
- `SP500/scanner/scanner.py` — computes conviction once per scan (same regime for all signals that day);
  sets `Signal.conviction_tier` and `Signal.regime_score`
- `52WeekHigh/bot/bot.py` — added `_fmt_sp500_conviction()` formatter; wired into
  `_fmt_signal()` S&P 500 branch (shows below "Type:" line in Telegram message)

**Tier rules (calibrated from sp500_52wh_v1 backtest + sp500_market_regime, 2006–2026):**

| Tier | Condition | Score | Historical context |
|---|---|---|---|
| HIGH | Bull regime + calm VIX (< 20) | +2 | 2012 (70%/+60%), 2013 (74%/+40%), 2016 (66%/+25%) |
| AVOID | Bear regime + elevated or stressed VIX (>= 20) | <= -1 | 2008 (11%/-15%), 2022 (27%/-0.5%) |
| STANDARD | All other combinations | 0 or 1 | bull+elevated/stressed, bear+calm |

**Scoring:** market_score (bull=+1, bear=-1) + vix_score (calm=+1, stressed=-1, elevated=0)

**Regime source:** `sp500_market_regime` table, nearest prior date (handles weekends/holidays).
Falls back to STANDARD with a warning if no regime data found.

**Telegram signal example (HIGH conviction):**
```
[S&P500] 52-Week High Signal

AAPL — Apple Inc.
Signal price: $197.50 — actual fill price may differ.
Above 252-day high: $182.00 (+8.52%)
Type: EOD Confirmed
Conviction: HIGH
[Bull regime + calm VIX (<20) — best historical SP500 momentum environment]

Detected: 2026-06-19 21:30 UTC
```

---

### ✅ CP-S7 — S&P 500 Docker Service (complete, 2026-06-19)

**Goal**: Add `sp500_scanner` as a persistent Docker service so the scanner runs
automatically at 21:30 UTC Mon–Fri without manual intervention.

**Files created:**
- `docker/Dockerfile.sp500_scanner` — python:3.13-slim, same pattern as Dockerfile.scanner,
  CMD runs `SP500/scanner/scanner.py` (APScheduler handles the 21:30 UTC schedule)

**Files modified:**
- `docker-compose.yml` — added `sp500_scanner` service:
  - `restart: unless-stopped` (stateless, safe to restart)
  - Env: `SP500_MAX_CONCURRENT_POSITIONS` (default: 20)
  - Same `trading_data:/app/data` named volume as other services
  - Log rotation: json-file 10MB × 3
- `.env.example` — added `SP500_MAX_CONCURRENT_POSITIONS=20`

**Architecture after CP-S7 — 4 Docker services:**

| Service | Restart policy | Role |
|---|---|---|
| `scanner` | unless-stopped | Nifty 500 hourly intraday scanner |
| `bot` | always | Telegram bot — long-polling, handles all Accepts/Rejects |
| `dashboard` | unless-stopped | Streamlit on port 8502 |
| `sp500_scanner` | unless-stopped | S&P 500 EOD scanner, fires 21:30 UTC Mon–Fri |

**Deploy on VPS:**
```bash
git pull
docker compose up --build -d
docker compose ps          # verify 4 services running
docker compose logs -f sp500_scanner
```

**Test without waiting for schedule:**
```bash
docker compose exec sp500_scanner python SP500/scanner/scanner.py --run-now
```

---

### ✅ Freshness Factor Analysis (complete, 2026-06-19)

**Goal**: Measure whether the time gap since a stock's *previous* 52-week high predicts trade
outcome — independently of market/sector regime.  Analysis only; no changes to live scanner,
Telegram bot, or strategy rules.

**Approach**:
- For each closed trade in `trade_regime_tags`, load the stock's prior price series and compute
  the 252-day rolling max benchmark (`shift(1).rolling(252).max()`).
- Find the last date *before* entry where `close > benchmark` (point-in-time, no lookahead).
- Gap = `(prior trading days after last prior signal) + 1` (the +1 = entry date itself).

**Three freshness categories**:
- `insufficient_history` — fewer than 253 price rows before entry; can't form the 252-day window.
- `first_observed_high`  — 253+ rows, no prior signal found in cache (gap > cache start date).
- `gap_computed`         — prior signal found; gap_td / gap_cal / prior_date are valid.

**Six gap buckets**: < 1 wk (1–4 td) · 1w–1m (5–21 td) · 1–6m (22–129 td) ·
6–12m (130–251 td) · 1–3yr (252–755 td) · 3yr+ (756+ td).

**Important caveat**: `52wh_v1` price cache starts 2021-01-01 → `first_observed_high` in that
dataset means "last signal was before Jan 2021", not necessarily a multi-year base breakout.
The `52wh_v1_survivorship_10y` dataset (cache from 2018) is the trustworthy one for this bucket.

**Files created:**
- `52WeekHigh/analysis/freshness_tagger.py` — `tag_freshness()` (batch UPDATE), `run_freshness_analysis()`
  (CLI printed analysis), `load_freshness_df()` (for dashboard), `assign_bucket()` + `BUCKET_ORDER` (shared).

**Files modified:**
- `shared/models.py` — 4 new nullable columns on `TradeRegimeTag`:
  `freshness_category`, `freshness_gap_td`, `freshness_gap_cal`, `freshness_prior_date`.
- `52WeekHigh/run_regime_analysis.py` — `--checkpoint freshness` and `--checkpoint freshness-analyze`.
- `dashboard/tabs/tab_regime.py` — "Freshness Factor" 6th inner tab with: gap distribution histogram,
  bucket breakdown table + bar chart, first_observed_high vs. rest, freshness×regime cross-tab, caveats.

**To populate freshness data (run locally, after --checkpoint tag):**
```powershell
python 52WeekHigh/run_regime_analysis.py --checkpoint freshness --strategy-version 52wh_v1
python 52WeekHigh/run_regime_analysis.py --checkpoint freshness --strategy-version 52wh_v1_survivorship_10y
# Full printed analysis:
python 52WeekHigh/run_regime_analysis.py --checkpoint freshness-analyze
```

---

### ✅ S&P 500 Freshness Factor + Full Setup Button (complete, 2026-06-19)

**Goal**: Mirror Nifty freshness analysis for the S&P 500 backtest; add a single "Run Everything"
button in Setup & Admin that populates all data in the correct order for a fresh VPS deployment.

**S&P 500 freshness storage**:
- New `Sp500TradeFreshness` table (`sp500_trade_freshness`) with FK to trades.
  S&P 500 has no `trade_regime_tags` equivalent, so this is a standalone table.
- Populated by `tag_freshness_sp500()` using DELETE+INSERT (safe to re-run).
- Loads regime data from `sp500_market_regime` for the cross-tab (LEFT JOIN).

**Key S&P 500 vs Nifty lookback difference**:
- S&P 500 price cache starts 2005-01-01 (~20 year lookback).
- `first_observed_high` for S&P 500 genuinely means no prior 52-week high in 20 years —
  a reliable long-base breakout indicator.  Much more trustworthy than the Nifty 2022-present
  dataset (cache from 2021).

**Files changed:**
- `shared/models.py`: `Sp500TradeFreshness` table
- `52WeekHigh/analysis/freshness_tagger.py`: `sp500_52wh_v1` in `_CACHE_DIRS`,
  `tag_freshness_sp500()`, `load_freshness_df_sp500()`
- `SP500/run_sp500_backtest.py`: `--checkpoint freshness`
- `dashboard/tabs/tab_sp500.py`: "Freshness Factor" 5th sub-tab
- `dashboard/tabs/tab_setup.py`: Steps 8 + 9 (freshness buttons), "Run Everything (Steps 1–9)"
  primary button, updated DB status metrics (freshness row counts), contextual hints

**To populate on VPS (after git pull + docker compose up --build -d):**
Open dashboard → Setup & Admin → click **"Run Everything (Steps 1–9)"**
Or run the individual CLI commands shown in the Advanced section.

---

### ✅ 52WHU-S1 — US S&P 500 Breakout System: Session 1 (complete, 2026-06-26)

**New folder**: `52WeekHighUS/` — sibling to `52WeekHigh/` and `SP500/`.
**Folder name rationale**: mirrors `52WeekHigh/` naming convention; no collision with existing `SP500/` project.
**Dashboard tab label**: "US S&P 500 Breakout" (to be added in Session 4).
**Strategy version**: `"52whu_v1"`.
**DB file**: `data/sp500_us_breakout.db` — separate from `trading.db` (existing Nifty/SP500 system unaffected).

**Files created:**
- `52WeekHighUS/__init__.py`
- `52WeekHighUS/models.py` — SQLAlchemy tables: `us52wh_signals`, `us52wh_scan_runs`, `us52wh_positions`
- `52WeekHighUS/db.py` — engine/session factory pointing at `sp500_us_breakout.db`
- `52WeekHighUS/universe.py` — S&P 500 fetch from Wikipedia (pandas read_html); GICS sector → ETF mapping; 7-day cache with refresh
- `52WeekHighUS/data_loader.py` — batch yfinance OHLCV download (50/chunk, tenacity retry); Wilder ATR-14; SMA50/200/EMA14; Prior252High (shift(1).rolling(252).max() on High); AvgVol20; SwingLow5 (prior 5 days, today excluded); RS3M (63-day); partial-bar detection via 16:15 ET buffer; per-ticker cache
- `52WeekHighUS/signal_logic.py` — 10-point checklist (B1-B4 hard gates, B5 risk gate, B6-B10 graded checks); Part C formulas (MIN stop, capital-capped sizing); Part D tier assignment; cooldown (20 trading days); earnings best-effort
- `52WeekHighUS/run_backtest.py` — CLI with `--checkpoint universe|setup|backtest`
- `52WeekHighUS/tests/__init__.py`
- `52WeekHighUS/tests/test_signal.py` — 25 unit tests (all pass)

**Files modified:**
- `requirements.txt` — added `lxml>=4.9.0`, `pytest>=7.0.0`

**Key verified behaviors (25 tests, all green):**
- StructuralSL uses MIN(TodayCandleLow, 5DaySwingLow) × 0.997 — confirmed NOT MAX
- FinalQty = MIN(QtyRiskBased, QtyCapitalBased) — capital cap enforced
- Cooldown suppresses within ~30 calendar days (≈20 trading days)
- Each hard gate (B1-B4) blocks independently
- Tier A (≥4), B (2-3), C (0-1) — correct
- Missing earnings → 'not verified', signal still generated
- Empty/NaN/short data → SKIP result, no exception raised

**Universe**: 503 constituents fetched from Wikipedia; all 11 GICS sectors mapped to ETFs.

**Import note**: `52WeekHighUS` starts with a digit so Python can't use it as a direct package name.
Pattern (matches existing `52WeekHigh/`): add `_ROOT` and `_ROOT/52WeekHighUS` to sys.path; use flat absolute imports.
`signal_logic.py` (not `signal.py`) avoids shadowing Python's built-in `signal` module.

---

### ✅ 52WHU-S2 — US S&P 500 Breakout System: Session 2 — Backtest Engine (complete, 2026-06-26)

**Goal**: Build a three-version backtest engine that demonstrates how different execution
assumptions change realised P&L, without contaminating the live scanner/bot code paths.

**Files created:**
- `52WeekHighUS/backtest/__init__.py` — package marker
- `52WeekHighUS/backtest/engine.py` — full simulation engine (~950 lines)

**Files modified:**
- `52WeekHighUS/run_backtest.py` — `--checkpoint backtest` now fully implemented (was a stub)

**Three backtest versions:**

| Version | Stop base | SL% cap | Capital cap | Signal filter | Purpose |
|---|---|---|---|---|---|
| A | MAX(low, swing_low) × 0.997 | None | None | B4 only | Buggy baseline (intentional mistakes) |
| B | MIN(low, swing_low) × 0.997 | 6% hard | max_capital / entry | B4 + B5 | Corrected stop + sizing |
| C | Full spec via `evaluate_ticker` | 6% (B5) | full cap (B5) | All B1-B10 | Live-equivalent logic |

**Key engine design decisions:**
- `BACKTEST_START = date(2022, 1, 1)` — price data from 2020-01-01 for 252-day warmup
- Entry windows: t+1/t+2/t+3 (3-day cancel), with gap-up/gap-down slippage fills
- Time stop: 15 calendar days after entry
- EMA-14 exit: 2 consecutive closes below EMA14
- Trailing stop: `max_high_since_entry - ATR14_at_signal` (fixed delta, starts NEXT day after T1 fill)
- T1 at `entry × 1.03`, T2 at `entry × 1.06`; T1 fills half, trailing half runs
- Same-day SL/T1 conflict: SL first (conservative), UNLESS Open >= T1 (gap-up past target)
- `TradeR = TradeRealizedPL / InitialRisk` — unit-normalised return
- Look-ahead prevention: entry attempts processed BEFORE new signal detection each day
- `pos_state[version][ticker]` dict tracks `days_held`, `consec_ema`, `max_high` ephemerally

**Smoke test results (confirmed):**
- V_A signal: SL=98.70 (MAX-based: max(99,97)×0.997), qty=294 (no capital cap)
- V_B signal: SL=96.71 (MIN-based: min(99,97)×0.997), qty=97 (capital capped)
- End-to-end mini run (1 ticker, 3 months): 3 versions run, 0 filled (signal on last date — correct)

**Physical isolation confirmed:** `engine.py` imports nothing from `scanner/`, `bot/`, or `dashboard/`.
Version A buggy assumptions (`_v_a_signal`, `_v_b_signal`) are only callable from `engine.py`.

**To run:**
```powershell
venv\Scripts\python.exe 52WeekHighUS\run_backtest.py --checkpoint backtest
# Set SP500_US_ACCOUNT_SIZE / SP500_US_RISK_PERCENT / SP500_US_MAX_CAPITAL_PER_TRADE in .env
# First run downloads + caches all 503 tickers; expect ~5-10 min
```

---

## Open Questions / Pending Decisions

None — all design questions confirmed as of 2026-06-19.

---

## Known Risks & Mitigations

| Risk | Status | Mitigation |
|---|---|---|
| NSE blocks automated CSV fetch (403) | Handled | Session + browser headers; manual fallback documented |
| yfinance intraday unreliability | Not yet tested | Retry/backoff built in; test at Checkpoint 4; escalate to user if unacceptable |
| Survivorship bias in universe | Accepted | Documented in README and UI caveats; not hidden |
