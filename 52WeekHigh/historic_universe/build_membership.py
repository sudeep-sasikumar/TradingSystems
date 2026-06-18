"""
Build the index_membership table.

Inputs (all in data/reconstitution_pdfs/):
  nifty500_baseline_20200725.csv   — Wayback Machine snapshot, July 25 2020
  *.pdf                            — niftyindices.com semi-annual reconstitution PDFs

Algorithm:
  1. Load baseline (501 stocks as of 2020-07-25).
  2. Parse every PDF in the directory → (effective_date, additions, removals).
  3. Forward pass  (recons after 2020-07-25): track additions and closures.
  4. Backward pass (recons on/before 2020-07-25): reconstruct pre-baseline state.
  5. Any baseline stock with no known add-event → added_date = COVERAGE_START, quality='inferred'.
  6. Write all intervals to index_membership (drops and recreates table each run).

Usage:
    python 52WeekHigh/historic_universe/build_membership.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent          # …/52WeekHigh/historic_universe
_ROOT = _HERE.parent.parent                      # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# ─────────────────────────────────────────────────────────────────────────────

import pandas as pd

from shared.db import session_scope, get_engine
from shared.models import Base, IndexMembership

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────
BASELINE_DATE   = date(2020, 7, 25)
BASELINE_CSV    = _ROOT / "data" / "reconstitution_pdfs" / "nifty500_baseline_20200725.csv"
RECON_PDF_DIR   = _ROOT / "data" / "reconstitution_pdfs"

# The first date for which we can reliably claim membership.
# Stocks in the baseline but with no known add-event are assigned this date.
COVERAGE_START  = date(2019, 10, 1)   # approx effective date of Sep-2019 reconstitution


# ════════════════════════════════════════════════════════════════════════════
#  PDF PARSER
# ════════════════════════════════════════════════════════════════════════════

_DATE_PATTERNS = [
    # "effective from September 30, 2022"  ← niftyindices.com primary format
    r"effective\s+from\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    # "effective from 28th October, 2024"  ← day-first variant
    r"effective\s+(?:from|date)[:\s]+(\d{1,2}(?:st|nd|rd|th)?\s+\w+[,\s]+\d{4})",
    # "w.e.f. October 28, 2024"
    r"w\.?e\.?f\.?\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    # "effective 28/10/2024" or "28-10-2024"
    r"effective[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})",
    # "Effective Date: 28 October 2024"
    r"effective\s+date[:\s]+(\d{1,2}\s+\w+[,\s]+\d{4})",
]

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def _parse_date_string(s: str) -> date | None:
    s = s.strip().rstrip(",").strip()
    # Remove ordinal suffixes: 28th → 28, 1st → 1, etc.
    s = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s, flags=re.I)

    # Try numeric formats first
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %m %Y"):
        try:
            return date(*[int(x) for x in re.split(r"[/\-\s]", s) if x])
        except Exception:
            pass

    # Try text formats: "28 October 2024" or "October 28, 2024" or "October 2024"
    parts = [p for p in re.split(r"[\s,]+", s) if p]
    if len(parts) == 3:
        for day_i, mon_i, yr_i in [(0, 1, 2), (1, 0, 2)]:
            try:
                day = int(parts[day_i])
                mon = _MONTH_MAP.get(parts[mon_i].lower())
                yr  = int(parts[yr_i])
                if mon and 1 <= day <= 31 and 2000 <= yr <= 2030:
                    return date(yr, mon, day)
            except (ValueError, IndexError):
                pass
    if len(parts) == 2:
        # "October 2024" — assume 1st
        mon = _MONTH_MAP.get(parts[0].lower())
        try:
            yr = int(parts[1])
            if mon and 2000 <= yr <= 2030:
                return date(yr, mon, 1)
        except ValueError:
            pass
    return None


def _extract_effective_date(full_text: str) -> date | None:
    text_lower = full_text.lower()
    for pattern in _DATE_PATTERNS:
        m = re.search(pattern, text_lower, re.I)
        if m:
            d = _parse_date_string(m.group(1))
            if d:
                return d
    return None


def _looks_like_symbol(s: str) -> bool:
    """NSE symbols: 2–20 uppercase letters/digits, sometimes & or - but no lowercase."""
    s = s.strip()
    return bool(re.match(r"^[A-Z0-9&\-\.]{2,20}$", s))


def _extract_nifty500_section(full_text: str) -> str | None:
    """
    Return the text block for the 'Nifty 500' sub-section of a reconstitution PDF.
    The PDFs cover many indices; we only want the Nifty 500 additions/removals.
    Section headers use either number or letter prefixes: "3) Nifty 500" or "b) Nifty 500".
    """
    # Match "3) Nifty 500" or "b) Nifty 500" at the start of any line
    m_start = re.search(r'(?:^|\n)[^\n]*?[a-zA-Z\d]+\s*\)\s*nifty\s+500\b', full_text, re.I)
    if not m_start:
        m_start = re.search(r'(?:^|\n)\s*nifty\s+500\b', full_text, re.I)
    if not m_start:
        return None

    rest = full_text[m_start.end():]

    # Find the next index section header (number or letter prefix + ) + Nifty)
    m_end = re.search(r'\n\s*[a-zA-Z\d]+\s*\)\s*(?:Nifty|NIFTY)', rest, re.I)
    if m_end:
        return full_text[m_start.start(): m_start.end() + m_end.start()]
    return full_text[m_start.start():]


def _parse_nifty500_section(section_text: str) -> tuple[list[dict], list[dict]]:
    """
    Parse additions and removals from the Nifty 500 text block.

    Each stock line is:  "<serial_no> <Company Name> <SYMBOL>"
    "being excluded" lines switch current list to removals;
    "being included" lines switch to additions.
    """
    additions: list[dict] = []
    removals:  list[dict] = []
    current:   list[dict] | None = None

    for line in section_text.splitlines():
        stripped = line.strip()
        low = stripped.lower()

        if not stripped:
            continue

        if re.search(r'being\s+excluded|being\s+deleted|being\s+removed', low):
            current = removals
            continue
        if re.search(r'being\s+included|being\s+added', low):
            current = additions
            continue

        if current is None:
            continue

        # Skip header rows and boilerplate
        if re.search(
            r'sr\.?\s*no|company\s+name|nse\s*symbol|^symbol$'
            r'|above\s+replacement|also\s+applicable|press\s+release|nse\s+indices',
            low
        ):
            continue

        tokens = stripped.split()
        # Stock lines start with a serial number
        if len(tokens) < 3 or not re.match(r'^\d+$', tokens[0]):
            continue

        # Symbol = rightmost token that looks like an NSE symbol (all-uppercase)
        symbol = None
        for tok in reversed(tokens[1:]):
            if _looks_like_symbol(tok) and not tok.isdigit():
                symbol = tok
                break
        if not symbol:
            continue

        # Company name = everything between serial number and symbol
        sym_pos = stripped.rfind(symbol)
        company = stripped[len(tokens[0]):sym_pos].strip()

        current.append({"symbol": symbol, "company_name": company})

    return additions, removals


def parse_reconstitution_pdf(pdf_path: Path) -> dict | None:
    """
    Parse a single niftyindices.com Nifty 500 reconstitution PDF.

    Returns:
        {
          "effective_date": date,
          "additions":  [{"symbol": ..., "company_name": ...}, ...],
          "removals":   [{"symbol": ..., "company_name": ...}, ...],
          "source":     str,
          "warnings":   [str],
        }
    or None if the file cannot be parsed at all.
    """
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError:
        logger.error("pdfplumber not installed. Run: pip install pdfplumber")
        return None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:
        logger.error(f"Cannot open PDF {pdf_path.name}: {exc}")
        return None

    if not full_text.strip():
        logger.warning(f"{pdf_path.name}: no text extracted — may be a scanned PDF")
        return None

    effective_date = _extract_effective_date(full_text)
    if effective_date is None:
        logger.warning(
            f"{pdf_path.name}: could not determine effective date from text. "
            "Will not be used."
        )
        return None

    warnings: list[str] = []
    nifty500_text = _extract_nifty500_section(full_text)
    if nifty500_text:
        additions, removals = _parse_nifty500_section(nifty500_text)
    else:
        logger.warning(f"{pdf_path.name}: Nifty 500 section not found.")
        warnings.append("Nifty 500 section not found in PDF text")
        additions, removals = [], []

    def dedup(lst):
        seen: set[str] = set()
        out = []
        for item in lst:
            if item["symbol"] not in seen:
                seen.add(item["symbol"])
                out.append(item)
        return out

    additions = dedup(additions)
    removals  = dedup(removals)

    if not additions and not removals:
        warnings.append("no additions or removals found — check parser against this PDF")

    return {
        "effective_date": effective_date,
        "additions":      additions,
        "removals":       removals,
        "source":         f"recon_{effective_date.strftime('%Y%m')}",
        "warnings":       warnings,
    }


# ════════════════════════════════════════════════════════════════════════════
#  MEMBERSHIP RECONSTRUCTION
# ════════════════════════════════════════════════════════════════════════════

def load_baseline() -> list[dict]:
    """Load the July 2020 baseline CSV. Returns list of {symbol, company_name, isin}."""
    if not BASELINE_CSV.exists():
        raise FileNotFoundError(
            f"Baseline CSV not found: {BASELINE_CSV}\n"
            "Download it from the Wayback Machine and save it there."
        )
    df = pd.read_csv(BASELINE_CSV, dtype=str).fillna("")
    df.columns = df.columns.str.strip()

    # NSE CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
    col_map = {}
    for c in df.columns:
        low = c.lower()
        if "symbol" in low and "nse" not in low:
            col_map["symbol"] = c
        elif "company" in low or "name" in low:
            col_map["company_name"] = c
        elif "isin" in low:
            col_map["isin"] = c

    if "symbol" not in col_map:
        raise ValueError(f"Cannot find Symbol column in baseline CSV. Columns: {df.columns.tolist()}")

    stocks = []
    for _, row in df.iterrows():
        sym = str(row[col_map["symbol"]]).strip().upper()
        if not sym or sym == "NAN":
            continue
        stocks.append({
            "symbol":       sym,
            "company_name": str(row.get(col_map.get("company_name", ""), "")).strip(),
            "isin":         str(row.get(col_map.get("isin", ""), "")).strip(),
        })

    logger.info(f"Baseline loaded: {len(stocks)} stocks as of {BASELINE_DATE}")
    return stocks


def load_all_pdfs() -> list[dict]:
    """
    Parse all PDFs in RECON_PDF_DIR.
    Returns a list of reconstitution events, sorted by effective_date.
    """
    pdf_files = sorted(RECON_PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        logger.warning(
            f"No PDF files found in {RECON_PDF_DIR}\n"
            "Download the semi-annual reconstitution PDFs from:\n"
            "  https://niftyindices.com/announcements/reconstitution\n"
            "Save them as *.pdf files in data/reconstitution_pdfs/"
        )
        return []

    events = []
    for pdf_path in pdf_files:
        logger.info(f"  Parsing {pdf_path.name} ...")
        result = parse_reconstitution_pdf(pdf_path)
        if result is None:
            logger.warning(f"  Skipping {pdf_path.name} (parse failed)")
            continue
        if result["warnings"]:
            for w in result["warnings"]:
                logger.warning(f"  {pdf_path.name}: {w}")
        logger.info(
            f"  {pdf_path.name}: effective {result['effective_date']}, "
            f"+{len(result['additions'])} additions, -{len(result['removals'])} removals"
        )
        events.append(result)

    events.sort(key=lambda e: e["effective_date"])
    logger.info(f"Parsed {len(events)} reconstitution events from {len(pdf_files)} PDFs.")
    return events


def reconstruct_membership(
    baseline_stocks: list[dict],
    events: list[dict],
) -> list[dict]:
    """
    Reconstruct point-in-time Nifty 500 membership.

    Returns a flat list of interval records:
        {symbol, company_name, isin, added_date, removed_date, date_quality, source, notes}
    """
    # Separate pre- and post-baseline events
    pre  = [e for e in events if e["effective_date"] <= BASELINE_DATE]
    post = [e for e in events if e["effective_date"] >  BASELINE_DATE]

    # ── State: symbol → {company_name, isin, current_added_date, quality}
    # Represents currently-open intervals (removed_date = None)
    state: dict[str, dict] = {}
    # Closed intervals (removed_date set)
    closed_intervals: list[dict] = []

    baseline_set = {s["symbol"] for s in baseline_stocks}
    baseline_info = {s["symbol"]: s for s in baseline_stocks}

    # ── Initialize open intervals from baseline ───────────────────────────────
    for sym, info in baseline_info.items():
        state[sym] = {
            "company_name": info["company_name"],
            "isin":         info["isin"],
            "added_date":   None,   # will be set during backward pass or COVERAGE_START
            "quality":      "inferred",
            "source":       "baseline_20200725",
        }

    # ── BACKWARD PASS — pre-baseline reconstitutions (most recent first) ──────
    for event in reversed(pre):
        eff = event["effective_date"]
        eff_str = eff.strftime("%Y-%m-%d")
        removed_str = (eff - timedelta(days=1)).strftime("%Y-%m-%d")

        for s in event["additions"]:
            sym = s["symbol"]
            if sym in state:
                # This stock was added at eff; before eff it was NOT in index
                state[sym]["added_date"] = eff_str
                state[sym]["quality"]    = "exact"
                state[sym]["source"]     = event["source"]
                # Before this reconstitution, sym was NOT present → no interval needed
                # (its only interval starts at eff and is still open)

        for s in event["removals"]:
            sym = s["symbol"]
            if sym not in state:
                # Stock was removed at eff; was in index before eff
                # If we have baseline info, use that; otherwise minimal info
                info = baseline_info.get(sym, {})
                # This stock had an interval ending at eff-1
                # We don't know when it started; mark as inferred from COVERAGE_START
                closed_intervals.append({
                    "symbol":       sym,
                    "company_name": s.get("company_name") or info.get("company_name", ""),
                    "isin":         info.get("isin", ""),
                    "added_date":   None,   # set below
                    "removed_date": removed_str,
                    "date_quality": "inferred",
                    "source":       event["source"],
                    "notes":        f"Removed at {eff_str}; pre-removal start inferred",
                })

    # ── FORWARD PASS — post-baseline reconstitutions (chronological) ──────────
    for event in post:
        eff = event["effective_date"]
        eff_str = eff.strftime("%Y-%m-%d")
        removed_str = (eff - timedelta(days=1)).strftime("%Y-%m-%d")

        for s in event["additions"]:
            sym = s["symbol"]
            if sym in state:
                logger.warning(
                    f"{sym} appears in additions for {eff_str} but already has an open interval. "
                    "Possible data issue or re-entry after removal."
                )
                # Close the existing interval first (shouldn't happen normally)
                existing = state.pop(sym)
                closed_intervals.append(_make_interval(sym, existing, removed_str))

            # Open a new interval
            info = baseline_info.get(sym, {})
            state[sym] = {
                "company_name": s.get("company_name") or info.get("company_name", ""),
                "isin":         info.get("isin", ""),
                "added_date":   eff_str,
                "quality":      "exact",
                "source":       event["source"],
            }

        for s in event["removals"]:
            sym = s["symbol"]
            if sym not in state:
                logger.warning(
                    f"{sym} appears in removals for {eff_str} but has no open interval. "
                    "Possible data issue or stock removed before we have coverage."
                )
                continue
            existing = state.pop(sym)
            closed_intervals.append(_make_interval(sym, existing, removed_str))

    # ── Finalize open intervals ───────────────────────────────────────────────
    coverage_start_str = COVERAGE_START.strftime("%Y-%m-%d")
    open_intervals = []
    for sym, info in state.items():
        if info["added_date"] is None:
            info["added_date"] = coverage_start_str
            info["quality"]    = "inferred"
        open_intervals.append({
            "symbol":       sym,
            "company_name": info["company_name"],
            "isin":         info.get("isin", ""),
            "added_date":   info["added_date"],
            "removed_date": None,
            "date_quality": info["quality"],
            "source":       info.get("source", "baseline_20200725"),
            "notes":        None,
        })

    # Finalize closed intervals with inferred start dates
    for rec in closed_intervals:
        if rec["added_date"] is None:
            rec["added_date"] = coverage_start_str

    all_records = closed_intervals + open_intervals
    logger.info(
        f"Membership reconstructed: {len(open_intervals)} currently-open intervals, "
        f"{len(closed_intervals)} closed intervals, {len(all_records)} total."
    )
    return all_records


def _make_interval(sym: str, info: dict, removed_str: str) -> dict:
    return {
        "symbol":       sym,
        "company_name": info.get("company_name", ""),
        "isin":         info.get("isin", ""),
        "added_date":   info.get("added_date") or COVERAGE_START.strftime("%Y-%m-%d"),
        "removed_date": removed_str,
        "date_quality": info.get("quality", "inferred"),
        "source":       info.get("source", ""),
        "notes":        None,
    }


# ════════════════════════════════════════════════════════════════════════════
#  DATABASE WRITE
# ════════════════════════════════════════════════════════════════════════════

def save_membership(records: list[dict], dry_run: bool = False) -> None:
    from sqlalchemy import text

    engine = get_engine()
    Base.metadata.create_all(engine)

    with session_scope() as session:
        deleted = session.execute(text("DELETE FROM index_membership")).rowcount
        if deleted:
            logger.info(f"Cleared {deleted} existing membership records.")

    if dry_run:
        logger.info(f"[dry-run] Would write {len(records)} membership records.")
        for r in records[:10]:
            logger.info(f"  {r}")
        return

    with session_scope() as session:
        for r in records:
            session.add(IndexMembership(
                symbol       = r["symbol"],
                company_name = r.get("company_name", ""),
                isin         = r.get("isin", ""),
                added_date   = r["added_date"],
                removed_date = r.get("removed_date"),
                date_quality = r.get("date_quality", "inferred"),
                source       = r.get("source", ""),
                notes        = r.get("notes"),
            ))

    logger.info(f"Saved {len(records)} membership intervals to SQLite.")


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def main():
    global RECON_PDF_DIR

    parser = argparse.ArgumentParser(description="Build Nifty 500 historical membership table")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and reconstruct but do not write to DB")
    parser.add_argument("--pdf-dir", type=Path, default=RECON_PDF_DIR,
                        help="Directory containing reconstitution PDFs and baseline CSV")
    args = parser.parse_args()

    RECON_PDF_DIR = args.pdf_dir

    logger.info("=" * 60)
    logger.info("Building Nifty 500 historical membership table")
    logger.info(f"Baseline  : {BASELINE_CSV}")
    logger.info(f"PDF dir   : {RECON_PDF_DIR}")
    logger.info(f"Coverage  : {COVERAGE_START} → present")
    logger.info("=" * 60)

    baseline_stocks = load_baseline()

    logger.info("Parsing reconstitution PDFs ...")
    events = load_all_pdfs()

    if not events:
        logger.warning(
            "No reconstitution PDFs found. Membership table will use BASELINE ONLY — "
            "all 501 stocks assigned added_date=%s (inferred). "
            "Download PDFs from https://niftyindices.com/announcements/reconstitution "
            "and rerun to improve accuracy.",
            COVERAGE_START,
        )

    records = reconstruct_membership(baseline_stocks, events)

    save_membership(records, dry_run=args.dry_run)

    # Summary
    exact = sum(1 for r in records if r["date_quality"] == "exact")
    inferred = len(records) - exact
    logger.info(f"\nSummary:")
    logger.info(f"  Total intervals : {len(records)}")
    logger.info(f"  Exact dates     : {exact}  (from PDF reconstitution events)")
    logger.info(f"  Inferred dates  : {inferred}  (baseline stocks with no known add-event)")
    logger.info(f"  PDFs used       : {len(events)}")
    if not events:
        logger.info("\n  NOTE: Without PDFs, the membership table is identical to the current")
        logger.info("  Nifty 500 list projected backward — same survivorship bias as before.")
        logger.info("  PDFs are REQUIRED for an accurate survivorship-corrected backtest.")


if __name__ == "__main__":
    main()
