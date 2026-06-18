# Project Status — TradingSystems

> **For new sessions**: Read `CLAUDE.md` first (full spec + design decisions), then this file for build state.

---

## Current State

- **Date last updated**: 2026-06-18
- **Active phase**: Phase 1 — 52-Week High Momentum Strategy
- **Completed checkpoints**: 0, 1, 2, 3, 4, 5, 6
- **Next action**: Phase 1 complete — deploy to Hostinger VPS

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
cp .env.example .env   # fill in BOT_TOKEN, CHAT_ID, etc.
mkdir -p data/cache
docker compose up -d --build
docker compose ps      # verify all three services running
```

**Day-to-day**:
```bash
docker compose pull && docker compose up -d   # deploy latest from git
docker compose logs -f bot                    # tail bot logs
docker compose logs -f scanner               # tail scanner logs
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

## Open Questions / Pending Decisions

None — all design questions confirmed as of 2026-06-17.

---

## Known Risks & Mitigations

| Risk | Status | Mitigation |
|---|---|---|
| NSE blocks automated CSV fetch (403) | Handled | Session + browser headers; manual fallback documented |
| yfinance intraday unreliability | Not yet tested | Retry/backoff built in; test at Checkpoint 4; escalate to user if unacceptable |
| Survivorship bias in universe | Accepted | Documented in README and UI caveats; not hidden |
