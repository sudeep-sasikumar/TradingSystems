#!/usr/bin/env python3
"""
52-Week High — Telegram Bot (Checkpoint 5)

This is a PERSISTENT, always-running service (restart: always in Docker).
If this process exits, in-flight Accept/Reject callbacks are lost.

Architecture:
    - PTB v20+ Application with long-polling (no webhook needed for VPS deploy)
    - PTB JobQueue for background polling jobs (no extra APScheduler needed here)
    - Scanner (scanner.py) writes Signal records to DB; bot polls DB and sends them
    - All Telegram I/O lives in this file — scanner never touches Telegram directly

Background jobs (via PTB JobQueue):
    every  60s  poll_signals     — find unsent pending signals, send to Telegram
    every 300s  poll_eod         — send EOD confirmation follow-ups
    every 300s  poll_exits       — send exit notifications for closed live trades
    every 300s  poll_expiry      — expire signals older than 24h
    10:15 UTC   eod_summary      — daily summary, Mon–Fri

Message design (confirmed in CLAUDE.md):
    - Signal price: ₹XXX — actual fill price may differ.
    - [CAP REACHED — X/X positions open] when open_count >= cap — never suppress
    - Inline keyboard [✅ Accept] [❌ Reject] on every signal
    - EOD follow-up: eod_confirmed or provisional_unconfirmed
    - Exit notification: automatic, no buttons
"""

import logging
import os
import sys
from contextlib import suppress
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
from dotenv import load_dotenv
from sqlalchemy import text
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent          # .../52WeekHigh/bot
_ROOT = _HERE.parent.parent                      # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(_ROOT / ".env")

from shared.db import get_engine, session_scope
from shared.models import Signal, Trade

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

# ── Constants ─────────────────────────────────────────────────────────────────
IST               = pytz.timezone("Asia/Kolkata")
STRATEGY_VERSION  = "52wh_v1"
SIGNAL_EXPIRY_H   = 24         # hours before a pending signal is auto-expired
_UTC              = timezone.utc


# ════════════════════════════════════════════════════════════════════════════
#  DB MIGRATION
# ════════════════════════════════════════════════════════════════════════════

