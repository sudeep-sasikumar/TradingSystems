# TradingSystems — Claude Code Project Guide

> **For new Claude Code sessions**: Read this file FIRST, then `PROJECT_STATUS.md` for current build state.
> This is a standalone project with NO relationship to any other project.

---

## Quick Status

**Phase**: Phase 1 — 52-Week High Momentum Strategy
**GitHub**: https://github.com/sudeep-sasikumar/TradingSystems
**Local path**: `E:\Trading Systems`
**venv**: `E:\Trading Systems\venv\Scripts\python.exe`
**Streamlit port**: 8502

See `PROJECT_STATUS.md` for checkpoint-by-checkpoint build state.

---

## Git Workflow — DO NOT deviate

- **All commits go directly to `master`** — no feature branches, no PRs, ever.
- Never create a branch. Never open a PR. Never suggest doing so.
- When asked to commit: stage the relevant files, commit to master, push to origin/master. Done.

---

## Confirmed Design Decisions — Do NOT change without asking the user

### 1. Entry Signal: Dual-benchmark approach
- **Backtest** (daily EOD data): Signal triggers when today's **CLOSE** strictly exceeds `max(daily CLOSE, trailing 252 trading days)`. Pure close-based. No ambiguity.
- **Live scanner** (intraday): **Provisional alert** fires when intraday price crosses the **intraday-high-based 252-day benchmark** (`max(daily HIGH, trailing 252 trading days)`). At EOD, a close-confirmation pass runs. If the stock CLOSED above the close-based 252-day level, signal is "eod_confirmed"; if not, it's logged "provisional_unconfirmed" and a follow-up Telegram note is sent.
- **Explicit asymmetry**: intraday price → ALERT (provisional). Day's close → CONFIRMATION for recording the trade. This comment must appear in `scanner.py` and `bot.py`.

### 2. Entry Price on Accept
- Recorded as **signal price at scan time** — the market price when the scanner ran.
- Does NOT update when Accept is pressed later.
- Telegram alert must include: `"Signal price: ₹XXX — actual fill price may differ."`

### 3. Re-entry Suppression Rules
| Signal/Trade State | Re-entry suppressed? |
|---|---|
| OPEN position | Yes — suppress new signals for this ticker |
| PENDING signal | Yes — suppress duplicate alerts while pending |
| REJECTED | No — stock eligible for fresh future signal |
| EXPIRED | No — stock eligible for fresh future signal |

### 4. Position Cap Behavior
- Cap = `MAX_CONCURRENT_POSITIONS` in `.env` (default 20)
- When cap is reached: signals STILL fire to Telegram with `[CAP REACHED — X/X positions open]`
- **Never silently suppress a signal**

### 5. Exit Rules
- Exit triggered when **daily CLOSE** ≤ current trailing stop level
- Trailing stop = `max_price_since_entry × 0.80`
- Stop only moves up, never down
- Intraday close-based exit asymmetry: entries may be flagged intraday, exits confirmed on close only

### 6. Backtest Assumptions (label these everywhere in UI and code)
- Equal-weight, **UNLIMITED capital** — no position cap applied retroactively
- **No** transaction costs, slippage, STT, or brokerage modeled
- Universe = current Nifty 500 (survivorship bias — see README)
- Required label: `"Illustrative, equal-weight, no capital constraints — not a real portfolio simulation"`

### 7. strategy_version
- Phase 1: `"52wh_v1"` — written to every trade and signal record in SQLite

---

## Strategy Rules (exact — do not change without user confirmation)

1. **Signal**: Price reaches new 252-trading-day high (close-based for backtest; intraday-high-based for live)
2. **Initial stop**: entry_price × 0.80
3. **Trailing stop**: max(price since entry) × 0.80 — moves up only, never down
4. **Exit**: triggered when daily CLOSE ≤ trailing_stop
5. **Re-entry**: once in open trade, ignore further signals until stopped out
6. **Position limit**: configurable cap; cap-reached signals still sent with warning
7. **Backtest sizing**: equal-weight, unlimited capital (no cap applied)

---

## Universe

- Nifty 500 from NSE archives: `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`
- Requires browser-like headers (NSE blocks naive requests) + session cookie via landing page
- Cached at `data/cache/nifty500.csv`; falls back to cache on any failure
- **Manual fallback**: download the URL in a browser, save to `data/cache/nifty500.csv`
- **Survivorship bias**: current constituent list only — documented in README, not hidden
- yfinance ticker format: `{SYMBOL}.NS`

