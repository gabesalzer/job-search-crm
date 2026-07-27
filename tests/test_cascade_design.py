"""Design proof for the master-detail / lookup / many-to-many relationships.

This test builds a SQLite database whose DDL mirrors app/models.py and asserts
the *relational behavior* the data model promises:

  * master-detail (Company -> Posting/Application/Person, Application -> History)
    cascades on delete;
  * lookup (Application -> Posting, Application -> Resume, Email Thread ->
    Application) sets NULL on delete and does NOT delete the child;
  * many-to-many (Email Thread <-> Person) removes the association row on
    either side's deletion, but never deletes the *other* side's actual row
    -- a thread survives losing one of several people on it, and a person
    survives being removed from a thread.

It uses only the standard library so it can run anywhere, including sandboxes
where SQLAlchemy isn't installed. If you change a relationship in models.py,
mirror it here.
"""
import sqlite3

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE resumes (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);

CREATE TABLE job_postings (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE
);

CREATE TABLE job_applications (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    job_posting_id INTEGER REFERENCES job_postings(id) ON DELETE SET NULL,
    resume_id INTEGER REFERENCES resumes(id) ON DELETE SET NULL,
    stage TEXT NOT NULL DEFAULT 'Saved'
);

CREATE TABLE stage_history (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES job_applications(id) ON DELETE CASCADE,
    to_stage TEXT NOT NULL
);

CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    application_id INTEGER REFERENCES job_applications(id) ON DELETE SET NULL,
    name TEXT NOT NULL
);

CREATE TABLE email_threads (
    id INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES job_applications(id) ON DELETE SET NULL,
    subject TEXT
);