def _migrate_db() -> None:
    """
    Add tracking columns to existing tables.
    ALTER TABLE ADD COLUMN is idempotent in SQLite -- errors are silently swallowed.
    """
    engine = get_engine()
    stmts = [
        "ALTER TABLE signals ADD COLUMN eod_notified INTEGER DEFAULT 0",
        "ALTER TABLE trades  ADD COLUMN exit_notified INTEGER DEFAULT 0",
        # Checkpoint 8b: conviction tier stored at signal creation time
        "ALTER TABLE signals ADD COLUMN conviction_tier TEXT",
        "ALTER TABLE signals ADD COLUMN regime_score INTEGER",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            with suppress(Exception):
                conn.execute(text(stmt))
                conn.commit()
    logger.debug("DB migration complete (columns may already have existed)")


# ════════════════════════════════════════════════════════════════════════════
#  MARKDOWNV2 HELPERS
# ════════════════════════════════════════════════════════════════════════════

_MDV2 = frozenset(r'\_*[]()~`>#+-=|{}.!')


def _esc(value) -> str:
    """Escape a data value for Telegram MarkdownV2. Apply to user data, NOT format markers."""
    return "".join(f"\\{c}" if c in _MDV2 else c for c in str(value))


# ════════════════════════════════════════════════════════════════════════════
#  MESSAGE FORMATTERS
# ════════════════════════════════════════════════════════════════════════════

def _fmt_conviction(row: pd.Series) -> str:
    """
    Return MarkdownV2 string for the conviction tier line.
    Returns empty string for old signals with no tier data.

    Tier rules (Checkpoint 8b):
      HIGH     : market 6M bottom-2 quintiles AND sector basket above 200-DMA
      AVOID    : market 6M strong_uptrend quintile
      STANDARD : everything else
    Advisory only -- never blocks the signal; user makes final call.
    """
    tier = row.get("conviction_tier")
    if not tier:
        return ""

    if tier == "HIGH":
        return (
            "\nConviction: *HIGH CONVICTION*\n"
            "_\\[Regime score >=2 \\- historically avg \\+40\\-60% in this environment\\]_"
        )
    if tier == "AVOID":
        return (
            "\n⚠ Conviction: *AVOID*\n"
            "_\\[Market in strong uptrend \\- historically weakest entry "
            "\\(avg \\+9% vs \\+27% baseline\\)\\. Take with caution\\.\\]_"
        )
    # STANDARD
    return "\nConviction: STANDARD"


def _fmt_signal(row: pd.Series) -> str:
    ticker   = _esc(row["ticker"])
    company  = _esc(row.get("company_name", ""))
    price    = float(row["signal_price"])
    bm       = float(row["benchmark_252d"]) if pd.notna(row.get("benchmark_252d")) else 0.0
    pct      = (price / bm - 1) * 100 if bm > 0 else 0.0
    ts       = str(row.get("scan_timestamp", ""))[:16].replace("T", " ")
    open_ct  = int(row.get("positions_open_at_signal") or 0)
    cap      = int(row.get("cap_at_signal") or 0)
    sig_type = str(row.get("signal_type", "intraday_provisional"))
    type_lbl = {
        "intraday_provisional": "Provisional",
        "eod_confirmed":        "EOD Confirmed",
        "provisional_unconfirmed": "Provisional \\(Unconfirmed\\)",
    }.get(sig_type, _esc(sig_type.replace("_", " ").title()))

    cap_note = (
        f"\n\n⚠ _CAP REACHED — {open_ct}/{cap} positions open \\(signal still recorded\\)_"
        if cap > 0 and open_ct >= cap
        else ""
    )

    conviction_note = _fmt_conviction(row)

    return (
        f"*52\\-Week High Signal*\n\n"
        f"*{ticker}* — {company}\n"
        f"Signal price: ₹{_esc(f'{price:.2f}')} — _actual fill price may differ\\._\n"
        f"Above 252\\-day high: ₹{_esc(f'{bm:.2f}')} \\(\\+{_esc(f'{pct:.2f}')}%\\)\n"
        f"Type: {type_lbl}"
        f"{conviction_note}\n\n"
        f"_Detected: {_esc(ts)} UTC_"
        f"{cap_note}"
    )


def _fmt_eod_update(row: pd.Series) -> str:
    ticker    = _esc(row["ticker"])
    company   = _esc(row.get("company_name", ""))
    confirmed = str(row.get("signal_type", "")) == "eod_confirmed"

    if confirmed:
        return (
            f"✅ *EOD Confirmed* — *{ticker}* {company}\n"
            f"Closed above 252\\-day close benchmark\\. Signal is confirmed\\."
        )
    return (
        f"⚠ *Provisional Unconfirmed* — *{ticker}* {company}\n"
        f"Did not close above 252\\-day close benchmark\\. "
        f"Signal remains provisional — no confirmed entry\\."
    )


def _fmt_exit(row: pd.Series) -> str:
    ticker   = _esc(row["ticker"])
    company  = _esc(row.get("company_name", ""))
    entry_p  = float(row.get("entry_price", 0) or 0)
    exit_p   = float(row.get("exit_price", 0) or 0)
    ret_pct  = float(row.get("return_pct", 0) or 0)
    hold_d   = int(row.get("holding_days", 0) or 0)
    stop_val = round(float(row.get("highest_price_reached", exit_p) or exit_p) * 0.80, 2)

    sign = "\\+" if ret_pct >= 0 else ""

    return (
        f"🔴 *Trailing Stop Exit* — *{ticker}* {company}\n"
        f"Exit: ₹{_esc(f'{exit_p:.2f}')} \\(stop was ₹{_esc(f'{stop_val:.2f}')}\\)\n"
        f"Entry: ₹{_esc(f'{entry_p:.2f}')}\n"
        f"Return: {sign}{_esc(f'{ret_pct:.2f}')}% over {hold_d} days"
    )


def _fmt_expiry(row: pd.Series) -> str:
    ticker  = _esc(row["ticker"])
    company = _esc(row.get("company_name", ""))
    price   = float(row.get("signal_price", 0) or 0)
    return (
        f"⏰ *Signal Expired* — *{ticker}* {company}\n"
        f"No action taken within 24h\\. "
        f"Signal price was ₹{_esc(f'{price:.2f}')}\\. "
        f"Stock remains eligible for future signals\\."
    )


def _keyboard(sig_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"accept:{sig_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{sig_id}"),
    ]])


# ════════════════════════════════════════════════════════════════════════════
#  DB ACTIONS — synchronous (called from async context)
# ════════════════════════════════════════════════════════════════════════════

