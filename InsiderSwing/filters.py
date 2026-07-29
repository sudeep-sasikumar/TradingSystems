"""
InsiderSwing — Form 4 noise classification.

This runs BEFORE any signal logic.  The raw Form 4 feed is overwhelmingly
mechanical: equity awards, RSU vesting, tax-withholding dispositions, option
exercises and gifts vastly outnumber discretionary open-market trades, and they
carry no information about what the insider thinks the stock is worth.  Leaving
them in is the fastest way to manufacture a signal that isn't there.

What survives
-------------
    open_market_buy   P-Purchase, non-derivative, cash price > 0, not a
                      confirmed Rule 10b5-1 plan
    open_market_sale  S-Sale, same conditions

Everything else is classified 'excluded' with a machine-readable reason, and is
KEPT in the database.  It is never deleted: the excluded set is what lets the
dashboard show "we discarded 94% of rows and here is the breakdown", and it is
needed to compute an insider's own trailing trade history honestly.

SEC transaction codes (Form 4 Table I/II, code column)
------------------------------------------------------
    P  Open-market or private purchase          → KEEP
    S  Open-market or private sale              → KEEP
    A  Grant/award from the issuer              → drop (not the insider's money)
    M  Exercise of derivative (exempt)          → drop (conversion, not conviction)
    F  Payment of exercise price / tax by       → drop (mechanical withholding)
       delivering securities ("in-kind")
    G  Bona fide gift                           → drop
    D  Disposition to the issuer                → drop (buyback/forfeit)
    C  Conversion of derivative                 → drop
    E/H  Expiration of short/long derivative    → drop
    I  Discretionary transaction (plan)         → drop
    J  Other (footnote-defined)                 → drop (unclassifiable)
    L  Small-acquisition exemption              → drop
    O/X  Out-of/in-the-money option exercise    → drop
    U  Tender of shares                         → drop (corporate action)
    W  Will / laws of descent                   → drop
    Z  Voting-trust deposit/withdrawal          → drop
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OPEN_MARKET_BUY = "open_market_buy"
OPEN_MARKET_SALE = "open_market_sale"
EXCLUDED = "excluded"

# code → exclusion reason, for the codes we deliberately discard
_DROP_CODES: dict[str, str] = {
    "A": "grant_award",
    "M": "option_exercise_exempt",
    "F": "tax_withholding_in_kind",
    "G": "gift",
    "D": "disposition_to_issuer",
    "C": "derivative_conversion",
    "E": "short_derivative_expiration",
    "H": "long_derivative_expiration",
    "I": "discretionary_plan_transaction",
    "J": "other_footnote_defined",
    "L": "small_acquisition_exemption",
    "O": "otm_option_exercise",
    "X": "itm_option_exercise",
    "U": "tender_of_shares",
    "W": "will_or_descent",
    "Z": "voting_trust",
}

# Security titles that are equity-linked instruments rather than the common
# stock itself.  A "purchase" of RSUs is a grant with a different code path.
_DERIVATIVE_TITLE_HINTS = (
    "option", "warrant", "restricted stock unit", "rsu", "phantom",
    "stock appreciation", "sar", "convertible", "right to buy", "deferred stock",
    "performance share", "psu",
)


def looks_derivative(security_title: Optional[str]) -> bool:
    t = (security_title or "").lower()
    return any(h in t for h in _DERIVATIVE_TITLE_HINTS)


def classify_transaction(
    *,
    transaction_code: Optional[str],
    acquired_disposed: Optional[str],
    is_derivative: bool,
    security_title: Optional[str],
    price_per_share: Optional[float],
    shares: Optional[float],
    rule_10b5_1: Optional[bool],
    exclude_unknown_10b5_1: bool = False,
) -> tuple[str, Optional[str]]:
    """
    Classify one Form 4 transaction line.

    Returns (classification, exclude_reason).  exclude_reason is None for kept
    rows.  Pure function — no DB, no config lookup — so it is trivially testable
    and the same call is used by ingest, by re-classification, and by tests.
    """
    code = (transaction_code or "").strip().upper()
    # FMP hands back 'P-Purchase' style codes; EDGAR hands back bare 'P'.
    if "-" in code:
        code = code.split("-", 1)[0]

    if not code:
        return EXCLUDED, "missing_transaction_code"

    if is_derivative or looks_derivative(security_title):
        return EXCLUDED, "derivative_security"

    if code in _DROP_CODES:
        return EXCLUDED, _DROP_CODES[code]

    if code not in ("P", "S"):
        return EXCLUDED, f"unhandled_code_{code}"

    if shares is None or shares <= 0:
        return EXCLUDED, "zero_or_missing_shares"

    # A $0 price on a P or S row is not an open-market trade — it is a
    # mis-coded transfer.  Cash consideration is the whole point.
    if price_per_share is None or price_per_share <= 0:
        return EXCLUDED, "zero_or_missing_price"

    if rule_10b5_1 is True:
        return EXCLUDED, "rule_10b5_1_plan"
    if rule_10b5_1 is None and exclude_unknown_10b5_1:
        return EXCLUDED, "rule_10b5_1_unknown"

    if code == "P":
        # Sanity: a purchase should be an acquisition.
        if acquired_disposed and acquired_disposed.strip().upper() != "A":
            return EXCLUDED, "purchase_code_with_disposition"
        return OPEN_MARKET_BUY, None

    if acquired_disposed and acquired_disposed.strip().upper() != "D":
        return EXCLUDED, "sale_code_with_acquisition"
    return OPEN_MARKET_SALE, None


def classify_record(txn, exclude_unknown_10b5_1: bool = False) -> tuple[str, Optional[str]]:
    """Convenience wrapper over a TransactionRecord dataclass."""
    return classify_transaction(
        transaction_code=txn.transaction_code,
        acquired_disposed=txn.acquired_disposed,
        is_derivative=txn.is_derivative,
        security_title=txn.security_title,
        price_per_share=txn.price_per_share,
        shares=txn.shares,
        rule_10b5_1=txn.rule_10b5_1,
        exclude_unknown_10b5_1=exclude_unknown_10b5_1,
    )


def reclassify_all(exclude_unknown_10b5_1: bool = False) -> dict[str, int]:
    """
    Re-run the classifier over every stored transaction.

    Used after a filter change so the existing corpus doesn't need re-fetching
    from EDGAR.  Rewrites only classification / exclude_reason.
    """
    import pandas as pd
    from sqlalchemy import text

    from db import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(text(
            "SELECT id, transaction_code, acquired_disposed, is_derivative, "
            "security_title, price_per_share, shares, rule_10b5_1 FROM ins_transactions"
        ), conn)

    if df.empty:
        return {}

    updates: list[dict] = []
    counts: dict[str, int] = {}
    for row in df.itertuples(index=False):
        cls, reason = classify_transaction(
            transaction_code=row.transaction_code,
            acquired_disposed=row.acquired_disposed,
            is_derivative=bool(row.is_derivative),
            security_title=row.security_title,
            price_per_share=row.price_per_share,
            shares=row.shares,
            rule_10b5_1=(None if pd.isna(row.rule_10b5_1) else bool(row.rule_10b5_1)),
            exclude_unknown_10b5_1=exclude_unknown_10b5_1,
        )
        key = cls if cls != EXCLUDED else f"excluded:{reason}"
        counts[key] = counts.get(key, 0) + 1
        updates.append({"pk": int(row.id), "cls": cls, "reason": reason})

    with engine.begin() as conn:
        for i in range(0, len(updates), 1000):
            conn.execute(
                text("UPDATE ins_transactions SET classification=:cls, "
                     "exclude_reason=:reason WHERE id=:pk"),
                updates[i:i + 1000],
            )

    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
