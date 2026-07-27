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


def ensure_schema():
    """Lightweight auto-migration for SQLite.

    Adds any model column that's missing from an existing table (SQLite supports
    ``ALTER TABLE ADD COLUMN``). This lets the schema evolve — e.g. new fields on
    Resume — without dropping the database or pulling in a migration framework.
    New columns must be nullable (no NOT NULL without a default), which they are.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue  # create_all will make it
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}')
                )


# Old (call-by-call) Stage -> new (macro/decision) Stage, July 2026 redesign.
# Covers BOTH string forms SQLAlchemy might have persisted for a Python
# str-Enum column -- the member NAME (e.g. "SAVED") or the member VALUE
# (e.g. "Saved") -- without needing to know in advance which one this
# database actually used. Both a name-keyed and value-keyed entry are listed
# for every old stage; whichever family isn't actually in use simply matches
# zero rows (name strings are ALL_CAPS_WITH_UNDERSCORES, value strings are
# "Title Case", so the two families can never collide with each other or
# with the new stage strings). CLOSED_WON/CLOSED_LOST aren't listed -- their
# name and value are unchanged, so there's nothing to remap.
_STAGE_REMAP = {
    "SAVED": "QUALIFICATION", "Saved": "Qualification",
    "APPLIED": "QUALIFICATION", "Applied": "Qualification",
    "RECRUITER_SCREEN": "DISCOVERY", "Recruiter Screen": "Discovery",
    "HIRING_MANAGER_SCREEN": "DISCOVERY", "Hiring Manager Screen": "Discovery",
    "ONSITE": "TAKEHOME", "Onsite / Technical": "Takehome",
    "OFFER": "NEGOTIATION", "Offer": "Negotiation",
}


def migrate_stage_names():
    """One-time remap of every stored Stage value from the old (per-call)
    model to the new (macro/decision) model. Safe to run on every startup:
    once the old strings are gone, every UPDATE below matches zero rows, so
    running this again is a no-op. Also cleans up StageHistory rows that
    became a same-stage no-op transition after consolidation -- e.g. an
    application that moved "Recruiter Screen" -> "Hiring Manager Screen"
    now shows a redundant "Discovery" -> "Discovery" row, which carries no
    information now that both collapse into one stage.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        for old, new in _STAGE_REMAP.items():
            conn.execute(
                text("UPDATE job_applications SET stage = :new WHERE stage = :old"),
                {"new": new, "old": old},
            )
            conn.execute(
                text("UPDATE stage_history SET to_stage = :new WHERE to_stage = :old"),
                {"new": new, "old": old},
            )
            conn.execute(
                text("UPDATE stage_history SET from_stage = :new WHERE from_stage = :old"),
                {"new": new, "old": old},
            )
        conn.execute(
            text(
                "DELETE FROM stage_history "
                "WHERE from_stage IS NOT NULL AND from_stage = to_stage"
            )
        )


def migrate_email_thread_people():
    """One-time migration: replace EmailThread's old single required
    ``person_id`` column with the new many-to-many ``email_thread_people``
    join table (see models.py's EmailThread docstring for why a
    distinguished "primary" person was dropped). Safe to run on every
    startup: once ``person_id`` is gone from the table, this is a no-op.

    SQLite can't ALTER a column's NOT NULL/FK constraints away in place, so
    this uses SQLite's own documented "twelve-step" procedure for changing a
    table's shape: build the new shape under a temp name, copy the data
    across, drop the old table, rename the new one into place.

    Ordering matters here in a way that's easy to get backwards: the
    person_id -> join-table backfill data is read into memory *before* the
    old table is dropped, not inserted into the join table first. That's
    deliberate -- with foreign_keys=ON (which this app always sets), SQLite
    treats `DROP TABLE email_threads` as an implicit `DELETE FROM
    email_threads` first, which would CASCADE and wipe out any
    email_thread_people rows already inserted, since they reference
    email_threads.id with ON DELETE CASCADE. Reading first and inserting
    after the rebuild sidesteps that entirely. Proven against exactly this
    failure mode in tests/test_email_thread_people_migration.py.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table("email_threads"):
        return  # fresh install; create_all already made the current shape
    existing_cols = {col["name"] for col in inspector.get_columns("email_threads")}
    if "person_id" not in existing_cols:
        return  # already migrated

    with engine.begin() as conn:
        # 1. Read the old links into memory before touching the table.
        old_links = conn.execute(
            text("SELECT id, person_id FROM email_threads WHERE person_id IS NOT NULL")
        ).fetchall()

        # 2. Rebuild email_threads without person_id.
        conn.execute(text("""
            CREATE TABLE email_threads_new (
                id INTEGER PRIMARY KEY,
                application_id INTEGER REFERENCES job_applications(id) ON DELETE SET NULL,
                subject VARCHAR(512),
                body TEXT,
                participants VARCHAR(512),
                started_at DATETIME,
                last_message_at DATETIME,
                notes TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        conn.execute(text(
            "INSERT INTO email_threads_new "
            "(id, application_id, subject, body, participants, started_at, "
            " last_message_at, notes, created_at, updated_at) "
            "SELECT id, application_id, subject, body, participants, started_at, "
            "       last_message_at, notes, created_at, updated_at "
            "FROM email_threads"
        ))
        conn.execute(text("DROP TABLE email_threads"))
        conn.execute(text("ALTER TABLE email_threads_new RENAME TO email_threads"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_email_threads_application_id "
            "ON email_threads (application_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_email_threads_last_message_at "
            "ON email_threads (last_message_at)"
        ))

        # 3. Backfill the join table now -- email_threads.id values are
        #    unchanged by the rebuild, so the links read in step 1 are still
        #    valid. INSERT OR IGNORE guards re-running against a partially
        #    populated join table.
        for thread_id, person_id in old_links:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO email_thread_people (email_thread_id, person_id) "
                    "VALUES (:t, :p)"
                ),
                {"t": thread_id, "p": person_id},
            )


def get_db():
    """FastAPI dependency that yields a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
