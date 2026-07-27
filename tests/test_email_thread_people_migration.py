"""Design proof for the July 2026 "drop the primary Person" migration.

Mirrors the exact SQL in app.database.migrate_email_thread_people() by hand,
the same way test_stage_migration.py mirrors migrate_stage_names() -- this
sandbox can't import SQLAlchemy/dotenv, so app/database.py itself can't be
imported here. If migrate_email_thread_people() changes, mirror it here too.

The critical risk this proves out: SQLite treats `DROP TABLE` (with
`PRAGMA foreign_keys=ON`, which this app always sets) as an implicit
`DELETE FROM` first -- which CASCADEs. If the join-table backfill ran
*before* the old email_threads table was dropped, those freshly-inserted
join rows reference email_threads.id with ON DELETE CASCADE, so the implicit
delete-then-drop would wipe them right back out. test_wrong_order_loses_data
demonstrates that failure mode directly; the other tests prove the actual
(correct) ordering -- read old links into memory, THEN rebuild the table,
THEN backfill the join table -- avoids it.
"""
import sqlite3

OLD_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE job_applications (id INTEGER PRIMARY KEY);
CREATE TABLE email_threads (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    application_id INTEGER REFERENCES job_applications(id) ON DELETE SET NULL,
    subject VARCHAR(512),
    body TEXT,
    participants VARCHAR(512),
    started_at DATETIME,
    last_message_at DATETIME,
    notes TEXT,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE TABLE email_thread_people (
    email_thread_id INTEGER REFERENCES email_threads(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (email_thread_id, person_id)
);
"""


def _old_shape_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(OLD_SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO people(id, name) VALUES (1, 'Deirdre Mullen')")
    conn.execute("INSERT INTO people(id, name) VALUES (2, 'Alexey Ivanov')")
    conn.execute("INSERT INTO job_applications(id) VALUES (1)")
    conn.execute(
        "INSERT INTO email_threads(id, person_id, application_id, subject, body) "
        "VALUES (1, 1, 1, 'Great catching up!', 'Hi Deirdre...')"
    )
    conn.execute(
        "INSERT INTO email_threads(id, person_id, application_id, subject, body) "
        "VALUES (2, 2, 1, 'Gabe <> Alexey Intro', 'Hi Alexey...')"
    )
    conn.commit()
    return conn


def _has_column(conn, table, column):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


def _run_correct_migration(conn):
    """Exact mirror of app.database.migrate_email_thread_people()'s
    correct ordering: read links first, rebuild the table, backfill after."""
    if not _has_column(conn, "email_threads", "person_id"):
        return  # already migrated -- mirrors the real no-op guard

    old_links = conn.execute(
        "SELECT id, person_id FROM email_threads WHERE person_id IS NOT NULL"
    ).fetchall()

    conn.execute("""
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
    """)
    conn.execute(
        "INSERT INTO email_threads_new "
        "(id, application_id, subject, body, participants, started_at, "
        " last_message_at, notes, created_at, updated_at) "
        "SELECT id, application_id, subject, body, participants, started_at, "
        "       last_message_at, notes, created_at, updated_at "
        "FROM email_threads"
    )
    conn.execute("DROP TABLE email_threads")
    conn.execute("ALTER TABLE email_threads_new RENAME TO email_threads")

    for thread_id, person_id in old_links:
        conn.execute(
            "INSERT OR IGNORE INTO email_thread_people (email_thread_id, person_id) VALUES (?, ?)",
            (thread_id, person_id),
        )
    conn.commit()


def _run_wrong_order_migration(conn):
    """The tempting-but-wrong ordering: backfill the join table FIRST, then
    rebuild the old table. Used only to prove the failure mode is real."""
    old_links = conn.execute(
        "SELECT id, person_id FROM email_threads WHERE person_id IS NOT NULL"
    ).fetchall()
    for thread_id, person_id in old_links:
        conn.execute(
            "INSERT INTO email_thread_people (email_thread_id, person_id) VALUES (?, ?)",
            (thread_id, person_id),
        )
    conn.execute("""
        CREATE TABLE email_threads_new (
            id INTEGER PRIMARY KEY,
            application_id INTEGER REFERENCES job_applications(id) ON DELETE SET NULL,
            subject VARCHAR(512), body TEXT, participants VARCHAR(512),
            started_at DATETIME, last_message_at DATETIME, notes TEXT,
            created_at DATETIME, updated_at DATETIME
        )
    """)
    conn.execute(
        "INSERT INTO email_threads_new (id, application_id, subject, body) "
        "SELECT id, application_id, subject, body FROM email_threads"
    )
    conn.execute("DROP TABLE email_threads")  # <-- implicit cascading DELETE happens here
    conn.execute("ALTER TABLE email_threads_new RENAME TO email_threads")
    conn.commit()


def test_wrong_order_loses_data():
    """Demonstrates the real failure mode: backfilling before dropping the
    old table loses every join row, because DROP TABLE (with foreign_keys=ON)
    is an implicit DELETE that cascades into email_thread_people."""
    conn = _old_shape_db()
    _run_wrong_order_migration(conn)
    remaining = conn.execute("SELECT COUNT(*) FROM email_thread_people").fetchone()[0]
    assert remaining == 0, "the wrong ordering should lose the backfilled links (proving the risk is real)"


def test_correct_order_preserves_links():
    conn = _old_shape_db()
    _run_correct_migration(conn)
    rows = sorted(conn.execute("SELECT email_thread_id, person_id FROM email_thread_people").fetchall())
    assert rows == [(1, 1), (2, 2)]


def test_person_id_column_is_gone_after_migration():
    conn = _old_shape_db()
    _run_correct_migration(conn)
    assert not _has_column(conn, "email_threads", "person_id")


def test_other_thread_columns_survive_the_rebuild():
    conn = _old_shape_db()
    _run_correct_migration(conn)
    row = conn.execute(
        "SELECT subject, body, application_id FROM email_threads WHERE id = 1"
    ).fetchone()
    assert row == ("Great catching up!", "Hi Deirdre...", 1)


def test_migration_is_idempotent():
    conn = _old_shape_db()
    _run_correct_migration(conn)
    _run_correct_migration(conn)  # should be a no-op the second time
    rows = sorted(conn.execute("SELECT email_thread_id, person_id FROM email_thread_people").fetchall())
    assert rows == [(1, 1), (2, 2)], "re-running must not duplicate or drop links"
    assert conn.execute("SELECT COUNT(*) FROM email_threads").fetchone()[0] == 2


def test_thread_with_no_person_id_is_simply_not_backfilled():
    """A NULL person_id (shouldn't happen given the old NOT NULL constraint,
    but the migration guards it anyway with a WHERE clause) shouldn't crash
    or create a bogus join row."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(OLD_SCHEMA.replace("person_id INTEGER NOT NULL", "person_id INTEGER"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO people(id, name) VALUES (1, 'Deirdre')")
    conn.execute("INSERT INTO email_threads(id, person_id, subject) VALUES (1, NULL, 'Orphan-ish thread')")
    conn.commit()
    _run_correct_migration(conn)
    assert conn.execute("SELECT COUNT(*) FROM email_thread_people").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_threads").fetchone()[0] == 1


def test_deleting_person_unlinks_but_does_not_delete_thread():
    """Post-migration cascade behavior: removing a person from the join
    table (e.g. because they were deleted) must not take the thread with
    them if someone else is still linked to it."""
    conn = _old_shape_db()
    _run_correct_migration(conn)
    # Thread 1 currently only has Deirdre (id 1) linked -- add Alexey too,
    # simulating a multi-person thread.
    conn.execute("INSERT INTO email_thread_people (email_thread_id, person_id) VALUES (1, 2)")
    conn.commit()
    conn.execute("DELETE FROM people WHERE id = 1")  # delete Deirdre
    conn.commit()
    remaining_links = conn.execute(
        "SELECT person_id FROM email_thread_people WHERE email_thread_id = 1"
    ).fetchall()
    assert remaining_links == [(2,)], "Deirdre's link should cascade away, Alexey's should remain"
    assert conn.execute("SELECT COUNT(*) FROM email_threads WHERE id = 1").fetchone()[0] == 1, (
        "the thread itself must survive -- Alexey is still on it"
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} email-thread-people migration assertions passed.")
