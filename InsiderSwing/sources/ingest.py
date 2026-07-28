"""
InsiderSwing — ingestion orchestrator.

Pulls FilingRecords from whichever source is configured, classifies each
transaction line, and upserts into ins_filings / ins_transactions.

Properties that matter
----------------------
* Idempotent.  Uniqueness constraints on (accession, reporting_cik) and
  (accession, reporting_cik, line_no) mean re-running a backfill inserts
  nothing new.  Combined with the EDGAR disk cache, a re-run is both free and
  side-effect-free.
* Streaming.  Records are consumed from a generator and flushed in batches, so
  a decade-wide backfill never holds the corpus in memory.
* Resumable.  Every run is logged to ins_ingest_runs; ``last_ingested_date()``
  lets the live scanner ask "what's the newest filing date I already have?" and
  pull only forward from there.
* Loud about failure.  Fetch failures are counted and stored, never swallowed.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_ROOT = _PKG.parent
for _p in (str(_ROOT), str(_PKG), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import text                                   # noqa: E402

import config as cfg                                          # noqa: E402
import filters                                                # noqa: E402
from base import FilingRecord                                 # noqa: E402
from db import get_engine, session_scope                      # noqa: E402
from models import InsiderFiling, InsiderIngestRun, InsiderTransaction   # noqa: E402

logger = logging.getLogger(__name__)

_BATCH = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
#  Source selection
# ──────────────────────────────────────────────────────────────────────────────

def make_source(name: str = "edgar", **kwargs):
    """
    Resolve a source by name: 'edgar' | 'fmp' | 'auto'.

    'auto' prefers FMP when the key works against the per-symbol SEARCH endpoint
    (not merely the 'latest' feed), and falls back to EDGAR otherwise.  The
    fallback is logged at WARNING because the two sources differ in what
    10b5-1 information they carry — a silent switch would change the meaning of
    the filtered dataset.
    """
    from edgar_source import EdgarSource
    from fmp_source import FmpSource

    name = (name or "edgar").lower()
    if name == "edgar":
        return EdgarSource(**kwargs)
    if name == "fmp":
        return FmpSource(**kwargs)
    if name == "auto":
        fmp = FmpSource(**kwargs)
        if fmp.is_available():
            logger.info("Source 'auto' resolved to FMP.")
            return fmp
        logger.warning(
            "Source 'auto': FMP unavailable (no key, or plan-gated on the "
            "per-symbol endpoint) — falling back to SEC EDGAR. EDGAR is the "
            "richer source: it carries the Rule 10b5-1 checkbox FMP omits."
        )
        return EdgarSource(**kwargs)
    raise ValueError(f"Unknown insider data source: {name!r}")


# ──────────────────────────────────────────────────────────────────────────────
#  Queries
# ──────────────────────────────────────────────────────────────────────────────

def last_ingested_date() -> Optional[date]:
    """Newest filing_date already stored, or None on an empty DB."""
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT MAX(filing_date) FROM ins_filings")).fetchone()
    if row and row[0]:
        try:
            return datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def ingest_summary() -> dict:
    """Row counts and coverage — surfaced in the dashboard's data-health panel."""
    with get_engine().connect() as conn:
        def _scalar(sql: str):
            r = conn.execute(text(sql)).fetchone()
            return r[0] if r else None

        return {
            "filings": _scalar("SELECT COUNT(*) FROM ins_filings") or 0,
            "transactions": _scalar("SELECT COUNT(*) FROM ins_transactions") or 0,
            "open_market_buys": _scalar(
                "SELECT COUNT(*) FROM ins_transactions WHERE classification='open_market_buy'"
            ) or 0,
            "open_market_sales": _scalar(
                "SELECT COUNT(*) FROM ins_transactions WHERE classification='open_market_sale'"
            ) or 0,
            "distinct_tickers": _scalar(
                "SELECT COUNT(DISTINCT ticker) FROM ins_transactions WHERE ticker IS NOT NULL"
            ) or 0,
            "first_filing_date": _scalar("SELECT MIN(filing_date) FROM ins_filings"),
            "last_filing_date": _scalar("SELECT MAX(filing_date) FROM ins_filings"),
            "plan_flag_known": _scalar(
                "SELECT COUNT(*) FROM ins_filings WHERE aff_10b5_one IS NOT NULL"
            ) or 0,
        }


