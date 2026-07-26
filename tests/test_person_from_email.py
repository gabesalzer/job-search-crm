"""Design proof for auto-creating People from an email thread's senders.

Mirrors the exact lookup logic in app.routers.ui's
_find_or_create_person_by_email / _find_or_create_company_by_domain by hand,
the same way test_cascade_design.py mirrors the DDL and test_stage_migration
mirrors the migration SQL -- this sandbox can't import ui.py (it pulls in
FastAPI/SQLAlchemy, which aren't installable here). If those functions change
in app/routers/ui.py, mirror the change here too.

What this proves:
  * email is the dedup key for Person, compared case-insensitively -- the
    same address never creates two People, regardless of casing;
  * a brand-new email creates a Person AND, if needed, a Company, inferring
    the company from the email's domain;
  * a second person at an already-known company (matched by domain) reuses
    that company instead of creating a duplicate.
"""
import re
import sqlite3

DDL = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT
);
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    email TEXT
);
"""


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _domain_of(email):
    email = (email or "").strip().lower()
    return email.split("@", 1)[1] if "@" in email else None


def _company_name_from_domain(domain):
    label = domain.split(".")[0]
    return re.sub(r"[-_]+", " ", label).strip().title() or domain


def _find_or_create_company_by_domain(conn, email):
    domain = _domain_of(email)
    if domain:
        for row in conn.execute("SELECT id, website FROM companies WHERE website IS NOT NULL"):
            host = row[1].replace("https://", "").replace("http://", "").split("/")[0]
            if host.startswith("www."):
                host = host[4:]
            if host == domain:
                return row[0]
    name = _company_name_from_domain(domain) if domain else "Unknown company"
    website = f"https://{domain}" if domain else None
    cur = conn.execute("INSERT INTO companies(name, website) VALUES (?, ?)", (name, website))
    return cur.lastrowid


def _find_or_create_person_by_email(conn, email, name=None):
    """Mirror of _find_or_create_person_by_email: email lookup is
    case-insensitive (SQLite's LIKE is case-insensitive for ASCII, same as
    the real code's .ilike()); existing rows are never overwritten."""
    email = (email or "").strip().lower()
    row = conn.execute("SELECT id FROM people WHERE email LIKE ?", (email,)).fetchone()
    if row:
        return row[0]
    company_id = _find_or_create_company_by_domain(conn, email)
    cur = conn.execute(
        "INSERT INTO people(company_id, name, email) VALUES (?, ?, ?)",
        (company_id, (name or "").strip() or email, email),
    )
    return cur.lastrowid


def test_new_email_creates_a_person_and_a_company():
    conn = _fresh_db()
    person_id = _find_or_create_person_by_email(conn, "Deirdre.Mullen@CondorSoftware.com", "Deirdre Mullen")
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    name, email = conn.execute("SELECT name, email FROM people WHERE id = ?", (person_id,)).fetchone()
    assert name == "Deirdre Mullen"
    assert email == "deirdre.mullen@condorsoftware.com", "email should be stored lowercased"


def test_same_email_different_casing_resolves_to_the_same_person():
    conn = _fresh_db()
    first = _find_or_create_person_by_email(conn, "jane@co.com", "Jane Doe")
    second = _find_or_create_person_by_email(conn, "JANE@CO.COM", "Jane Doe (again)")
    assert first == second, "casing should not create a duplicate"
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
    # The original name is preserved -- an existing Person is never overwritten.
    name = conn.execute("SELECT name FROM people WHERE id = ?", (first,)).fetchone()[0]
    assert name == "Jane Doe"


def test_second_person_at_known_company_reuses_it_by_domain():
    conn = _fresh_db()
    _find_or_create_person_by_email(conn, "deirdre.mullen@condorsoftware.com", "Deirdre Mullen")
    _find_or_create_person_by_email(conn, "alexey@condorsoftware.com", "Alexey Ivanov")
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1, (
        "both people share one company, matched by domain, not duplicated"
    )


def test_different_domains_create_separate_companies():
    conn = _fresh_db()
    _find_or_create_person_by_email(conn, "deirdre@condorsoftware.com", "Deirdre")
    _find_or_create_person_by_email(conn, "sam@otherco.com", "Sam")
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2


def test_company_name_guessed_from_domain():
    conn = _fresh_db()
    _find_or_create_person_by_email(conn, "deirdre@condor-software.com", "Deirdre")
    name = conn.execute("SELECT name FROM companies").fetchone()[0]
    assert name == "Condor Software"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} person-from-email assertions passed.")
