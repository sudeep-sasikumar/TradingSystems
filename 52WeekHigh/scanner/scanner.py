"""
52-Week High — Hourly Intraday Scanner (STUB — Checkpoint 4)

ENTRY SIGNAL DESIGN (confirmed with user):
    BACKTEST uses close-based 252-day benchmark.
    LIVE SCANNER uses intraday-high-based 252-day benchmark:
        - Provisional alert fires when current intraday price >
          max(daily HIGH, prior 252 trading days).
        - At EOD, a close-confirmation pass runs. If the stock also
          closed above the close-based 252-day level, signal is
          "eod_confirmed"; otherwise logged as "provisional_unconfirmed"
          and a follow-up Telegram note is sent.
    This asymmetry (intraday alert, close confirmation) is intentional.

Runs via APScheduler during NSE market hours:
    9:15 AM – 3:30 PM IST  =  3:45 AM – 10:00 AM UTC
"""
# TODO: Implement at Checkpoint 4
# Key tasks:
#   1. APScheduler with market-hours guard (UTC timezone conversion)
#   2. Fetch latest intraday prices for all Nifty 500 tickers (yfinance)
#   3. Compute 252-day intraday-high benchmark from daily data
#   4. Detect new-high crossings (vs. open positions + pending signals)
#   5. Check trailing stop breaches for open positions
#   6. Write new Signal records to SQLite
#   7. Trigger Telegram bot notification (via shared message queue or DB poll)
