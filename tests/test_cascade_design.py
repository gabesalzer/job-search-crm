"""Design proof for the master-detail vs. lookup relationships.

This test builds a SQLite database whose DDL mirrors app/models.py and asserts
the *relational behavior* the data model promises:

  * master-detail (Company -> Posting/Application/Person, Application -> History)
    cascades on delete;
  * lookup (Application -> Posting, Application -> Resume) sets NULL on delete
    and does NOT delete the child.

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
    a recruiter at the agency tied to the application, and a stage-history row."""
    conn.execute("INSERT INTO companies(id, name) VALUES (1, 'Hiring Co')")
    conn.execute("INSERT INTO companies(id, name) VALUES (2, 'Staffing Agency')")
    conn.execute("INSERT INTO resumes(id, label) VALUES (1, 'RevOps v3')")
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} design assertions passed.")
