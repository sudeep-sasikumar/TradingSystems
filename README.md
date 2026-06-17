# TradingSystems

A momentum trading system for Indian equities (NSE / Nifty 500).

## Phases

| Phase | Strategy | Status |
|---|---|---|
| 1 | 52-Week High Breakout (`52WeekHigh/`) | In progress |
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
