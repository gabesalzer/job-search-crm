"""Design proof for the July 2026 Stage migration (call-by-call -> macro).

Mirrors the exact SQL in app.database.migrate_stage_names() by hand, the
same way test_cascade_design.py mirrors the DDL by hand -- this sandbox
can't import SQLAlchemy/dotenv, so app/database.py itself can't be imported
here. If _STAGE_REMAP changes in database.py, mirror it here too.

The critical risk this proves out: SQLAlchemy's Enum column may persist a
Python str-Enum as either the member NAME ("SAVED") or its VALUE ("Saved"),
and this sandbox has no way to check which SQLAlchemy actually chose. So the
migration lists both forms for every old stage and applies all of them
unconditionally -- whichever family isn't actually in use just matches zero
rows. This test proves that's genuinely safe: applying the full remap to a
database using ONLY the name form works, applying it to one using ONLY the
value form also works, and applying it to a mix of both (simulating any
uncertainty about which convention is in play) still lands every row
correctly with no cross-contamination between the two families.
"""
import sqlite3

# Must match app/database.py's _STAGE_REMAP exactly.
STAGE_REMAP = {
    "SAVED": "QUALIFICATION", "Saved": "Qualification",
    "APPLIED": "QUALIFICATION", "Applied": "Qualification",
    "RECRUITER_SCREEN": "DISCOVERY", "Recruiter Screen": "Discovery",
    "HIRING_MANAGER_SCREEN": "DISCOVERY", "Hiring Manager Screen": "Discovery",
    "ONSITE": "TAKEHOME", "Onsite / Technical": "Takehome",
    "OFFER": "NEGOTIATION", "Offer": "Negotiation",
}

DDL = """
CREATE TABLE job_applications (
    id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL
);
CREATE TABLE stage_history (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL,
    from_stage TEXT,
    to_stage TEXT NOT NULL
);
"""


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _run_migration(conn):
    """Exact mirror of app.database.migrate_stage_names()'s SQL."""
    for old, new in STAGE_REMAP.items():
        conn.execute("UPDATE job_applications SET stage = ? WHERE stage = ?", (new, old))
        conn.execute("UPDATE stage_history SET to_stage = ? WHERE to_stage = ?", (new, old))
        conn.execute("UPDATE stage_history SET from_stage = ? WHERE from_stage = ?", (new, old))
    conn.execute(
        "DELETE FROM stage_history WHERE from_stage IS NOT NULL AND from_stage = to_stage"
    )
    conn.commit()


def _stage_of(conn, app_id):
    return conn.execute(
        "SELECT stage FROM job_applications WHERE id = ?", (app_id,)
    ).fetchone()[0]


def test_migrates_name_format_correctly():
    """A database that persisted enum member NAMES (e.g. "RECRUITER_SCREEN")."""
    conn = _fresh_db()
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (1, 'RECRUITER_SCREEN')")
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (2, 'CLOSED_WON')")
    conn.commit()
    _run_migration(conn)
    assert _stage_of(conn, 1) == "DISCOVERY"
    assert _stage_of(conn, 2) == "CLOSED_WON", "unchanged stages must be left alone"


def test_migrates_value_format_correctly():
    """A database that persisted enum member VALUES (e.g. "Recruiter Screen")."""
    conn = _fresh_db()
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (1, 'Recruiter Screen')")
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (2, 'Closed Won')")
    conn.commit()
    _run_migration(conn)
    assert _stage_of(conn, 1) == "Discovery"
    assert _stage_of(conn, 2) == "Closed Won", "unchanged stages must be left alone"


def test_both_formats_present_dont_cross_contaminate():
    """Even if name-format and value-format rows somehow coexist, each maps
    to its own correctly-cased target with no bleed between the families."""
    conn = _fresh_db()
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (1, 'ONSITE')")
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (2, 'Onsite / Technical')")
    conn.commit()
    _run_migration(conn)
    assert _stage_of(conn, 1) == "TAKEHOME"
    assert _stage_of(conn, 2) == "Takehome"


