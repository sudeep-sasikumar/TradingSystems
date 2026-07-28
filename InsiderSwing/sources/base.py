"""
InsiderSwing — data source interface.

Both FMP and SEC EDGAR implementations emit the same two dataclasses, so the
ingest orchestrator, filters, and scoring layers never learn which source they
are looking at.

THE POINT-IN-TIME CONTRACT
--------------------------
``FilingRecord.filing_date`` is when the disclosure became public.  It is the
only date any downstream signal logic is allowed to key on.
``TransactionRecord.transaction_date`` is when the insider actually traded —
useful for analysis (execution-to-disclosure lag), but unknowable to a trader
at the time, so using it for signal timing manufactures lookahead.

Sources MUST populate filing_date.  A record without one is dropped at ingest.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent           # InsiderSwing/
_ROOT = _PKG.parent           # project root
for _p in (str(_ROOT), str(_PKG), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Role bucketing ────────────────────────────────────────────────────────────

CEO_CFO_KEYWORDS = (
    "chief executive", "ceo",
    "chief financial", "cfo",
    "president and chief executive", "principal executive officer",
    "principal financial officer",
)


def classify_role(
    is_officer: bool,
    is_director: bool,
    is_ten_percent: bool,
    officer_title: Optional[str],
) -> str:
    """
    Map Form 4 relationship flags to a role bucket.

    Ordering is deliberate: an insider who is BOTH a director and the CEO is
    bucketed as ceo_cfo, because the CEO hat is the one that carries the
    information.  10%-owner is checked last and only when the filer holds no
    officer/director role — pure 10%-owners are frequently funds rebalancing.
    """
    title = (officer_title or "").strip().lower()

    if is_officer and any(k in title for k in CEO_CFO_KEYWORDS):
        return "ceo_cfo"
    if is_officer:
        return "officer"
    if is_director:
        return "director"
    if is_ten_percent:
        return "ten_pct"
    return "other"


# ── Records ───────────────────────────────────────────────────────────────────

@dataclass
class TransactionRecord:
    """One transaction line from a Form 4."""
    accession_no: str
    line_no: int

    ticker: Optional[str]
    issuer_cik: Optional[str]
    reporting_cik: Optional[str]
    reporting_name: Optional[str]

    is_director: bool
    is_officer: bool
    is_ten_percent: bool
    officer_title: Optional[str]

    security_title: Optional[str]
    is_derivative: bool

    transaction_date: Optional[str]   # YYYY-MM-DD — analysis only
    filing_date: str                  # YYYY-MM-DD — the ONLY signal-legal date

    transaction_code: Optional[str]
    acquired_disposed: Optional[str]
    shares: Optional[float]
    price_per_share: Optional[float]
    shares_owned_after: Optional[float]
    direct_or_indirect: Optional[str]

    rule_10b5_1: Optional[bool] = None    # None = unknown, NOT False
    source: str = "edgar"

    @property
    def role_bucket(self) -> str:
        return classify_role(
            self.is_officer, self.is_director, self.is_ten_percent, self.officer_title
        )

    @property
    def value_usd(self) -> Optional[float]:
        if self.shares is None or self.price_per_share is None:
            return None
        return float(self.shares) * float(self.price_per_share)


@dataclass
class FilingRecord:
    """One Form 4 accession, for one reporting owner, plus its transaction lines."""
    accession_no: str
    source: str

    issuer_cik: Optional[str]
    issuer_name: Optional[str]
    ticker: Optional[str]

    reporting_cik: Optional[str]
    reporting_name: Optional[str]

    is_director: bool
    is_officer: bool
    is_ten_percent: bool
    is_other: bool
    officer_title: Optional[str]

    period_of_report: Optional[str]
    filing_date: str
    acceptance_datetime: Optional[str] = None

    aff_10b5_one: Optional[bool] = None
    footnotes: Optional[str] = None
    url: Optional[str] = None
    form_type: str = "4"

    transactions: list[TransactionRecord] = field(default_factory=list)


# ── Interface ─────────────────────────────────────────────────────────────────

class InsiderDataSource(ABC):
    """Interface every insider data source implements."""

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap liveness/authorisation probe.  Must not raise."""

    @abstractmethod
    def fetch_range(
        self,
        start: date,
        end: date,
        tickers: Optional[set[str]] = None,
        ciks: Optional[set[str]] = None,
        progress_cb=None,
    ) -> Iterable[FilingRecord]:
        """
        Yield FilingRecords whose FILING date falls in [start, end].

        tickers / ciks narrow the pull to a universe.  A source that can only
        filter on one of them should filter on what it can and let the ingest
        layer drop the rest — over-fetching is fine, under-fetching is not.

        Implementations yield lazily so a multi-year backfill streams to the DB
        instead of accumulating in memory.
        """

    def describe(self) -> str:
        return self.name