def _accept_signal(sig_id: int) -> Optional[str]:
    """
    Accept a pending signal:
      - Signal.status → 'accepted'
      - Create Trade(source='live', status='open') with entry_price = signal_price
    Returns ticker on success, None if signal not found / already actioned.
    Entry price is the signal price at scan time — does NOT update when Accept is pressed.
    """
    today = date.today().isoformat()
    with session_scope() as sess:
        sig = sess.query(Signal).filter_by(id=sig_id).first()
        if not sig or sig.status != "pending":
            return None
        sig.status     = "accepted"
        sig.updated_at = datetime.now(_UTC).isoformat()
        trade = Trade(
            signal_id              = sig_id,
            ticker                 = sig.ticker,
            company_name           = sig.company_name,
            entry_date             = today,
            entry_price            = sig.signal_price,
            source                 = "live",
            highest_price_reached  = sig.signal_price,
            trailing_stop          = round(sig.signal_price * 0.80, 2),
            status                 = "open",
            trade_year             = date.today().year,
            strategy_version       = STRATEGY_VERSION,
        )
        sess.add(trade)
        return sig.ticker


def _reject_signal(sig_id: int) -> Optional[str]:
    """Reject a pending signal. Stock remains eligible for future signals."""
    with session_scope() as sess:
        sig = sess.query(Signal).filter_by(id=sig_id).first()
        if not sig or sig.status != "pending":
            return None
        sig.status     = "rejected"
        sig.updated_at = datetime.now(_UTC).isoformat()
        return sig.ticker


def _mark_telegram_sent(sig_id: int, msg_id: int) -> None:
    with session_scope() as sess:
        sig = sess.query(Signal).filter_by(id=sig_id).first()
        if sig:
            sig.telegram_message_id = str(msg_id)
            sig.updated_at          = datetime.now(_UTC).isoformat()


def _mark_eod_notified(sig_id: int) -> None:
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE signals SET eod_notified=1 WHERE id=:id"),
            {"id": sig_id},
        )
        conn.commit()


def _mark_exit_notified(trade_id: int) -> None:
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE trades SET exit_notified=1 WHERE id=:id"),
            {"id": trade_id},
        )
        conn.commit()


def _expire_signal(sig_id: int) -> None:
    with session_scope() as sess:
        sig = sess.query(Signal).filter_by(id=sig_id).first()
        if sig and sig.status == "pending":
            sig.status     = "expired"
            sig.updated_at = datetime.now(_UTC).isoformat()


# ════════════════════════════════════════════════════════════════════════════
#  BACKGROUND JOBS (PTB JobQueue)
# ════════════════════════════════════════════════════════════════════════════

def _chat_id() -> Optional[str]:
    cid = os.getenv("CHAT_ID")
    if not cid:
        logger.warning("CHAT_ID not set — cannot send Telegram messages")
    return cid


