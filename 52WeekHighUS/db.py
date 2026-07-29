"""
DB engine and session factory for the US S&P 500 breakout system.

Uses a SEPARATE SQLite file from the main trading.db:
  default: ./data/sp500_us_breakout.db  (overridden by SP500_US_DB_PATH env var)

Import pattern (matches existing 52WeekHigh/ conventions):
  sys.path must include both the project root AND the 52WeekHighUS/ directory
  before importing this module.  Each entry-point script handles that setup.
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent   # 52WeekHighUS/
_ROOT = _HERE.parent                      # project root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

from models import Base   # models.py lives in 52WeekHighUS/ (same dir as this file)

load_dotenv()

_DEFAULT_DB = _ROOT / "data" / "sp500_us_breakout.db"
_DB_PATH = Path(os.getenv("SP500_US_DB_PATH", str(_DEFAULT_DB)))

_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{_DB_PATH}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
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
