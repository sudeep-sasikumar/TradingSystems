"""
InsiderSwing — Financial Modeling Prep insider-trade source.

KNOWN LIMITATIONS — read before relying on this source
------------------------------------------------------
1. PLAN GATING.  On free/lower tiers ``latest-insider-trade`` works but the
   per-symbol history endpoints (``search-insider-trades``) return HTTP 403.
   That makes FMP unusable for a historical backfill of exactly the small/mid
   caps this system targets.  ``is_available()`` probes the *search* endpoint,
   not the *latest* one, so 'auto' source selection fails over to EDGAR rather
   than silently ingesting a large-cap-only sample.

2. NO 10b5-1 FLAG.  FMP's payload has no field for the Rule 10b5-1 checkbox.
   Everything ingested through this source carries ``rule_10b5_1 = None``
   (unknown) — a distinct third state from True/False.  filters.py keeps
   unknowns by default (dropping them would delete all pre-2022 history) and
   the coverage gap is reported in the backtest limitations section.  Set
   INSIDER_EXCLUDE_UNKNOWN_10B5_1=1 to drop them instead.  If you want real
   plan filtering, use EDGAR.

3. NO FOOTNOTES.  The pre-2022 10b5-1 keyword fallback is unavailable here too.

FMP is therefore positioned as a convenience source for a low-latency daily
incremental pull on a paid plan, not as the system of record.

Endpoint reference: https://site.financialmodelingprep.com/developer/docs
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

import requests

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent
_ROOT = _PKG.parent
for _p in (str(_ROOT), str(_PKG), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg                                          # noqa: E402
from base import FilingRecord, InsiderDataSource, TransactionRecord   # noqa: E402

logger = logging.getLogger(__name__)


class FmpSource(InsiderDataSource):

    name = "fmp"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 page_size: int = 100, max_pages: int = 500):
        self.api_key = api_key if api_key is not None else cfg.FMP_API_KEY
        self.base_url = (base_url or cfg.FMP_BASE_URL).rstrip("/")
        self.page_size = page_size
        self.max_pages = max_pages
        self._session = requests.Session()
        self.fetch_failures: list[dict] = []
        self._gated = False

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict, retries: int = 3) -> Optional[list[dict]]:
        if not self.api_key:
            return None
        q = dict(params)
        q["apikey"] = self.api_key
        url = f"{self.base_url}/{endpoint}"

        for attempt in range(retries):
            try:
                resp = self._session.get(url, params=q, timeout=30)
                if resp.status_code in (401, 402, 403):
                    # Plan gating — record once, loudly, and stop hammering it.
                    if not self._gated:
                        logger.warning(
                            "FMP endpoint %s returned HTTP %d (plan-gated or bad key). "
                            "Falling back to SEC EDGAR is the supported path.",
                            endpoint, resp.status_code,
                        )
                        self._gated = True
                    self.fetch_failures.append({"url": endpoint, "reason": f"HTTP {resp.status_code}"})
                    return None
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    # FMP returns {"Error Message": ...} on failure
                    logger.warning("FMP %s error: %s", endpoint, data)
                    return None
                return data
            except Exception as exc:
                if attempt == retries - 1:
                    logger.warning("FMP GET failed (%s): %s", endpoint, exc)
                    self.fetch_failures.append({"url": endpoint, "reason": str(exc)})
                    return None
                time.sleep(2 ** attempt)
        return None

    def is_available(self) -> bool:
        """
        Probe the SEARCH endpoint, not 'latest'.  A plan that only serves
        'latest-insider-trade' cannot backfill history, and treating it as
        available would produce a silently truncated dataset.
        """
        if not self.api_key:
            return False
        rows = self._get("insider-trading/search", {"symbol": "AAPL", "limit": 1})
        return rows is not None

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_record(row: dict) -> Optional[FilingRecord]:
        filing_date = str(row.get("filingDate") or "")[:10]
        if not filing_date:
            return None      # no filing date = unusable; point-in-time contract

        type_of_owner = str(row.get("typeOfOwner") or "").lower()
        is_director = "director" in type_of_owner
        is_officer = "officer" in type_of_owner
        is_ten_pct = "10 percent" in type_of_owner or "10%" in type_of_owner

        officer_title = None
        if "officer:" in type_of_owner:
            officer_title = type_of_owner.split("officer:", 1)[1].strip()

        acc = str(row.get("accessionNumber") or "").strip()
        if not acc:
            # FMP does not always return it; synthesise a stable surrogate so the
            # uniqueness constraint still de-duplicates repeated pulls.
            acc = "FMP-{}-{}-{}-{}".format(
                row.get("symbol"), row.get("reportingCik"),
                row.get("transactionDate"), row.get("securitiesTransacted"),
            )

        ticker = str(row.get("symbol") or "").upper() or None
        shares = row.get("securitiesTransacted")
        price = row.get("price")

        txn = TransactionRecord(
            accession_no=acc,
            line_no=0,
            ticker=ticker,
            issuer_cik=str(row.get("companyCik") or "").zfill(10) or None,
            reporting_cik=str(row.get("reportingCik") or "").zfill(10) or None,
            reporting_name=row.get("reportingName"),
            is_director=is_director,
            is_officer=is_officer,
            is_ten_percent=is_ten_pct,
            officer_title=officer_title,
            security_title=row.get("securityName"),
            # FMP flattens derivative and non-derivative tables together and has
            # no explicit marker; "option"/"warrant"/"right" in the security name
            # is the best available proxy.
            is_derivative=any(
                k in str(row.get("securityName") or "").lower()
                for k in ("option", "warrant", "right", "unit", "rsu", "phantom")
            ),
            transaction_date=str(row.get("transactionDate") or "")[:10] or None,
            filing_date=filing_date,
            transaction_code=row.get("transactionType"),
            acquired_disposed=row.get("acquisitionOrDisposition"),
            shares=float(shares) if shares not in (None, "") else None,
            price_per_share=float(price) if price not in (None, "") else None,
            shares_owned_after=(
                float(row["securitiesOwned"]) if row.get("securitiesOwned") not in (None, "") else None
            ),
            direct_or_indirect=row.get("directOrIndirect"),
            rule_10b5_1=None,      # FMP does not expose it — unknown, not False
            source="fmp",
        )

        return FilingRecord(
            accession_no=acc,
            source="fmp",
            issuer_cik=txn.issuer_cik,
            issuer_name=None,
            ticker=ticker,
            reporting_cik=txn.reporting_cik,
            reporting_name=txn.reporting_name,
            is_director=is_director,
            is_officer=is_officer,
            is_ten_percent=is_ten_pct,
            is_other=False,
            officer_title=officer_title,
            period_of_report=txn.transaction_date,
            filing_date=filing_date,
            acceptance_datetime=None,
            aff_10b5_one=None,
            footnotes=None,
            url=row.get("url"),
            form_type=str(row.get("formType") or "4"),
            transactions=[txn],
        )

    # ── Public fetch ──────────────────────────────────────────────────────────

    def fetch_range(
        self,
        start: date,
        end: date,
        tickers: Optional[set[str]] = None,
        ciks: Optional[set[str]] = None,
        progress_cb=None,
    ) -> Iterator[FilingRecord]:
        """
        Per-symbol history when a ticker universe is given; otherwise page
        through the market-wide 'latest' feed until it predates ``start``.

        FMP returns one row per transaction line rather than per filing, so each
        row becomes a single-transaction FilingRecord.  The DB uniqueness
        constraint on (accession, reporting_cik, line_no) merges them back.
        """
        want_start, want_end = str(start), str(end)

        if tickers:
            symbols = sorted({t.upper() for t in tickers})
            for n, sym in enumerate(symbols, 1):
                for page in range(self.max_pages):
                    rows = self._get("insider-trading/search",
                                     {"symbol": sym, "page": page, "limit": self.page_size})
                    if self._gated:
                        return
                    if not rows:
                        break
                    oldest = None
                    for row in rows:
                        rec = self._to_record(row)
                        if rec is None:
                            continue
                        oldest = rec.filing_date if oldest is None else min(oldest, rec.filing_date)
                        if want_start <= rec.filing_date <= want_end:
                            yield rec
                    if oldest is not None and oldest < want_start:
                        break
                if progress_cb and n % 25 == 0:
                    progress_cb("fetch", n, len(symbols))
            return

        for page in range(self.max_pages):
            rows = self._get("insider-trading/latest", {"page": page, "limit": self.page_size})
            if self._gated or not rows:
                return
            oldest = None
            for row in rows:
                rec = self._to_record(row)
                if rec is None:
                    continue
                oldest = rec.filing_date if oldest is None else min(oldest, rec.filing_date)
                if want_start <= rec.filing_date <= want_end:
                    yield rec
            if oldest is not None and oldest < want_start:
                return
            if progress_cb and page % 10 == 0:
                progress_cb("fetch", page, self.max_pages)
