#!/usr/bin/env python3
"""
InsiderSwing — live daily scanner.

Runs once per US trading day after the close and after that day's Form 4
dissemination window, and does five things in order:

    1. incremental Form 4 ingest for the current universe
    2. recompute conviction scores over the recent window
    3. raise new qualifying signals (status='pending')
    4. check pending signals for technical confirmation; confirm or expire
    5. update open positions against stops, targets and the time stop

It writes to the DB only.  Telegram I/O lives entirely in the bot process
(``InsiderSwing/telegram_jobs.py``, wired into ``52WeekHigh/bot/bot.py``) —
the same separation the Nifty and S&P 500 scanners use, so a scanner crash can
never lose an in-flight Accept/Reject callback.

SCHEDULE
--------
23:15 UTC Mon–Fri = 19:15 ET (EDT) / 18:15 ET (EST).

Why that late: EDGAR accepts filings until 22:00 ET, and anything accepted
after 17:30 ET carries the NEXT business day's filing date.  Running at 19:15 ET
means the day's filing-date cohort is complete and the daily bar has settled.
Running earlier would systematically miss late-afternoon filings — which is
exactly when a lot of Form 4s land.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE), str(_HERE / "sources"), str(_HERE / "backtest")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg          # noqa: E402
import prices as price_mod    # noqa: E402
import risk                   # noqa: E402
import scoring                # noqa: E402
import technical              # noqa: E402
import universe as univ       # noqa: E402
from db import get_engine, session_scope     # noqa: E402
from models import (                          # noqa: E402
    InsiderPosition, InsiderScanRun, InsiderSignal,
)

logger = logging.getLogger("insider.scanner")

_UTC = timezone.utc
SCAN_HOUR_UTC = int(os.getenv("INSIDER_SCAN_HOUR_UTC", "23"))
SCAN_MINUTE_UTC = int(os.getenv("INSIDER_SCAN_MINUTE_UTC", "15"))

# How far back to re-ingest each run.  Overlap is deliberate: Form 4/A
# amendments and late acceptances can appear with a back-dated filing date, and
# re-ingesting a week costs nothing thanks to the uniqueness constraints.
INGEST_LOOKBACK_DAYS = 7


def _now() -> str:
    return datetime.now(_UTC).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
#  Steps
# ──────────────────────────────────────────────────────────────────────────────

def _existing_signal_keys(param_key: str, since: date) -> set[tuple[str, str]]:
    from sqlalchemy import text
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT ticker, signal_date FROM ins_signals "
                 "WHERE source='live' AND param_key=:pk AND signal_date >= :since"),
            {"pk": param_key, "since": since.isoformat()},
        ).fetchall()
    return {(r[0], r[1]) for r in rows}


def _raise_new_signals(qual: pd.DataFrame, conf: cfg.InsiderConfig, lookback: date) -> int:
    """Insert qualifying scores that aren't already recorded as live signals."""
    if qual is None or qual.empty:
        return 0

    pk = conf.param_key()
    existing = _existing_signal_keys(pk, lookback)
    added = 0

    with session_scope() as sess:
        for row in qual.itertuples(index=False):
            key = (str(row.ticker), str(row.as_of_date))
            if key in existing:
                continue
            existing.add(key)

            sig_date = datetime.strptime(str(row.as_of_date)[:10], "%Y-%m-%d").date()
            sess.add(InsiderSignal(
                ticker=str(row.ticker),
                signal_date=sig_date.isoformat(),
                param_key=pk,
                score=float(row.score),
                cluster_count=int(getattr(row, "cluster_count", 0) or 0),
                role_bucket_top=getattr(row, "role_bucket_top", None),
                sell_pressure_flag=bool(getattr(row, "sell_pressure_flag", False)),
                earnings_proximity_flag=bool(getattr(row, "earnings_proximity_flag", False)),
                components_json=getattr(row, "components_json", None),
                status=str(getattr(row, "status", "pending")),
                block_reason=getattr(row, "block_reason", None),
                # Calendar-day approximation of the confirmation window; the
                # actual trigger scan below works in trading days on the
                # instrument's own calendar, which is the binding rule.
                expiry_date=(sig_date + timedelta(
                    days=int(conf.confirmation_window_days * 1.5) + conf.signal_lag_days
                )).isoformat(),
                source="live",
            ))
            added += 1

    if added:
        logger.info("Raised %d new insider signals", added)
    return added