async def _job_poll_signals(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Find pending signals not yet sent to Telegram (telegram_message_id IS NULL).
    Send each with inline Accept/Reject keyboard.
    """
    chat_id = _chat_id()
    if not chat_id:
        return

    engine = get_engine()
    try:
        df = pd.read_sql(
            "SELECT * FROM signals "
            "WHERE telegram_message_id IS NULL AND status='pending' "
            "AND strategy_version=:v "
            "ORDER BY id ASC",
            engine, params={"v": STRATEGY_VERSION},
        )
    except Exception as exc:
        logger.error("poll_signals: DB read failed: %s", exc)
        return

    for _, row in df.iterrows():
        sig_id = int(row["id"])
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=_fmt_signal(row),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_keyboard(sig_id),
            )
            _mark_telegram_sent(sig_id, msg.message_id)
            logger.info("Sent signal %d (%s) to Telegram", sig_id, row["ticker"])
        except Exception as exc:
            logger.error("poll_signals: failed to send signal %d: %s", sig_id, exc)


async def _job_poll_eod(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Find signals whose signal_type was updated to 'eod_confirmed' or
    'provisional_unconfirmed' but haven't had the follow-up sent (eod_notified=0).
    """
    chat_id = _chat_id()
    if not chat_id:
        return

    engine = get_engine()
    try:
        df = pd.read_sql(
            "SELECT * FROM signals "
            "WHERE eod_notified=0 "
            "AND signal_type IN ('eod_confirmed','provisional_unconfirmed') "
            "AND strategy_version=:v",
            engine, params={"v": STRATEGY_VERSION},
        )
    except Exception as exc:
        logger.error("poll_eod: DB read failed: %s", exc)
        return

    for _, row in df.iterrows():
        sig_id = int(row["id"])
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=_fmt_eod_update(row),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            _mark_eod_notified(sig_id)
            logger.info("Sent EOD update for signal %d (%s)", sig_id, row["ticker"])
        except Exception as exc:
            logger.error("poll_eod: failed to send eod update %d: %s", sig_id, exc)


async def _job_poll_exits(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Find closed live trades not yet notified (exit_notified=0).
    Send exit notification (no buttons — exits are automatic).
    """
    chat_id = _chat_id()
    if not chat_id:
        return

    engine = get_engine()
    try:
        df = pd.read_sql(
            "SELECT * FROM trades "
            "WHERE source='live' AND status='closed' AND exit_notified=0 "
            "AND strategy_version=:v",
            engine, params={"v": STRATEGY_VERSION},
        )
    except Exception as exc:
        logger.error("poll_exits: DB read failed: %s", exc)
        return

    for _, row in df.iterrows():
        trade_id = int(row["id"])
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=_fmt_exit(row),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            _mark_exit_notified(trade_id)
            logger.info("Sent exit notification for trade %d (%s)", trade_id, row["ticker"])
        except Exception as exc:
            logger.error("poll_exits: failed to send exit %d: %s", trade_id, exc)


async def _job_poll_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Expire signals that have been pending > 24h without Accept/Reject.
    Send expiry notification if the signal had been sent to Telegram.
    """
    chat_id = _chat_id()
    cutoff  = (datetime.now(_UTC) - timedelta(hours=SIGNAL_EXPIRY_H)).isoformat()

    engine = get_engine()
    try:
        df = pd.read_sql(
            "SELECT * FROM signals "
            "WHERE status='pending' AND created_at <= :cutoff "
            "AND strategy_version=:v",
            engine, params={"cutoff": cutoff, "v": STRATEGY_VERSION},
        )
    except Exception as exc:
        logger.error("poll_expiry: DB read failed: %s", exc)
        return

    for _, row in df.iterrows():
        sig_id = int(row["id"])
        _expire_signal(sig_id)
        logger.info("Expired signal %d (%s)", sig_id, row["ticker"])

        if chat_id and pd.notna(row.get("telegram_message_id")):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=_fmt_expiry(row),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as exc:
                logger.warning("poll_expiry: failed to send expiry note %d: %s", sig_id, exc)


async def _job_eod_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Daily EOD summary at 10:15 UTC (15:45 IST):
    Signals sent today, accepted/rejected/expired, open positions, exits today.
    Also lists trailing stop levels for all open live positions.
    """
    chat_id = _chat_id()
    if not chat_id:
        return

    today = date.today().isoformat()
    engine = get_engine()

    try:
        sigs = pd.read_sql(
            "SELECT status, COUNT(*) AS n FROM signals "
            "WHERE signal_date=:d AND strategy_version=:v GROUP BY status",
            engine, params={"d": today, "v": STRATEGY_VERSION},
        )
        open_trades = pd.read_sql(
            "SELECT ticker, entry_price, trailing_stop, highest_price_reached "
            "FROM trades WHERE source='live' AND status='open' AND strategy_version=:v "
            "ORDER BY entry_price DESC",
            engine, params={"v": STRATEGY_VERSION},
        )
        exits_today = pd.read_sql(
            "SELECT COUNT(*) AS n FROM trades "
            "WHERE source='live' AND status='closed' AND exit_date=:d AND strategy_version=:v",
            engine, params={"d": today, "v": STRATEGY_VERSION},
        )
    except Exception as exc:
        logger.error("eod_summary: DB read failed: %s", exc)
        return

    sig_counts = {r["status"]: int(r["n"]) for _, r in sigs.iterrows()}
    n_today    = sum(sig_counts.values())
    n_accepted = sig_counts.get("accepted", 0)
    n_rejected = sig_counts.get("rejected", 0)
    n_expired  = sig_counts.get("expired",  0)
    n_open     = len(open_trades)
    n_exits    = int(exits_today["n"].iloc[0]) if not exits_today.empty else 0

    today_esc  = _esc(today)

    lines = [
        f"📊 *Daily Summary — {today_esc}*\n",
        f"Signals today: {n_today} "
        f"\\(✅ {n_accepted} accepted \\| ❌ {n_rejected} rejected \\| ⏰ {n_expired} expired\\)",
        f"Open positions: {n_open}",
        f"Exits today: {n_exits}",
    ]

    if not open_trades.empty:
        lines.append("\n*Open Positions \\(trailing stops\\):*")
        for _, r in open_trades.iterrows():
            tk   = _esc(r["ticker"])
            ep   = _esc(f"{float(r['entry_price']):.2f}")
            ts   = _esc(f"{float(r['trailing_stop']):.2f}")
            hp   = _esc(f"{float(r['highest_price_reached']):.2f}")
            lines.append(f"  • *{tk}* — entry ₹{ep} \\| high ₹{hp} \\| stop ₹{ts}")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info("Sent EOD summary for %s", today)
    except Exception as exc:
        logger.error("eod_summary: failed to send: %s", exc)


# ════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept / Reject button handler."""
    query = update.callback_query
    await query.answer()   # must answer within 60s or Telegram shows "loading"

    try:
        action, sig_id_str = query.data.split(":", 1)
        sig_id = int(sig_id_str)
    except (ValueError, AttributeError):
        logger.warning("Bad callback data: %s", query.data)
        return

    if action == "accept":
        ticker = _accept_signal(sig_id)
        if ticker:
            ticker_esc = _esc(ticker)
            await query.edit_message_text(
                f"✅ *Accepted* — *{ticker_esc}*\n"
                f"Open position created at signal price\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            logger.info("Signal %d (%s) ACCEPTED", sig_id, ticker)
        else:
            await query.edit_message_text(
                "Signal already actioned or not found\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    elif action == "reject":
        ticker = _reject_signal(sig_id)
        if ticker:
            ticker_esc = _esc(ticker)
            await query.edit_message_text(
                f"❌ *Rejected* — *{ticker_esc}*\n"
                f"Stock remains eligible for future signals\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            logger.info("Signal %d (%s) REJECTED", sig_id, ticker)
        else:
            await query.edit_message_text(
                "Signal already actioned or not found\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to /start."""
    await update.message.reply_text(
        "52-Week High Bot is running.\n\n"
        "Commands:\n"
        "/status — open positions and today's signals\n"
        "/positions — full open position list"
    )


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick status: open positions + today's pending signals."""
    today  = date.today().isoformat()
    engine = get_engine()
    try:
        n_open = pd.read_sql(
            "SELECT COUNT(*) AS n FROM trades WHERE source='live' AND status='open'", engine
        )["n"].iloc[0]
        n_pend = pd.read_sql(
            "SELECT COUNT(*) AS n FROM signals WHERE status='pending' AND signal_date=:d",
            engine, params={"d": today},
        )["n"].iloc[0]
    except Exception:
        await update.message.reply_text("Error reading database.")
        return

    today_esc = _esc(today)
    await update.message.reply_text(
        f"*Status — {today_esc}*\n\n"
        f"Open positions: {int(n_open)}\n"
        f"Pending signals today: {int(n_pend)}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all open live positions with trailing stops."""
    engine = get_engine()
    try:
        df = pd.read_sql(
            "SELECT ticker, company_name, entry_date, entry_price, "
            "highest_price_reached, trailing_stop FROM trades "
            "WHERE source='live' AND status='open' AND strategy_version=:v "
            "ORDER BY entry_date DESC",
            engine, params={"v": STRATEGY_VERSION},
        )
    except Exception:
        await update.message.reply_text("Error reading database.")
        return

    if df.empty:
        await update.message.reply_text("No open positions.")
        return

    lines = ["*Open Positions*\n"]
    for _, r in df.iterrows():
        tk  = _esc(r["ticker"])
        dt  = _esc(str(r["entry_date"]))
        ep  = _esc(f"{float(r['entry_price']):.2f}")
        hp  = _esc(f"{float(r['highest_price_reached']):.2f}")
        ts  = _esc(f"{float(r['trailing_stop']):.2f}")
        lines.append(f"*{tk}* \\(entry {dt}\\)\n  ₹{ep} → high ₹{hp} \\| stop ₹{ts}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ════════════════════════════════════════════════════════════════════════════
#  APPLICATION LIFECYCLE
# ════════════════════════════════════════════════════════════════════════════

async def _post_init(app: Application) -> None:
    """Called after Application is initialized but before polling starts."""
    jq = app.job_queue

    # Polling jobs
    jq.run_repeating(_job_poll_signals,  interval=60,  first=10,  name="poll_signals")
    jq.run_repeating(_job_poll_eod,      interval=300, first=30,  name="poll_eod")
    jq.run_repeating(_job_poll_exits,    interval=300, first=60,  name="poll_exits")
    jq.run_repeating(_job_poll_expiry,   interval=300, first=90,  name="poll_expiry")

    # Daily summary: 10:15 UTC Mon–Fri
    jq.run_daily(
        _job_eod_summary,
        time=time(10, 15, tzinfo=_UTC),
        days=(0, 1, 2, 3, 4),
        name="eod_summary",
    )

    logger.info("Jobs scheduled. Polling for Telegram updates...")


def main() -> None:
    _migrate_db()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise SystemExit("BOT_TOKEN not set in .env — cannot start bot")

    app = (
        Application.builder()
        .token(bot_token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",     handle_start))
    app.add_handler(CommandHandler("status",    handle_status))
    app.add_handler(CommandHandler("positions", handle_positions))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Starting 52-Week High Telegram Bot (long-polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