---

## Directory Structure

```
E:\Trading Systems\
├── 52WeekHigh\                   # Phase 1 strategy
│   ├── run_backtest.py           # CLI entry point (run from here)
│   ├── backtest\
│   │   ├── __init__.py
│   │   ├── universe.py           # Nifty 500 fetch + cache
│   │   └── engine.py             # strategy logic, trade log, stats (Checkpoint 2)
│   ├── scanner\
│   │   ├── __init__.py
│   │   └── scanner.py            # hourly intraday scanner (Checkpoint 4)
│   └── bot\
│       ├── __init__.py
│       └── bot.py                # persistent Telegram bot (Checkpoint 5)
│
├── dashboard\                    # shared across ALL phases
│   ├── app.py                    # st.tabs() shell — add new tabs here for new phases
│   └── tabs\
│       ├── __init__.py
│       └── tab_52wh.py           # Phase 1 tab (Checkpoint 3)
│
├── shared\                       # shared by all phases and all services
│   ├── __init__.py
│   ├── models.py                 # SQLAlchemy table definitions
│   └── db.py                     # DB engine, session factory
│
├── data\
│   ├── cache\                    # nifty500.csv lives here
│   └── trading.db                # single SQLite DB — all phases, all services write here
│
├── docker\
│   ├── Dockerfile.scanner
│   ├── Dockerfile.bot
│   └── Dockerfile.dashboard
│
├── venv\                         # project venv — D:\Python313 base
├── docker-compose.yml
├── .env                          # NOT committed — secrets
├── .env.example                  # committed template
├── requirements.txt
├── .gitignore
├── CLAUDE.md                     # THIS FILE
├── PROJECT_STATUS.md             # checkpoint-by-checkpoint build state
└── README.md
```

---

## Service Architecture

| Service | Docker lifecycle | Why |
|---|---|---|
| `scanner` | Long-running with internal APScheduler | Wakes hourly during market hours (9:15–15:30 IST = 03:45–10:00 UTC), checks signals + stop-losses |
| `bot` | **Always running, `restart: always`** | Must stay alive to receive Telegram button-press callbacks. If it exits, callbacks are lost. |
| `dashboard` | Always running | Streamlit web server on port 8502 |

---

## Environment Variables (.env)

| Variable | Purpose | Example |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather | `123456:ABC...` |
| `CHAT_ID` | Your Telegram chat ID | `123456789` |
| `MAX_CONCURRENT_POSITIONS` | Live position cap | `20` |
| `DASHBOARD_PORT` | Streamlit port | `8502` |
| `DB_PATH` | SQLite file path | `./data/trading.db` |
| `CACHE_DIR` | Cache directory | `./data/cache` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

---

## Running Locally

```powershell
# Activate venv
E:\Trading Systems\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Checkpoint 1 — verify Nifty 500 universe
cd "E:\Trading Systems"
python 52WeekHigh\run_backtest.py --checkpoint universe

# Checkpoint 2 — run full backtest
python 52WeekHigh\run_backtest.py --checkpoint backtest

# Run dashboard
streamlit run dashboard\app.py
```

---

## Key Technical Notes

- **yfinance intraday**: Known reliability issues for 500 tickers at hourly frequency. Built with retry/backoff. This is near-real-time, not guaranteed. If unacceptable in testing → upgrade path is Zerodha Kite Connect (Phase 2 decision).
- **Backtest lookback**: Data fetched from ~Jan 2021 so the 252-day window is valid from Jan 2022 (the actual backtest start).
- **Batch downloads**: yfinance fetched in chunks of ~50 tickers with retry. Failed tickers logged explicitly — never silently dropped.
- **2026 label**: Auto-detected at runtime, shown as `"2026 (YTD — partial)"` in year-by-year table.
- **SQLite**: Single DB for all phases. `strategy_version` column separates phase data.

---

## For New Claude Code Sessions — Checklist

1. Read this file (`CLAUDE.md`) fully
2. Read `PROJECT_STATUS.md` for current build state
3. Do NOT change confirmed design decisions without asking user
4. Confirm any ambiguous spec points with user before coding (user's explicit preference)
5. Update `PROJECT_STATUS.md` when starting and completing checkpoints