# ──────────────────────────────────────────────────────────────────────────────
#  Persistence
# ──────────────────────────────────────────────────────────────────────────────

def _existing_keys(accessions: list[str]) -> set[tuple[str, str]]:
    """(accession, reporting_cik) pairs already stored, for the given batch."""
    if not accessions:
        return set()
    out: set[tuple[str, str]] = set()
    with get_engine().connect() as conn:
        for i in range(0, len(accessions), 500):
            chunk = accessions[i:i + 500]
            placeholders = ",".join(f":a{j}" for j in range(len(chunk)))
            params = {f"a{j}": v for j, v in enumerate(chunk)}
            rows = conn.execute(
                text(f"SELECT accession_no, reporting_cik FROM ins_filings "
                     f"WHERE accession_no IN ({placeholders})"),
                params,
            ).fetchall()
            out.update((r[0], r[1] or "") for r in rows)
    return out


def _flush(records: list[FilingRecord], exclude_unknown_plan: bool) -> tuple[int, int]:
    """Insert a batch, skipping rows already present.  Returns (filings, txns)."""
    if not records:
        return 0, 0

    existing = _existing_keys([r.accession_no for r in records])
    n_filings = n_txns = 0

    with session_scope() as sess:
        for rec in records:
            key = (rec.accession_no, rec.reporting_cik or "")
            if key in existing:
                continue
            existing.add(key)

            filing = InsiderFiling(
                accession_no=rec.accession_no,
                source=rec.source,
                form_type=rec.form_type,
                issuer_cik=rec.issuer_cik,
                issuer_name=rec.issuer_name,
                ticker=(rec.ticker or None),
                reporting_cik=rec.reporting_cik,
                reporting_name=rec.reporting_name,
                is_director=rec.is_director,
                is_officer=rec.is_officer,
                is_ten_percent=rec.is_ten_percent,
                is_other=rec.is_other,
                officer_title=rec.officer_title,
                period_of_report=rec.period_of_report,
                filing_date=rec.filing_date,
                acceptance_datetime=rec.acceptance_datetime,
                aff_10b5_one=rec.aff_10b5_one,
                footnotes=(rec.footnotes[:20000] if rec.footnotes else None),
                url=rec.url,
            )
            sess.add(filing)
            sess.flush()          # need filing.id for the soft FK
            n_filings += 1

            for txn in rec.transactions:
                classification, reason = filters.classify_record(
                    txn, exclude_unknown_10b5_1=exclude_unknown_plan
                )
                sess.add(InsiderTransaction(
                    filing_id=filing.id,
                    accession_no=txn.accession_no,
                    line_no=txn.line_no,
                    ticker=(txn.ticker or None),
                    issuer_cik=txn.issuer_cik,
                    reporting_cik=txn.reporting_cik,
                    reporting_name=txn.reporting_name,
                    is_director=txn.is_director,
                    is_officer=txn.is_officer,
                    is_ten_percent=txn.is_ten_percent,
                    officer_title=txn.officer_title,
                    role_bucket=txn.role_bucket,
                    security_title=txn.security_title,
                    is_derivative=txn.is_derivative,
                    transaction_date=txn.transaction_date,
                    filing_date=txn.filing_date,
                    transaction_code=txn.transaction_code,
                    acquired_disposed=txn.acquired_disposed,
                    shares=txn.shares,
                    price_per_share=txn.price_per_share,
                    value_usd=txn.value_usd,
                    shares_owned_after=txn.shares_owned_after,
                    direct_or_indirect=txn.direct_or_indirect,
                    rule_10b5_1=txn.rule_10b5_1,
                    classification=classification,
                    exclude_reason=reason,
                    source=txn.source,
                ))
                n_txns += 1

    return n_filings, n_txns


