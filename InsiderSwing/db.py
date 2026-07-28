"""
InsiderSwing — DB engine and session factory.

Uses a SEPARATE SQLite file from trading.db:
    default: ./data/insider_swing.db   (override with INSIDER_DB_PATH)

Same isolation rationale as 52WeekHighUS/db.py — a multi-hour EDGAR backfill
must not be able to lock or corrupt the live Nifty / S&P 500 database.

Import pattern: put the project root AND InsiderSwing/ on sys.path before
importing this module.  Entry-point scripts handle that.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent   # InsiderSwing/
_ROOT = _HERE.parent                      # project root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

from models import Base   # noqa: E402  (models.py lives beside this file)

load_dotenv(_ROOT / ".env")

_DEFAULT_DB = _ROOT / "data" / "insider_swing.db"
DB_PATH = Path(os.getenv("INSIDER_DB_PATH", str(_DEFAULT_DB)))

_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            connect_args={"check_same_thread": False, "timeout": 60},
            echo=False,
        )

        # WAL + relaxed sync: the EDGAR backfill writes hundreds of thousands of
        # rows, and the dashboard reads concurrently.  WAL lets both proceed.
        @event.listens_for(_engine, "connect")
        def _set_pragmas(dbapi_conn, _rec):   # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        Base.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory()


@contextmanager
def session_scope():
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def read_sql(sql: str, params: dict | None = None):
    """Convenience pandas read against the insider DB (used by the dashboard)."""
    import pandas as pd
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def main_db_read_sql(sql: str, params: dict | None = None):
    """
    Read from the MAIN trading.db (shared/db.py) — used to borrow the
    survivorship-corrected sp500_membership table without duplicating it.
    Read-only by contract: this module never writes to trading.db.
    """
    import pandas as pd
    from sqlalchemy import text
    from shared.db import get_engine as main_engine
    with main_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})