def test_all_six_old_stages_map_to_expected_new_stage():
    conn = _fresh_db()
    expected = {
        "Saved": "Qualification",
        "Applied": "Qualification",
        "Recruiter Screen": "Discovery",
        "Hiring Manager Screen": "Discovery",
        "Onsite / Technical": "Takehome",
        "Offer": "Negotiation",
    }
    for i, old in enumerate(expected, start=1):
        conn.execute("INSERT INTO job_applications(id, stage) VALUES (?, ?)", (i, old))
    conn.commit()
    _run_migration(conn)
    for i, (old, new) in enumerate(expected.items(), start=1):
        assert _stage_of(conn, i) == new, f"{old} should have migrated to {new}"


def test_stage_history_from_and_to_both_migrate():
    # Applied -> Recruiter Screen is a genuine transition even post-remap
    # (Qualification -> Discovery), unlike Saved -> Applied which collapses
    # into a self-loop and gets cleaned up -- that case is covered by
    # test_redundant_self_loop_history_rows_are_cleaned_up below.
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO stage_history(id, application_id, from_stage, to_stage) "
        "VALUES (1, 1, 'Applied', 'Recruiter Screen')"
    )
    conn.commit()
    _run_migration(conn)
    row = conn.execute(
        "SELECT from_stage, to_stage FROM stage_history WHERE id = 1"
    ).fetchone()
    assert row == ("Qualification", "Discovery")


def test_redundant_self_loop_history_rows_are_cleaned_up():
    """Recruiter Screen -> Hiring Manager Screen both collapse into Discovery,
    so that transition becomes a no-op row and should be deleted."""
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO stage_history(id, application_id, from_stage, to_stage) "
        "VALUES (1, 1, 'Recruiter Screen', 'Hiring Manager Screen')"
    )
    # A genuine transition (Discovery -> Takehome) must survive.
    conn.execute(
        "INSERT INTO stage_history(id, application_id, from_stage, to_stage) "
        "VALUES (2, 1, 'Hiring Manager Screen', 'Onsite / Technical')"
    )
    conn.commit()
    _run_migration(conn)
    remaining = conn.execute("SELECT id, from_stage, to_stage FROM stage_history").fetchall()
    assert remaining == [(2, "Discovery", "Takehome")], (
        "the Discovery->Discovery no-op should be deleted; the real "
        "Discovery->Takehome transition should survive"
    )


def test_saved_to_applied_collapses_and_gets_cleaned_up():
    """Saved and Applied both collapse into Qualification, so a logged
    Saved -> Applied transition becomes a Qualification -> Qualification
    self-loop after remapping and correctly gets deleted as redundant."""
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO stage_history(id, application_id, from_stage, to_stage) "
        "VALUES (1, 1, 'Saved', 'Applied')"
    )
    conn.commit()
    _run_migration(conn)
    remaining = conn.execute("SELECT * FROM stage_history").fetchall()
    assert remaining == [], "Saved->Applied should collapse to a no-op and be removed"


def test_opening_history_row_with_null_from_stage_is_untouched_by_cleanup():
    """The very first history row (application creation) has from_stage=NULL
    -- must never be treated as a self-loop and deleted."""
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO stage_history(id, application_id, from_stage, to_stage) "
        "VALUES (1, 1, NULL, 'Saved')"
    )
    conn.commit()
    _run_migration(conn)
    remaining = conn.execute("SELECT id, from_stage, to_stage FROM stage_history").fetchall()
    assert remaining == [(1, None, "Qualification")]


def test_migration_is_idempotent():
    """Running it twice must be a no-op the second time -- old strings are
    gone after the first pass, so every UPDATE matches zero rows."""
    conn = _fresh_db()
    conn.execute("INSERT INTO job_applications(id, stage) VALUES (1, 'Recruiter Screen')")
    conn.commit()
    _run_migration(conn)
    _run_migration(conn)
    assert _stage_of(conn, 1) == "Discovery"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} stage-migration assertions passed.")
