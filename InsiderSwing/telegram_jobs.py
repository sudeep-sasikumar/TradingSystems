"""
InsiderSwing — Telegram jobs and message formatting.

Wired into the existing bot process (``52WeekHigh/bot/bot.py``) rather than
running a second bot: Telegram long-polling only tolerates one consumer per
token, and the Nifty/S&P 500 systems already own that process.  Three lines in
bot.py register everything here.

Message conventions match the other systems in this repo:
  * ``[INSIDER]`` prefix, as ``[S&P500]`` is used for the US breakout system
  * MarkdownV2 with every data value escaped
  * ``Signal price: $X — actual fill price may differ.``
  * Inline ``[✅ Accept] [❌ Reject]`` on every actionable alert
  * Alerts are never silently suppressed; a position-cap warning is appended
    instead

Alert points (per spec):
  1. new qualifying cluster signal      — watchlist, no trade yet
  2. technical confirmation fired       — actionable, Accept/Reject
  3. stop / target / time-stop hit      — exit notification, no buttons
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE), str(_HERE / "sources"), str(_HERE / "backtest")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logger = logging.getLogger("insider.telegram")

CALLBACK_PREFIX_ACCEPT = "ins_accept:"
CALLBACK_PREFIX_REJECT = "ins_reject:"

_MD_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def esc(value) -> str:
    """Escape a DATA value for MarkdownV2.  Never apply to formatting markers."""
    s = "" if value is None else str(value)
    return "".join("\\" + c if c in _MD_SPECIALS else c for c in s)


def _money(v: Optional[float], dp: int = 2) -> str:
    return "—" if v is None else f"${v:,.{dp}f}"


# ──────────────────────────────────────────────────────────────────────────────
#  Formatters
# ──────────────────────────────────────────────────────────────────────────────

def _why_lines(components_json: Optional[str], limit: int = 4) -> list[str]:
    """
    Turn the stored score breakdown into a human-readable 'why'.

    Every alert carries its own audit trail — the same contract the Nifty and
    S&P 500 conviction tiers hold to.  A score with no explanation is a number
    the user has no way to sanity-check at 7am.
    """
    if not components_json:
        return []
    try:
        data = json.loads(components_json)
    except Exception:
        return []

    lines: list[str] = []
    for ins in (data.get("insiders") or [])[:limit]:
        role = {"ceo_cfo": "CEO/CFO", "officer": "Officer",
                "director": "Director", "ten_pct": "10% owner"}.get(ins.get("role"), "Insider")
        bits = [f"{esc(role)} {esc(ins.get('name', '?'))} — {esc(_money(ins.get('buy_value'), 0))}"]
        ratio = ins.get("size_ratio")
        if ratio:
            bits.append(f"{esc(f'{ratio:.1f}x')} own avg")
        if ins.get("first_buy_in_lookback"):
            bits.append("first buy in 12m")
        lines.append("  • " + ", ".join(bits))
    return lines


def format_new_signal(sig) -> str:
    """Alert 1 — a cluster cleared the conviction threshold.  Watchlist only."""
    out = [
        "*\\[INSIDER\\] Cluster Signal*",
        "",
        f"*{esc(sig.ticker)}* — conviction *{esc(f'{sig.score:.0f}')}/100*",
        f"Cluster: {esc(sig.cluster_count or 0)} insider\\(s\\) buying "
        f"\\({esc({'ceo_cfo': 'CEO/CFO', 'officer': 'officer', 'director': 'director', 'ten_pct': '10% owner'}.get(sig.role_bucket_top, sig.role_bucket_top or 'n/a'))} led\\)",
        f"Filing date: {esc(sig.signal_date)}",
    ]

    why = _why_lines(sig.components_json)
    if why:
        out += ["", "*Why:*"] + why

    flags = []
    if sig.earnings_proximity_flag:
        flags.append("earnings within 2 weeks — buys in that window are more often plan\\-driven")
    if sig.sell_pressure_flag:
        flags.append("cluster SELLING exceeds buying — entry blocked")
    if flags:
        out += ["", "*Flags:* " + "; ".join(flags)]

    out += [
        "",
        f"_Awaiting technical confirmation \\(window: {esc(sig.expiry_date or 'n/a')}\\)\\._",
        "_No trade yet — this is a watchlist alert\\._",
    ]
    return "\n".join(out)


def format_confirmation(sig, plan, cap_note: Optional[str] = None) -> str:
    """Alert 2 — technical trigger fired.  This is the actionable one."""
    trig = {"dma_reclaim": "20/50-DMA reclaim",
            "range_breakout": "range breakout",
            "rsi_reset": "RSI reset from oversold"}.get(sig.trigger_type, sig.trigger_type or "?")

    out = [
        "*\\[INSIDER\\] Entry Confirmed*",
        "",
        f"*{esc(sig.ticker)}* — conviction *{esc(f'{sig.score:.0f}')}/100*, "
        f"{esc(sig.cluster_count or 0)} insider\\(s\\)",
        f"Trigger: {esc(trig)} on {esc(sig.trigger_date)}",
        "",
        f"Signal price: {esc(_money(plan.entry_ref_price))} — actual fill price may differ\\.",
        f"Stop: {esc(_money(plan.initial_stop))} \\({esc(plan.stop_basis)}\\)",
        f"Target: {esc(_money(plan.target_price))}",
        f"Size: {esc(plan.qty)} shares \\({esc(_money(plan.notional, 0))}\\)",
        f"Risk: {esc(_money((plan.risk_per_share or 0) * plan.qty, 0))} "
        f"\\({esc(f'{plan.risk_per_share:.2f}')}/share\\)",
        f"Time stop: {esc(_cfg().time_stop_days)} trading days",
    ]

    if plan.participation_rate:
        out.append(f"Order is {esc(f'{100 * plan.participation_rate:.2f}%')} of avg daily volume")

    if sig.earnings_proximity_flag:
        out += ["", "⚠️ Earnings within 2 weeks of the filing — score was down\\-weighted\\."]

    if cap_note:
        out += ["", esc(cap_note)]

    return "\n".join(out)


def format_exit(pos) -> str:
    """Alert 3 — position closed."""
    reason = {"stop": "stop loss", "trail": "trailing stop", "target": "target",
              "time_stop": "time stop", "delisted": "delisted"}.get(pos.exit_reason, pos.exit_reason)
    pnl = pos.realized_pnl or 0.0
    emoji = "✅" if pnl > 0 else "🔻"

    return "\n".join([
        f"*\\[INSIDER\\] Exit* {emoji}",
        "",
        f"*{esc(pos.ticker)}* closed on {esc(pos.exit_date)} — {esc(reason)}",
        f"Entry {esc(_money(pos.entry_price))} → Exit {esc(_money(pos.exit_price))}",
        f"P&L: {esc(_money(pnl))} \\({esc(f'{pos.r_multiple:.2f}' if pos.r_multiple else '—')}R\\)",
        f"Held from {esc(pos.entry_date)}",
    ])


def _cfg():
    import config as cfg
    return cfg.DEFAULT_CONFIG


# ──────────────────────────────────────────────────────────────────────────────
#  Jobs (registered on the bot's JobQueue)
# ──────────────────────────────────────────────────────────────────────────────

async def job_poll_signals(context) -> None:
    """Send watchlist alerts for new pending signals that haven't been sent."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode

    from db import session_scope
    from models import InsiderSignal

    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return

    with session_scope() as sess:
        pending = (
            sess.query(InsiderSignal)
            .filter(InsiderSignal.source == "live",
                    InsiderSignal.alert_sent.is_(False),
                    InsiderSignal.status.in_(("pending", "blocked")))
            .order_by(InsiderSignal.score.desc())
            .limit(20)
            .all()
        )
        for sig in pending:
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id, text=format_new_signal(sig),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                sig.alert_sent = True
                sig.telegram_message_id = str(msg.message_id)
            except Exception as exc:
                logger.error("Failed to send insider signal alert for %s: %s", sig.ticker, exc)


