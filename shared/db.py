"""
Database engine and session factory.
Shared by backtest, scanner, bot, and dashboard.

Uses the DB_PATH env var (default: ./data/trading.db relative to project root).
Creates the DB and all tables on first use.
"""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

from shared.models import Base

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "data" / "trading.db"
_DB_PATH = Path(os.getenv("DB_PATH", str(_DEFAULT_DB)))

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