CREATE TABLE email_thread_people (
    email_thread_id INTEGER NOT NULL REFERENCES email_threads(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (email_thread_id, person_id)
);
"""


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _seed(conn):
    """Employer company (1), agency company (2), resume, posting, application,
    a recruiter at the agency tied to the application, a stage-history row,
    and an email thread (linked to that recruiter via the join table) about
    that application."""
    conn.execute("INSERT INTO companies(id, name) VALUES (1, 'Hiring Co')")
    conn.execute("INSERT INTO companies(id, name) VALUES (2, 'Staffing Agency')")
    conn.execute("INSERT INTO resumes(id, label) VALUES (1, 'Resume v3')")
    conn.execute("INSERT INTO job_postings(id, company_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO job_applications(id, company_id, job_posting_id, resume_id, stage)"
        " VALUES (1, 1, 1, 1, 'Applied')"
    )
    conn.execute("INSERT INTO stage_history(id, application_id, to_stage) VALUES (1, 1, 'Applied')")
    # Recruiter's OWN company is the agency (2), independent of the app's company (1).
    conn.execute(
        "INSERT INTO people(id, company_id, application_id, name) VALUES (1, 2, 1, 'Rec Ruiter')"
    )
    conn.execute(
        "INSERT INTO email_threads(id, application_id, subject) VALUES (1, 1, 'Great catching up today!')"
    )
    conn.execute(
        "INSERT INTO email_thread_people(email_thread_id, person_id) VALUES (1, 1)"
    )
    conn.commit()


def test_deleting_company_cascades_to_its_details():
    conn = _fresh_db()
    _seed(conn)
    conn.execute("DELETE FROM companies WHERE id = 1")  # the hiring company
    conn.commit()
    # Its posting and application (and the app's history) are gone.
    assert _count(conn, "job_postings") == 0, "posting should cascade"
    assert _count(conn, "job_applications") == 0, "application should cascade"
    assert _count(conn, "stage_history") == 0, "stage history should cascade with app"
    # The agency company and its recruiter are untouched (independent parent),
    # though the recruiter's lookup to the now-deleted application is nulled.
    assert _count(conn, "companies") == 1, "agency company should remain"
    assert _count(conn, "people") == 1, "agency recruiter should remain"
    row = conn.execute("SELECT application_id FROM people WHERE id = 1").fetchone()
    assert row[0] is None, "recruiter's application lookup should be SET NULL"
    # The email thread survives too (Application is just a lookup on it) --
    # only its application link is nulled; its people-links are untouched.
    assert _count(conn, "email_threads") == 1, "email thread should survive"
    row = conn.execute("SELECT application_id FROM email_threads WHERE id = 1").fetchone()
    assert row[0] is None, "thread's application lookup should be SET NULL"
    assert _count(conn, "email_thread_people") == 1, "thread's people-links should be untouched"


def test_deleting_posting_nulls_application_but_keeps_it():
    conn = _fresh_db()
    _seed(conn)
    conn.execute("DELETE FROM job_postings WHERE id = 1")
    conn.commit()
    assert _count(conn, "job_applications") == 1, "application must survive posting deletion"
    row = conn.execute("SELECT job_posting_id FROM job_applications WHERE id = 1").fetchone()
    assert row[0] is None, "application.job_posting_id should be SET NULL"


def test_deleting_resume_nulls_application_but_keeps_it():
    conn = _fresh_db()
    _seed(conn)
    conn.execute("DELETE FROM resumes WHERE id = 1")
    conn.commit()
    assert _count(conn, "job_applications") == 1, "application must survive resume deletion"
    row = conn.execute("SELECT resume_id FROM job_applications WHERE id = 1").fetchone()
    assert row[0] is None, "application.resume_id should be SET NULL"


def test_deleting_application_cascades_history_and_nulls_people():
    conn = _fresh_db()
    _seed(conn)
    conn.execute("DELETE FROM job_applications WHERE id = 1")
    conn.commit()
    assert _count(conn, "stage_history") == 0, "history should cascade with application"
    assert _count(conn, "people") == 1, "person should survive"
    row = conn.execute("SELECT application_id FROM people WHERE id = 1").fetchone()
    assert row[0] is None, "person.application_id should be SET NULL"
    assert _count(conn, "email_threads") == 1, "email thread should survive"
    row = conn.execute("SELECT application_id FROM email_threads WHERE id = 1").fetchone()
    assert row[0] is None, "thread.application_id should be SET NULL"


def test_deleting_only_linked_person_unlinks_but_thread_survives():
    """No distinguished "primary" person -- deleting the only person linked
    to a thread just removes that link (email_thread_people row); the
    thread itself is never auto-deleted. It becomes an orphan you can clean
    up by hand later if you want to, not something the DB decides for you."""
    conn = _fresh_db()
    _seed(conn)
    conn.execute("DELETE FROM people WHERE id = 1")
    conn.commit()
    assert _count(conn, "email_thread_people") == 0, "the link should cascade away"
    assert _count(conn, "email_threads") == 1, "the thread itself must survive, now orphaned"
    assert _count(conn, "job_applications") == 1, "application should be unaffected"


def test_deleting_one_of_several_people_leaves_the_other_linked():
    """A thread with two people linked (e.g. a recruiter and a hiring
    manager on an intro thread) should only lose the deleted one's link --
    the thread stays, and the remaining person's link is untouched."""
    conn = _fresh_db()
    _seed(conn)
    conn.execute("INSERT INTO companies(id, name) VALUES (3, 'Hiring Co 2')")
    conn.execute(
        "INSERT INTO people(id, company_id, name) VALUES (2, 3, 'Hiring Manager')"
    )
    conn.execute("INSERT INTO email_thread_people(email_thread_id, person_id) VALUES (1, 2)")
    conn.commit()
    conn.execute("DELETE FROM people WHERE id = 1")  # the recruiter
    conn.commit()
    remaining = conn.execute(
        "SELECT person_id FROM email_thread_people WHERE email_thread_id = 1"
    ).fetchall()
    assert remaining == [(2,)], "the hiring manager's link should survive"
    assert _count(conn, "email_threads") == 1


def test_deleting_thread_clears_its_links_but_not_the_people():
    conn = _fresh_db()
    _seed(conn)
    conn.execute("DELETE FROM email_threads WHERE id = 1")
    conn.commit()
    assert _count(conn, "email_thread_people") == 0, "the link should cascade away"
    assert _count(conn, "people") == 1, "the person must survive"


def test_thread_can_have_multiple_people_linked_at_once():
    """The whole point of dropping a distinguished primary: a thread can
    genuinely involve more than one person at the same time."""
    conn = _fresh_db()
    _seed(conn)
    conn.execute("INSERT INTO companies(id, name) VALUES (3, 'Hiring Co 2')")
    conn.execute(
        "INSERT INTO people(id, company_id, name) VALUES (2, 3, 'Hiring Manager')"
    )
    conn.execute("INSERT INTO email_thread_people(email_thread_id, person_id) VALUES (1, 2)")
    conn.commit()
    linked = conn.execute(
        "SELECT person_id FROM email_thread_people WHERE email_thread_id = 1 ORDER BY person_id"
    ).fetchall()
    assert linked == [(1,), (2,)]


def test_multiple_email_threads_can_share_one_application():
    """application_id on email_threads is a plain indexed lookup, not unique
    -- a recruiter thread and a separate hiring-manager thread should both be
    able to point at the same application at once."""
    conn = _fresh_db()
    _seed(conn)
    conn.execute("INSERT INTO companies(id, name) VALUES (3, 'Hiring Co')")
    conn.execute(
        "INSERT INTO people(id, company_id, application_id, name) VALUES (2, 3, 1, 'Hiring Manager')"
    )
    conn.execute(
        "INSERT INTO email_threads(id, application_id, subject) VALUES (2, 1, 'Thread w/ hiring manager')"
    )
    conn.execute("INSERT INTO email_thread_people(email_thread_id, person_id) VALUES (2, 2)")
    conn.commit()
    rows = conn.execute(
        "SELECT id FROM email_threads WHERE application_id = 1 ORDER BY id"
    ).fetchall()
    assert rows == [(1,), (2,)], "both threads should be attached to the same application"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} design assertions passed.")