def _check_confirmations(conf: cfg.InsiderConfig, today: date) -> tuple[int, int]:
    """
    Scan pending signals for a technical trigger.  Returns (confirmed, expired).

    A signal that has run past its window without a trigger is marked 'expired'
    rather than deleted — the expired set is what makes it possible to measure
    later how much the timing requirement costs.
    """
    from sqlalchemy import text

    with get_engine().connect() as conn:
        pending = pd.read_sql(
            text("SELECT * FROM ins_signals WHERE source='live' AND status='pending' "
                 "AND param_key=:pk ORDER BY signal_date"),
            conn, params={"pk": conf.param_key()},
        )
    if pending.empty:
        return 0, 0

    confirmed = expired = 0
    start = (today - timedelta(days=400)).isoformat()

    for row in pending.itertuples(index=False):
        ticker = str(row.ticker)
        sig_date = datetime.strptime(str(row.signal_date)[:10], "%Y-%m-%d").date()

        pdf = price_mod.load_one(ticker, start=start, config=conf)
        if pdf is None or pdf.empty:
            logger.warning("No price data for pending signal %s (%s)", ticker, sig_date)
            continue

        actionable = price_mod.shift_sessions(pdf, sig_date, conf.signal_lag_days)
        if actionable is None:
            continue

        res = technical.find_trigger(
            pdf, from_date=actionable.date(),
            window_sessions=conf.confirmation_window_days, config=conf,
        )

        with session_scope() as sess:
            sig = sess.get(InsiderSignal, int(row.id))
            if sig is None or sig.status != "pending":
                continue

            if res.fired:
                sig.status = "confirmed"
                sig.trigger_date = res.trigger_date.date().isoformat()
                sig.trigger_type = res.trigger_type
                sig.trigger_price = res.trigger_price
                confirmed += 1
                logger.info("Trigger fired: %s (%s) on %s", ticker, res.trigger_type,
                            sig.trigger_date)
            else:
                # Only expire once the window has genuinely elapsed on the
                # instrument's own calendar — not on a calendar-day guess.
                last_session = price_mod.shift_sessions(
                    pdf, actionable.date(), conf.confirmation_window_days - 1
                )
                if last_session is not None and pdf.index[-1] > last_session:
                    sig.status = "expired"
                    sig.expiry_date = last_session.date().isoformat()
                    expired += 1

    return confirmed, expired


def _update_positions(conf: cfg.InsiderConfig, today: date) -> int:
    """Advance open live positions against stop / target / trailing / time stop."""
    from sqlalchemy import text

    with get_engine().connect() as conn:
        open_pos = pd.read_sql(
            text("SELECT * FROM ins_positions WHERE status='open'"), conn
        )
    if open_pos.empty:
        return 0

    exits = 0
    start = (today - timedelta(days=400)).isoformat()

    for row in open_pos.itertuples(index=False):
        ticker = str(row.ticker)
        pdf = price_mod.load_one(ticker, start=start, config=conf)
        if pdf is None or pdf.empty:
            continue

        entry_date = datetime.strptime(str(row.entry_date)[:10], "%Y-%m-%d").date()
        bars = pdf.loc[pdf.index > pd.Timestamp(entry_date)]
        if bars.empty:
            continue

        # Replay from entry each run.  The scanner is stateless between runs by
        # design: a missed day (VPS reboot, holiday) must not silently skip the
        # stop check for that day.
        plan = risk.EntryPlan(
            ok=True,
            entry_ref_price=float(row.entry_price),
            entry_price=float(row.entry_price),
            qty=int(row.qty),
            notional=float(row.entry_price) * int(row.qty),
            initial_stop=float(row.initial_stop),
            risk_per_share=float(row.initial_risk or 0) or None,
            target_price=float(row.target_price) if row.target_price else None,
            atr=float(row.atr_at_entry) if row.atr_at_entry else None,
        )
        if not plan.risk_per_share or not plan.atr or not plan.target_price:
            continue

        pos = risk.OpenPosition(plan, pd.Timestamp(entry_date), conf)
        event = None
        for ts, bar in bars.iterrows():
            ev = pos.update(bar, ts)
            if ev.exited:
                event = ev
                break

        with session_scope() as sess:
            db_pos = sess.get(InsiderPosition, int(row.id))
            if db_pos is None or db_pos.status != "open":
                continue
            db_pos.highest_close_since_entry = pos.highest_close
            db_pos.trailing_stop = pos.stop
            db_pos.updated_at = _now()

            if event is not None:
                out = pos.realise(float(event.exit_ref_price))
                db_pos.status = "closed"
                db_pos.exit_date = event.exit_date.date().isoformat()
                db_pos.exit_price = out["exit_price"]
                db_pos.exit_reason = event.exit_reason
                db_pos.realized_pnl = out["net_pnl"]
                db_pos.r_multiple = out["r_multiple"]
                exits += 1
                logger.info("Position closed: %s %s @ %.2f (%.2fR)", ticker,
                            event.exit_reason, out["exit_price"], out["r_multiple"] or 0.0)

    return exits