# ──────────────────────────────────────────────────────────────────────────────
#  Entry points
# ──────────────────────────────────────────────────────────────────────────────

def ingest_range(
    start: date,
    end: date,
    tickers: Optional[set[str]] = None,
    ciks: Optional[set[str]] = None,
    source_name: Optional[str] = None,
    mode: str = "backfill",
    config: Optional["cfg.InsiderConfig"] = None,
    progress_cb=None,
) -> dict:
    """
    Ingest every Form 4 with a FILING date in [start, end].

    Note the window is on filing_date, not transaction_date.  A trade executed
    in December and filed in January belongs to January as far as this system is
    concerned, because January is when a trader could have known about it.
    """
    conf = config or cfg.DEFAULT_CONFIG
    src_name = source_name or conf.preferred_source
    cfg.ensure_dirs()

    source = make_source(src_name)

    logger.info(
        "Ingest start: source=%s mode=%s window=%s→%s tickers=%s ciks=%s",
        source.name, mode, start, end,
        len(tickers) if tickers else "all", len(ciks) if ciks else "all",
    )

    with session_scope() as sess:
        run = InsiderIngestRun(
            source=source.name, mode=mode,
            start_date=str(start), end_date=str(end),
            started_at=_now(), status="running",
        )
        sess.add(run)
        sess.flush()
        run_id = run.id

    seen = inserted_f = inserted_t = 0
    batch: list[FilingRecord] = []
    status = "ok"
    error_message = None

    try:
        for rec in source.fetch_range(start, end, tickers=tickers, ciks=ciks,
                                      progress_cb=progress_cb):
            if not rec.filing_date:
                continue     # point-in-time contract: no filing date, no record
            seen += 1
            batch.append(rec)
            if len(batch) >= _BATCH:
                f, t = _flush(batch, conf.exclude_unknown_10b5_1)
                inserted_f += f
                inserted_t += t
                batch.clear()

        if batch:
            f, t = _flush(batch, conf.exclude_unknown_10b5_1)
            inserted_f += f
            inserted_t += t

    except Exception as exc:                     # noqa: BLE001
        status = "error"
        error_message = str(exc)
        logger.exception("Ingest failed")

    failures = list(getattr(source, "fetch_failures", []) or [])
    if failures and status == "ok":
        status = "partial"

    with session_scope() as sess:
        run = sess.get(InsiderIngestRun, run_id)
        if run is not None:
            run.finished_at = _now()
            run.status = status
            run.filings_seen = seen
            run.filings_inserted = inserted_f
            run.txns_inserted = inserted_t
            run.fetch_failures = len(failures)
            run.failure_detail = json.dumps(failures[:200])
            run.error_message = error_message

    result = {
        "run_id": run_id,
        "source": source.name,
        "status": status,
        "filings_seen": seen,
        "filings_inserted": inserted_f,
        "transactions_inserted": inserted_t,
        "fetch_failures": len(failures),
        "error": error_message,
    }
    logger.info("Ingest done: %s", result)
    return result


def ingest_incremental(
    lookback_days: int = 7,
    tickers: Optional[set[str]] = None,
    ciks: Optional[set[str]] = None,
    source_name: Optional[str] = None,
    config: Optional["cfg.InsiderConfig"] = None,
) -> dict:
    """
    Daily top-up for the live scanner.

    Starts from the newest stored filing date minus ``lookback_days``.  The
    overlap is deliberate: Form 4/A amendments and late-accepted filings can
    appear with a back-dated filing date, and re-ingesting a week is free
    thanks to the uniqueness constraints.
    """
    last = last_ingested_date()
    today = datetime.now(timezone.utc).date()
    start = (last - timedelta(days=lookback_days)) if last else (today - timedelta(days=30))
    return ingest_range(
        start=start, end=today, tickers=tickers, ciks=ciks,
        source_name=source_name, mode="incremental", config=config,
    )
