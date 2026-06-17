"""
52-Week High — Telegram Bot Service (STUB — Checkpoint 5)

This is a PERSISTENT long-running service (not a cron job).
It must remain running at all times to receive Telegram button-press
callbacks. If this process exits, callbacks are lost.

In Docker: use `restart: always` for this service specifically.
The scanner can be a scheduled job; this cannot.

Responsibilities:
    - Send entry signal alerts with [Accept] [Reject] inline buttons
    - Include [CAP REACHED — X/X] warning when position cap is met
    - Auto-expire pending signals after 24 hours with follow-up message
    - On Accept: create Trade record in SQLite (source='live')
    - On Reject: log as rejected; stock remains eligible for future signals
    - Send stop-loss exit alerts automatically (no buttons needed for exits)
    - Send daily EOD summary: signals today / accepted / rejected / expired /
      currently open / trailing stops updated
    - Send trailing stop update messages once daily for open positions
"""
# TODO: Implement at Checkpoint 5
# Key tasks:
#   1. python-telegram-bot Application with long-polling
#   2. InlineKeyboardButton [Accept] [Reject] handlers
#   3. 24-hour expiry job (APScheduler or asyncio)
#   4. SQLite Signal.status update on each action
#   5. Trade creation on Accept
#   6. Daily EOD summary job
