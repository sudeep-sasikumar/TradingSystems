"""
InsiderSwing — SEC EDGAR data source (the fallback that is really the primary).

Why EDGAR is the default
------------------------
1. Free and complete.  No plan gating: the FMP per-symbol insider endpoints
   return 403 on lower tiers, which makes a multi-year historical backfill
   impossible through FMP for exactly the small/mid caps this system targets.
2. It carries the Rule 10b5-1 checkbox (``<aff10b5One>``, added to Form 4 by
   the SEC's 2022 amendments).  FMP does not expose it at all.  Pre-scheduled
   plan sales/purchases are mechanical, not discretionary, and dropping them is
   a core part of the noise filter.
3. It is the source of record.  Every other vendor is a re-publication of it.

Two discovery paths
-------------------
``per-issuer``  data.sec.gov/submissions/CIK##########.json — targeted, cheap
                (a handful of requests per issuer covering its whole history).
                Used for historical backfill over a known universe.

``daily-index`` www.sec.gov/Archives/edgar/daily-index/{yr}/QTR{q}/form.{date}.idx
                — one small file per trading day listing every filing that day.
                Used by the live scanner for the incremental daily pull.

Rate limiting
-------------
SEC's published ceiling is 10 requests/second with a descriptive User-Agent.
``_RateLimiter`` enforces a configurable rate (default 8/s) across all threads,
and every fetched document is cached to disk gzipped, so a re-run of a backfill
costs zero network requests.

See https://www.sec.gov/os/accessing-edgar-data
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional

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

_SEC_ARCHIVES = "https://www.sec.gov/Archives"
_SEC_DATA = "https://data.sec.gov"
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


# ──────────────────────────────────────────────────────────────────────────────
#  Rate limiting
# ──────────────────────────────────────────────────────────────────────────────

class _RateLimiter:
    """Process-wide minimum-interval limiter, safe across threads."""

    def __init__(self, rate_per_sec: float):
        self._min_interval = 1.0 / max(rate_per_sec, 0.1)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────

def pad_cik(cik) -> str:
    """EDGAR wants zero-padded 10-digit CIKs in submissions URLs."""
    s = re.sub(r"\D", "", str(cik or ""))
    return s.zfill(10) if s else ""


def _strip_cik(cik) -> str:
    """Un-padded CIK, as used in Archives paths."""
    return re.sub(r"\D", "", str(cik or "")).lstrip("0") or "0"


def _txt(node: Optional[ET.Element]) -> Optional[str]:
    if node is None:
        return None
    # Form 4 wraps most scalars in a <value> child.
    val = node.find("value")
    raw = (val.text if val is not None else node.text) or ""
    raw = raw.strip()
    return raw or None


def _num(node: Optional[ET.Element]) -> Optional[float]:
    s = _txt(node)
    if s is None:
        return None
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _flag(node: Optional[ET.Element]) -> bool:
    s = _txt(node)
    return bool(s) and s.strip().lower() in ("1", "true", "y", "yes")


def _tri_flag(node: Optional[ET.Element]) -> Optional[bool]:
    """True/False when the element exists, None when it is absent entirely.

    The distinction matters: a Form 4 filed before the 2022 amendments has no
    10b5-1 checkbox at all, and treating that absence as "not a plan" would
    silently mis-filter a decade of history.
    """
    if node is None:
        return None
    s = _txt(node)
    if s is None:
        return None
    return s.strip().lower() in ("1", "true", "y", "yes")


# ──────────────────────────────────────────────────────────────────────────────
#  Source
# ──────────────────────────────────────────────────────────────────────────────

class EdgarSource(InsiderDataSource):

    name = "edgar"

    def __init__(
        self,
        user_agent: Optional[str] = None,
        rate_limit: Optional[float] = None,
        cache_dir: Optional[Path] = None,
        max_workers: int = 4,
    ):
        self.user_agent = user_agent or cfg.SEC_USER_AGENT
        self.limiter = _RateLimiter(rate_limit or cfg.SEC_RATE_LIMIT)
        self.cache_dir = Path(cache_dir or cfg.EDGAR_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self._ticker_map: Optional[dict[str, str]] = None
        self.fetch_failures: list[dict] = []

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get(self, url: str, *, cache_name: Optional[str] = None, retries: int = 4) -> Optional[str]:
        """
        GET with rate limiting, retry/backoff, and optional gzipped disk cache.

        Returns None (and records a fetch failure) rather than raising — one bad
        document must never abort a backfill of hundreds of thousands.
        """
        cache_path = self.cache_dir / f"{cache_name}.gz" if cache_name else None
        if cache_path is not None and cache_path.exists():
            try:
                with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
                    return fh.read()
            except Exception as exc:
                logger.debug("Cache read failed (%s): %s — refetching", cache_path.name, exc)

        headers = {"Host": "data.sec.gov"} if url.startswith(_SEC_DATA) else {"Host": "www.sec.gov"}

        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            self.limiter.acquire()
            try:
                resp = self._session.get(url, headers=headers, timeout=30)
                if resp.status_code == 404:
                    self.fetch_failures.append({"url": url, "reason": "404"})
                    return None
                if resp.status_code in (403, 429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                text = resp.text
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with gzip.open(cache_path, "wt", encoding="utf-8") as fh:
                            fh.write(text)
                    except Exception as exc:
                        logger.debug("Cache write failed for %s: %s", cache_path.name, exc)
                return text
            except Exception as exc:
                last_exc = exc
                sleep_s = min(2 ** attempt, 16)
                logger.debug("EDGAR GET failed (%s) attempt %d/%d: %s — retry in %.0fs",
                             url, attempt + 1, retries, exc, sleep_s)
                time.sleep(sleep_s)

        logger.warning("EDGAR GET gave up: %s (%s)", url, last_exc)
        self.fetch_failures.append({"url": url, "reason": str(last_exc)})
        return None

    def is_available(self) -> bool:
        try:
            return self._get(f"{_SEC_DATA}/submissions/CIK0000320193.json",
                             cache_name="probe/CIK0000320193") is not None
        except Exception:
            return False

    # ── CIK ↔ ticker ──────────────────────────────────────────────────────────

    def ticker_to_cik(self) -> dict[str, str]:
        """SEC's own ticker→CIK map.  Cached to disk; refreshed weekly."""
        if self._ticker_map is not None:
            return self._ticker_map

        path = self.cache_dir / "company_tickers.json"
        raw: Optional[str] = None
        if path.exists():
            age_days = (time.time() - path.stat().st_mtime) / 86400
            if age_days < 7:
                raw = path.read_text(encoding="utf-8")

        if raw is None:
            raw = self._get(_COMPANY_TICKERS_URL)
            if raw is not None:
                path.write_text(raw, encoding="utf-8")
            elif path.exists():
                logger.warning("company_tickers.json fetch failed — using stale cache")
                raw = path.read_text(encoding="utf-8")

        mapping: dict[str, str] = {}
        if raw:
            try:
                data = json.loads(raw)
                for row in data.values():
                    t = str(row.get("ticker", "")).upper().strip()
                    if t:
                        mapping[t] = pad_cik(row.get("cik_str"))
            except Exception as exc:
                logger.error("Failed to parse company_tickers.json: %s", exc)

        self._ticker_map = mapping
        logger.info("SEC ticker→CIK map: %d symbols", len(mapping))
        return mapping

    def resolve_cik(self, ticker: str) -> Optional[str]:
        """
        Resolve one ticker to a CIK, trying progressively harder.

        company_tickers.json is the fast path but it is incomplete — it omits a
        surprising number of live large caps (BK, CMA, ANSS at time of writing)
        and uses dashes for share classes where index lists use dots.  So:

          1. exact match in company_tickers.json
          2. dot→dash normalisation (BRK.B → BRK-B)
          3. EDGAR's browse-edgar atom endpoint, which resolves tickers the
             bulk file misses (one cached request per unresolved ticker)
        """
        t = str(ticker).upper().strip()
        if not t:
            return None

        table = self.ticker_to_cik()
        for candidate in (t, t.replace(".", "-"), t.replace("-", ".")):
            if candidate in table:
                return table[candidate]

        raw = self._get(
            "https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={t}&type=4&dateb=&owner=include&count=1&output=atom",
            cache_name=f"ticker-lookup/{t.replace('.', '_')}",
        )
        if not raw:
            return None
        m = re.search(r"<cik>\s*(\d+)\s*</cik>", raw, re.IGNORECASE)
        if not m:
            return None
        cik = pad_cik(m.group(1))
        self._ticker_map[t] = cik      # memoise for the rest of the process
        return cik

    # ── Discovery: per-issuer submissions JSON ────────────────────────────────

    def issuer_form4_index(self, cik: str, start: date, end: date) -> list[dict]:
        """
        List every Form 4 accession filed under an issuer CIK with a filing date
        in [start, end].  Walks the paginated older-submission files too, so this
        reaches back to the 1990s rather than the most recent 1,000 filings.
        """
        cik10 = pad_cik(cik)
        if not cik10:
            return []

        out: list[dict] = []
        seen: set[str] = set()

        def _harvest(block: dict) -> None:
            forms = block.get("form") or []
            for i, form in enumerate(forms):
                if form not in ("4", "4/A"):
                    continue
                fdate = (block.get("filingDate") or [None] * len(forms))[i]
                if not fdate or not (str(start) <= fdate <= str(end)):
                    continue
                acc = (block.get("accessionNumber") or [None] * len(forms))[i]
                if not acc or acc in seen:
                    continue
                seen.add(acc)
                out.append({
                    "accession_no": acc,
                    "filing_date": fdate,
                    "report_date": (block.get("reportDate") or [None] * len(forms))[i],
                    "acceptance_datetime": (block.get("acceptanceDateTime") or [None] * len(forms))[i],
                    "primary_document": (block.get("primaryDocument") or [None] * len(forms))[i],
                    "issuer_cik": cik10,
                    "form_type": form,
                })

        raw = self._get(f"{_SEC_DATA}/submissions/CIK{cik10}.json",
                        cache_name=f"submissions/CIK{cik10}")
        if raw is None:
            return []
        try:
            doc = json.loads(raw)
        except Exception as exc:
            logger.warning("Bad submissions JSON for CIK %s: %s", cik10, exc)
            return []

        _harvest(doc.get("filings", {}).get("recent", {}))

        # Older pages — only fetch those whose date range overlaps our window.
        for extra in doc.get("filings", {}).get("files", []) or []:
            f_from, f_to = extra.get("filingFrom", ""), extra.get("filingTo", "")
            if f_to and f_to < str(start):
                continue
            if f_from and f_from > str(end):
                continue
            name = extra.get("name")
            if not name:
                continue
            page = self._get(f"{_SEC_DATA}/submissions/{name}",
                             cache_name=f"submissions/{name.replace('.json', '')}")
            if page is None:
                continue
            try:
                _harvest(json.loads(page))
            except Exception as exc:
                logger.debug("Bad older submissions page %s: %s", name, exc)

        return out

    # ── Discovery: daily index ────────────────────────────────────────────────

    def daily_form4_index(self, day: date) -> list[dict]:
        """
        Every Form 4 filed on a single calendar day, from the EDGAR daily index.

        Returns [] for weekends/holidays (the index file simply doesn't exist).
        Rows are deduped by accession — the index lists a filing once per filer,
        so a Form 4 appears under both the issuer and each reporting owner.
        """
        qtr = (day.month - 1) // 3 + 1
        url = (f"{_SEC_ARCHIVES}/edgar/daily-index/{day.year}/QTR{qtr}/"
               f"form.{day.strftime('%Y%m%d')}.idx")
        raw = self._get(url, cache_name=f"daily-index/form.{day.strftime('%Y%m%d')}")
        if raw is None:
            return []

        rows: list[dict] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            if not line.startswith("4 ") and not line.startswith("4/A "):
                continue
            # Fixed-width; the file name is the last whitespace-delimited token.
            parts = line.split()
            fname = parts[-1] if parts else ""
            if "/" not in fname:
                continue
            m = re.search(r"/(\d+)/([\d-]+)\.txt$", fname)
            if not m:
                continue
            issuer_cik, acc = m.group(1), m.group(2)
            if acc in seen:
                continue
            seen.add(acc)
            rows.append({
                "accession_no": acc,
                "filing_date": day.isoformat(),
                "issuer_cik": pad_cik(issuer_cik),
                "primary_document": None,
                "acceptance_datetime": None,
                "report_date": None,
                "form_type": parts[0],
            })
        return rows

    # ── Document fetch ────────────────────────────────────────────────────────

    def fetch_form4_xml(self, issuer_cik: str, accession_no: str,
                        primary_document: Optional[str] = None) -> Optional[str]:
        """
        Fetch the ownership XML for one Form 4.

        Preferred path is the primary document named in submissions.json (a
        small XML file).  When that is unknown (daily-index path) we fall back
        to the full submission .txt and cut the <ownershipDocument> block out of
        it — one request either way.
        """
        acc_nodash = accession_no.replace("-", "")
        cik_bare = _strip_cik(issuer_cik)
        cache_key = f"form4/{acc_nodash[:6]}/{accession_no}"

        if primary_document:
            doc = primary_document.split("/")[-1]     # drop any xslF345X0n/ prefix
            url = f"{_SEC_ARCHIVES}/edgar/data/{cik_bare}/{acc_nodash}/{doc}"
            text = self._get(url, cache_name=cache_key)
            if text and "<ownershipDocument" in text:
                return text

        # Fallback: the complete submission text file.
        url = f"{_SEC_ARCHIVES}/edgar/data/{cik_bare}/{accession_no}.txt"
        text = self._get(url, cache_name=f"{cache_key}.sub")
        if not text:
            return None
        start = text.find("<ownershipDocument")
        end = text.find("</ownershipDocument>")
        if start == -1 or end == -1:
            self.fetch_failures.append({"url": url, "reason": "no ownershipDocument block"})
            return None
        return text[start:end + len("</ownershipDocument>")]

    # ── Parsing ───────────────────────────────────────────────────────────────

    def parse_form4(
        self,
        xml_text: str,
        accession_no: str,
        filing_date: str,
        acceptance_datetime: Optional[str] = None,
        url: Optional[str] = None,
        form_type: str = "4",
    ) -> list[FilingRecord]:
        """
        Parse one Form 4 XML into FilingRecords (one per reporting owner).

        Multi-owner Form 4s (joint filings by affiliated entities) attach the
        transaction lines to the FIRST owner only.  Attaching them to every
        owner would double-count the dollar value; the additional owners are
        still emitted as zero-transaction filings so the roster stays complete.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.debug("Form 4 XML parse error (%s): %s", accession_no, exc)
            self.fetch_failures.append({"url": url or accession_no, "reason": f"xml parse: {exc}"})
            return []

        issuer = root.find("issuer")
        issuer_cik = _txt(issuer.find("issuerCik")) if issuer is not None else None
        issuer_name = _txt(issuer.find("issuerName")) if issuer is not None else None
        ticker = _txt(issuer.find("issuerTradingSymbol")) if issuer is not None else None
        if ticker:
            ticker = ticker.upper().strip()
            if ticker in ("NONE", "N/A", "NA", "-"):
                ticker = None

        period = _txt(root.find("periodOfReport"))

        # ── 10b5-1 detection (three independent signals) ──────────────────────
        # 1. The document-level checkbox added by the 2022 Form 4 amendments.
        plan_flag = _tri_flag(root.find("aff10b5One"))
        # 2. Any transaction-level 10b5-1 element some filers use instead.
        if plan_flag is not True:
            for el in root.iter():
                if "10b5" in el.tag.lower() and _flag(el):
                    plan_flag = True
                    break
        # 3. Footnote free text — the only marker available pre-2022, and still
        #    the most common one.  Keyword match, deliberately narrow.
        footnotes = " ".join(
            (fn.text or "").strip()
            for fn in root.iter("footnote")
        ).strip() or None
        if footnotes and re.search(r"10b5[\s\-–—]?1", footnotes, re.IGNORECASE):
            plan_flag = True

        # ── Reporting owners ──────────────────────────────────────────────────
        owners: list[dict] = []
        for ro in root.findall("reportingOwner"):
            oid = ro.find("reportingOwnerId")
            rel = ro.find("reportingOwnerRelationship")
            owners.append({
                "cik": _txt(oid.find("rptOwnerCik")) if oid is not None else None,
                "name": _txt(oid.find("rptOwnerName")) if oid is not None else None,
                "is_director": _flag(rel.find("isDirector")) if rel is not None else False,
                "is_officer": _flag(rel.find("isOfficer")) if rel is not None else False,
                "is_ten_percent": _flag(rel.find("isTenPercentOwner")) if rel is not None else False,
                "is_other": _flag(rel.find("isOther")) if rel is not None else False,
                "officer_title": _txt(rel.find("officerTitle")) if rel is not None else None,
            })
        if not owners:
            return []

        primary = owners[0]

        # ── Transaction lines ─────────────────────────────────────────────────
        txns: list[TransactionRecord] = []

        def _emit(node: ET.Element, line_no: int, derivative: bool) -> None:
            coding = node.find("transactionCoding")
            amounts = node.find("transactionAmounts")
            post = node.find("postTransactionAmounts")
            nature = node.find("ownershipNature")

            code = _txt(coding.find("transactionCode")) if coding is not None else None
            shares = _num(amounts.find("transactionShares")) if amounts is not None else None
            price = _num(amounts.find("transactionPricePerShare")) if amounts is not None else None
            a_or_d = _txt(amounts.find("transactionAcquiredDisposedCode")) if amounts is not None else None

            txns.append(TransactionRecord(
                accession_no=accession_no,
                line_no=line_no,
                ticker=ticker,
                issuer_cik=pad_cik(issuer_cik),
                reporting_cik=pad_cik(primary["cik"]),
                reporting_name=primary["name"],
                is_director=primary["is_director"],
                is_officer=primary["is_officer"],
                is_ten_percent=primary["is_ten_percent"],
                officer_title=primary["officer_title"],
                security_title=_txt(node.find("securityTitle")),
                is_derivative=derivative,
                transaction_date=_txt(node.find("transactionDate")),
                filing_date=filing_date,
                transaction_code=code,
                acquired_disposed=a_or_d,
                shares=shares,
                price_per_share=price,
                shares_owned_after=(
                    _num(post.find("sharesOwnedFollowingTransaction")) if post is not None else None
                ),
                direct_or_indirect=(
                    _txt(nature.find("directOrIndirectOwnership")) if nature is not None else None
                ),
                rule_10b5_1=plan_flag,
                source="edgar",
            ))

        nd_table = root.find("nonDerivativeTable")
        if nd_table is not None:
            for i, node in enumerate(nd_table.findall("nonDerivativeTransaction")):
                _emit(node, i, derivative=False)

        d_table = root.find("derivativeTable")
        if d_table is not None:
            # Offset keeps line numbers unique within an accession so the
            # (accession, owner, line_no) uniqueness constraint holds.
            for i, node in enumerate(d_table.findall("derivativeTransaction")):
                _emit(node, 1000 + i, derivative=True)

        acc_nodash = accession_no.replace("-", "")
        default_url = (f"{_SEC_ARCHIVES}/edgar/data/{_strip_cik(issuer_cik)}/"
                       f"{acc_nodash}/{accession_no}-index.htm")

        records: list[FilingRecord] = []
        for idx, owner in enumerate(owners):
            records.append(FilingRecord(
                accession_no=accession_no,
                source="edgar",
                issuer_cik=pad_cik(issuer_cik),
                issuer_name=issuer_name,
                ticker=ticker,
                reporting_cik=pad_cik(owner["cik"]),
                reporting_name=owner["name"],
                is_director=owner["is_director"],
                is_officer=owner["is_officer"],
                is_ten_percent=owner["is_ten_percent"],
                is_other=owner["is_other"],
                officer_title=owner["officer_title"],
                period_of_report=period,
                filing_date=filing_date,
                acceptance_datetime=acceptance_datetime,
                aff_10b5_one=plan_flag,
                footnotes=footnotes,
                url=url or default_url,
                form_type=form_type,
                transactions=txns if idx == 0 else [],
            ))
        return records

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
        Yield FilingRecords with filing_date in [start, end].

        When ``ciks`` is supplied we discover per issuer (cheap, targeted).
        Otherwise we walk the daily index day by day — used by the live scanner,
        and by any run that wants the whole market rather than a universe.
        """
        if ciks:
            index_rows = self._discover_by_issuer(ciks, start, end, progress_cb)
        else:
            index_rows = self._discover_by_day(start, end, progress_cb)

        yield from self._fetch_and_parse(index_rows, tickers, progress_cb)

    def _discover_by_issuer(self, ciks: set[str], start: date, end: date,
                            progress_cb=None) -> list[dict]:
        rows: list[dict] = []
        cik_list = sorted({pad_cik(c) for c in ciks if pad_cik(c)})
        logger.info("EDGAR discovery: %d issuer CIKs, %s → %s", len(cik_list), start, end)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.issuer_form4_index, c, start, end): c for c in cik_list}
            for n, (fut, c) in enumerate(futures.items(), 1):
                try:
                    rows.extend(fut.result())
                except Exception as exc:
                    logger.warning("Discovery failed for CIK %s: %s", c, exc)
                    self.fetch_failures.append({"url": f"submissions/{c}", "reason": str(exc)})
                if progress_cb and n % 25 == 0:
                    progress_cb("discover", n, len(cik_list))

        logger.info("EDGAR discovery: %d Form 4 accessions found", len(rows))
        return rows

    def _discover_by_day(self, start: date, end: date, progress_cb=None) -> list[dict]:
        rows: list[dict] = []
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        days = [d for d in days if d.weekday() < 5]     # daily index is business-day only
        logger.info("EDGAR discovery: %d business days, %s → %s", len(days), start, end)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for n, day_rows in enumerate(pool.map(self.daily_form4_index, days), 1):
                rows.extend(day_rows)
                if progress_cb and n % 10 == 0:
                    progress_cb("discover", n, len(days))

        logger.info("EDGAR discovery: %d Form 4 accessions found", len(rows))
        return rows

    def _fetch_and_parse(self, index_rows: list[dict], tickers: Optional[set[str]],
                         progress_cb=None) -> Iterator[FilingRecord]:
        # Dedupe: the same accession can surface from several discovery rows.
        unique: dict[str, dict] = {}
        for r in index_rows:
            unique.setdefault(r["accession_no"], r)
        rows = sorted(unique.values(), key=lambda r: (r["filing_date"], r["accession_no"]))
        total = len(rows)
        logger.info("EDGAR fetch: %d unique Form 4 documents", total)

        want = {t.upper() for t in tickers} if tickers else None

        def _one(row: dict) -> list[FilingRecord]:
            xml_text = self.fetch_form4_xml(
                row["issuer_cik"], row["accession_no"], row.get("primary_document")
            )
            if not xml_text:
                return []
            return self.parse_form4(
                xml_text,
                accession_no=row["accession_no"],
                filing_date=row["filing_date"],
                acceptance_datetime=row.get("acceptance_datetime"),
                form_type=row.get("form_type") or "4",
            )

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for n, recs in enumerate(pool.map(_one, rows), 1):
                for rec in recs:
                    if want is not None and (rec.ticker or "").upper() not in want:
                        continue
                    yield rec
                if progress_cb and n % 200 == 0:
                    progress_cb("fetch", n, total)

        if progress_cb:
            progress_cb("fetch", total, total)