# ──────────────────────────────────────────────────────────────────────────────
#  Scan
# ──────────────────────────────────────────────────────────────────────────────

def run_scan(trigger_type: str = "scheduled", skip_ingest: bool = False) -> dict:
    """One full scan pass.  Every outcome is logged to ins_scan_runs."""
    import ingest

    conf = cfg.DEFAULT_CONFIG
    cfg.ensure_dirs()
    today = datetime.now(_UTC).date()

    with session_scope() as sess:
        run = InsiderScanRun(trigger_type=trigger_type, start_time=_now(), status="running")
        sess.add(run)
        sess.flush()
        run_id = run.id

    result: dict = {"run_id": run_id, "trigger_type": trigger_type, "date": today.isoformat()}
    status = "ok"
    error = None

    try:
        tickers = univ.universe_on_date(today)
        result["universe"] = len(tickers)
        if not tickers:
            raise RuntimeError(
                "Universe is empty — run the S&P 500 membership checkpoint, or provide "
                "data/cache/insider_extra_universe.csv"
            )

        # 1. ingest
        if skip_ingest:
            result["ingest"] = {"skipped": True}
        else:
            cik_map = univ.ticker_cik_map(tickers)
            result["ingest"] = ingest.ingest_incremental(
                lookback_days=INGEST_LOOKBACK_DAYS,
                ciks=set(cik_map.values()) or None,
                config=conf,
            )

        # 2. score — only the recent window needs recomputing
        score_start = today - timedelta(days=conf.cluster_window_days + INGEST_LOOKBACK_DAYS)
        scores = scoring.compute_scores(score_start, today, config=conf, tickers=tickers)
        result["scores_computed"] = int(len(scores))
        if not scores.empty:
            scoring.save_scores(scores, conf)

        # 3. raise signals
        qual = scoring.qualifying_signals(scores, conf)
        result["new_signals"] = _raise_new_signals(qual, conf, score_start)

        # 4. confirmations
        confirmed, expired = _check_confirmations(conf, today)
        result["triggers_fired"] = confirmed
        result["signals_expired"] = expired

        # 5. positions
        result["exits_recorded"] = _update_positions(conf, today)

        latest = ingest.last_ingested_date()
        result["latest_filing_date"] = latest.isoformat() if latest else None

        # Stale-data guard: EDGAR should have filings from the last few business
        # days at all times.  Silence usually means a broken fetch, not a quiet
        # market, and it should be visible rather than looking like "no signals".
        if latest and (today - latest).days > 5:
            status = "stale_data"
            logger.warning("Newest stored filing is %s — %d days old. Check the ingest path.",
                           latest, (today - latest).days)

    except Exception as exc:      # noqa: BLE001
        status = "error"
        error = str(exc)
        logger.exception("Insider scan failed")

    with session_scope() as sess:
        run = sess.get(InsiderScanRun, run_id)
        if run is not None:
            run.end_time = _now()
            run.status = status
            run.universe_scanned = result.get("universe")
            run.filings_ingested = (result.get("ingest") or {}).get("filings_inserted")
            run.scores_computed = result.get("scores_computed")
            run.new_signals = result.get("new_signals", 0)
            run.triggers_fired = result.get("triggers_fired", 0)
            run.signals_expired = result.get("signals_expired", 0)
            run.exits_recorded = result.get("exits_recorded", 0)
            run.latest_filing_date = result.get("latest_filing_date")
            run.error_message = error

    result["status"] = status
    result["error"] = error
    logger.info("Insider scan complete: %s", json.dumps(result, default=str))
    return result


# ──────────────────────────────────────────────────────────────────────────────
#  Service
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        lambda: run_scan("scheduled"),
        CronTrigger(day_of_week="mon-fri", hour=SCAN_HOUR_UTC, minute=SCAN_MINUTE_UTC),
        name="insider_daily_scan",
        misfire_grace_time=3600,
    )
    logger.info("Insider scanner started — daily at %02d:%02d UTC, Mon-Fri",
                SCAN_HOUR_UTC, SCAN_MINUTE_UTC)
    sched.start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="InsiderSwing live scanner")
    ap.add_argument("--run-now", action="store_true", help="run one scan and exit")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="skip the EDGAR pull (score/confirm/positions only)")
    args = ap.parse_args()

    if args.run_now:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
                            datefmt="%H:%M:%S")
        print(json.dumps(run_scan("manual", skip_ingest=args.skip_ingest),
                         indent=2, default=str))
    else:
        main()
