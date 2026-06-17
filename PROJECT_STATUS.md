# Project Status — TradingSystems

> **For new sessions**: Read `CLAUDE.md` first (full spec + design decisions), then this file for build state.

---

## Current State

- **Date last updated**: 2026-06-17
- **Active phase**: Phase 1 — 52-Week High Momentum Strategy
- **Completed checkpoints**: 0 (Scaffolding)
- **Next action**: Run Checkpoint 1 universe fetch, show output to user for confirmation

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

### 🔄 Checkpoint 1 — Universe Fetch + Caching (READY TO RUN)

**Goal**: Fetch Nifty 500 from NSE, cache locally, show user the list for confirmation.

**Command**:
```powershell
cd "E:\Trading Systems"
venv\Scripts\python.exe 52WeekHigh\run_backtest.py --checkpoint universe
```

**Files**:
- `52WeekHigh/backtest/universe.py` — fully implemented
- `52WeekHigh/run_backtest.py` — Checkpoint 1 implemented

**Status**: Awaiting user confirmation of Nifty 500 list output.

---

### ⬜ Checkpoint 2 — Backtest Engine

Not started. Start only after Checkpoint 1 confirmed.

**Files to create/complete**:
- `52WeekHigh/backtest/engine.py` — core strategy: signal detection, trailing stop, exit logic, trade records
- Extend `52WeekHigh/run_backtest.py` with `--checkpoint backtest`
- Populate SQLite `trades` table (strategy_version = "52wh_v1")
- Output: combined stats + year-by-year breakdown table + sample trades for user to spot-check

**Backtest spec**:
- Period: Jan 2022 – today; lookback data from ~Jan 2021
- Data: yfinance daily OHLCV, `.NS` suffix, chunks of ~50, with retry/backoff
- Signal: daily CLOSE > max(daily CLOSE, prior 252 trading days)
- Exit: daily CLOSE ≤ trailing_stop (= max_price_since_entry × 0.80)
- Per-trade record: ticker, company_name, entry_date, entry_price, highest_price_reached, exit_date, exit_price, holding_days, return_pct, trade_year
- Stats: combined (full period) + year-by-year table

---

### ⬜ Checkpoint 3 — Dashboard (Backtest View Only)

Not started. Start after Checkpoint 2 validated.

**Files to complete**:
- `dashboard/tabs/tab_52wh.py` — backtest stats, year-by-year table, trade log, equity curve
- Chart label: `"Illustrative, equal-weight, no capital constraints — not a real portfolio simulation"`

---

### ⬜ Checkpoint 4 — Live Scanner (No Telegram Yet)

Not started. Start after Checkpoint 3.

**Files to complete**:
- `52WeekHigh/scanner/scanner.py` — hourly scanner, APScheduler, market-hours check
- Signal logic: intraday price > max(daily HIGH, prior 252 trading days)
- Cross-references open positions + pending signals from SQLite
- Checks trailing stop breaches
- Verify signal detection on recent real data before wiring Telegram

**Risk flag**: yfinance intraday reliability for 500 tickers. Test carefully. Report to user if unacceptable.

---

### ⬜ Checkpoint 5 — Telegram Bot (Accept/Reject)

Not started. Start after Checkpoint 4.

**Files to complete**:
- `52WeekHigh/bot/bot.py` — persistent python-telegram-bot service
- Entry signal → inline keyboard [Accept] [Reject]
- Cap warning in message when MAX_CONCURRENT_POSITIONS reached
- 24-hour auto-expiry with follow-up message
- On Accept: create open position record in DB
- Stop-loss exit alerts: automatic, no buttons needed
- Daily EOD summary (not per-scan — too spammy hourly)
- Trailing stop daily updates for open positions

---

### ⬜ Checkpoint 6 — Docker + Deployment

Not started. Build last.

**Files to complete**:
- `docker/Dockerfile.scanner` — flesh out stub
- `docker/Dockerfile.bot` — flesh out stub
- `docker/Dockerfile.dashboard` — flesh out stub
- `docker-compose.yml` — flesh out stub
- Target: one-click deploy on Hostinger VPS from GitHub repo

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