async def job_poll_confirmations(context) -> None:
    """
    Send actionable alerts for confirmed signals, with Accept/Reject buttons.

    The entry plan is rebuilt here so the message shows the size and stop the
    user would actually get, rather than a stale figure from signal time.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode

    import prices as price_mod
    import risk
    from db import get_engine, session_scope
    from models import InsiderPosition, InsiderSignal
    from sqlalchemy import text

    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return

    conf = _cfg()
    today = datetime.now(timezone.utc).date()

    with get_engine().connect() as conn:
        open_count = conn.execute(
            text("SELECT COUNT(*) FROM ins_positions WHERE status='open'")
        ).scalar() or 0

    with session_scope() as sess:
        confirmed = (
            sess.query(InsiderSignal)
            .filter(InsiderSignal.source == "live",
                    InsiderSignal.status == "confirmed",
                    InsiderSignal.trigger_alert_sent.is_(False))
            .order_by(InsiderSignal.score.desc())
            .all()
        )

        for sig in confirmed:
            pdf = price_mod.load_one(
                sig.ticker, start=(today - timedelta(days=400)).isoformat(), config=conf
            )
            if pdf is None or pdf.empty:
                logger.warning("No price data to size %s — skipping alert", sig.ticker)
                continue

            last = pdf.iloc[-1]
            plan = risk.build_entry(pdf.iloc[-2] if len(pdf) > 1 else last,
                                    float(last["Close"]), conf)
            if not plan.ok:
                logger.info("Confirmed signal %s not tradeable: %s", sig.ticker, plan.reason)
                sig.trigger_alert_sent = True
                sig.block_reason = plan.reason
                continue

            # Cap-reached signals still fire — never silently suppressed.
            cap_note = None
            if open_count >= conf.max_concurrent_positions:
                cap_note = (f"[CAP REACHED — {open_count}/{conf.max_concurrent_positions} "
                            "positions open (signal still recorded)]")

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"{CALLBACK_PREFIX_ACCEPT}{sig.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"{CALLBACK_PREFIX_REJECT}{sig.id}"),
            ]])
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id, text=format_confirmation(sig, plan, cap_note),
                    parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb,
                )
                sig.trigger_alert_sent = True
                sig.telegram_message_id = str(msg.message_id)
            except Exception as exc:
                logger.error("Failed to send insider confirmation for %s: %s", sig.ticker, exc)


async def job_poll_exits(context) -> None:
    """Send exit notifications for closed positions.  No buttons."""
    from telegram.constants import ParseMode

    from db import session_scope
    from models import InsiderPosition

    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return

    with session_scope() as sess:
        closed = (
            sess.query(InsiderPosition)
            .filter(InsiderPosition.status == "closed",
                    InsiderPosition.exit_notified.is_(False))
            .all()
        )
        for pos in closed:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=format_exit(pos),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                pos.exit_notified = True
            except Exception as exc:
                logger.error("Failed to send insider exit alert for %s: %s", pos.ticker, exc)


# ──────────────────────────────────────────────────────────────────────────────
#  Callback handling
# ──────────────────────────────────────────────────────────────────────────────

def handles(callback_data: str) -> bool:
    """True if this module owns the callback — lets bot.py dispatch cheaply."""
    return callback_data.startswith((CALLBACK_PREFIX_ACCEPT, CALLBACK_PREFIX_REJECT))


async def handle_callback(update, context) -> None:
    """Accept → create a position.  Reject → mark the signal rejected."""
    from datetime import date as _date

    import prices as price_mod
    import risk
    from db import session_scope
    from models import InsiderPosition, InsiderSignal

    query = update.callback_query
    data = query.data or ""
    await query.answer()

    conf = _cfg()
    accept = data.startswith(CALLBACK_PREFIX_ACCEPT)
    try:
        sig_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return

    with session_scope() as sess:
        sig = sess.get(InsiderSignal, sig_id)
        if sig is None:
            await query.edit_message_text("Signal no longer exists.")
            return

        if not accept:
            sig.status = "rejected"
            await query.edit_message_text(
                f"❌ Rejected — {sig.ticker}. The stock stays eligible for future signals."
            )
            return

        today = datetime.now(timezone.utc).date()
        pdf = price_mod.load_one(
            sig.ticker, start=(today - timedelta(days=400)).isoformat(), config=conf
        )
        if pdf is None or pdf.empty or len(pdf) < 2:
            await query.edit_message_text(f"Could not price {sig.ticker} — no position created.")
            return

        last = pdf.iloc[-1]
        plan = risk.build_entry(pdf.iloc[-2], float(last["Close"]), conf)
        if not plan.ok:
            await query.edit_message_text(f"{sig.ticker} not tradeable: {plan.reason}")
            return

        sess.add(InsiderPosition(
            signal_id=sig.id,
            ticker=sig.ticker,
            entry_date=today.isoformat(),
            # Entry price is the SIGNAL price at confirmation time, matching the
            # convention the other systems use. The actual fill may differ and
            # the alert says so.
            entry_price=plan.entry_ref_price,
            qty=plan.qty,
            initial_stop=plan.initial_stop,
            target_price=plan.target_price,
            atr_at_entry=plan.atr,
            initial_risk=plan.risk_per_share,
            highest_close_since_entry=plan.entry_ref_price,
            trailing_stop=plan.initial_stop,
            time_stop_date=(today + timedelta(days=int(conf.time_stop_days * 1.5))).isoformat(),
            score_at_entry=sig.score,
            cluster_count=sig.cluster_count,
        ))
        sig.status = "accepted"

        await query.edit_message_text(
            f"✅ Accepted — {sig.ticker}: {plan.qty} shares, "
            f"stop {plan.initial_stop:.2f}, target {plan.target_price:.2f}"
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Registration
# ──────────────────────────────────────────────────────────────────────────────

def register_jobs(job_queue) -> None:
    """Called from bot.py's _post_init.  Safe to call when the DB is empty."""
    job_queue.run_repeating(job_poll_signals, interval=120, first=20,
                            name="insider_poll_signals")
    job_queue.run_repeating(job_poll_confirmations, interval=180, first=45,
                            name="insider_poll_confirmations")
    job_queue.run_repeating(job_poll_exits, interval=300, first=75,
                            name="insider_poll_exits")
    logger.info("InsiderSwing Telegram jobs registered")
