"""Database engine, session factory, and declarative base.

SQLite is used for single-user simplicity. `PRAGMA foreign_keys=ON` is enabled
per-connection because SQLite does NOT enforce foreign keys (and therefore
ON DELETE CASCADE / SET NULL) unless you turn it on explicitly.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/jobsearch.db")

# check_same_thread=False lets the SQLite connection be shared across FastAPI's
# threadpool workers. Safe here because sessions are per-request.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enforce foreign keys on every SQLite connection."""
    # Only applies to SQLite; other drivers ignore this.
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        pass


def get_db():
    """FastAPI dependency that yields a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
