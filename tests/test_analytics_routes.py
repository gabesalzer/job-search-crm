"""The analytics page, the JSON API, and the Closed Lost migration.

The migration is the reason this file exists. `lost_reason` changed from an
Enum column to free text in the same commit that added `lost_category`, and
that is exactly the kind of change that looks fine in review and eats data on
startup. `database._migrate_email_thread_person_id` is the cautionary tale
already in this repo: a rebuild that drops columns, unreachable on the live
database and therefore never noticed. This one runs against every database on
every boot, so it is tested against a real one.

Run: python3 tests/test_analytics_routes.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-analytics-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    print("      pip install -r requirements.txt --break-system-packages")
    raise SystemExit(0)

from sqlalchemy import text  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal, engine, migrate_lost_reason  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

DAY = timedelta(days=1)
BASE = datetime(2026, 5, 1)


def _company(db, name):
    found = db.query(models.Company).filter_by(name=name).first()
    if found:
        return found.id
    row = models.Company(name=name)
    db.add(row)
    db.flush()
    return row.id


def _make(db, name, *, stage, applied=None, history=(), lost_category=None,
          lost_reason=None):
    """Create an application with a hand-written stage history.

    History is written directly rather than by assigning `stage` repeatedly,
    because the event listener stamps `changed_at` with the wall clock and
    these tests need known dates to assert known durations against.
    """
    appn = models.JobApplication(
        company_id=_company(db, name), title="RevOps Lead",
        stage=models.Stage(stage), applied_date=applied,
        lost_category=(models.LostCategory(lost_category)
                       if lost_category else None),
        lost_reason=lost_reason,
    )
    db.add(appn)
    db.flush()
    # The listener already queued an opening row for the initial stage; drop it
    # so the explicit history below is the whole story.
    appn.stage_history.clear()
    for to_stage, when in history:
        appn.stage_history.append(models.StageHistory(
            from_stage=None, to_stage=models.Stage(to_stage), changed_at=when))
    db.flush()
    return appn.id


def _seed():
    with SessionLocal() as db:
        if db.query(models.JobApplication).first():
            return
        # Three applications that between them can answer every interval, plus
        # one that can answer none -- the shape that makes `n` differ from the
        # application count, which is the whole reason `n` is displayed.
        _make(db, "Condor", stage="Discovery", applied=BASE + 12 * DAY, history=[
            ("Staging", BASE), ("Qualification", BASE + 10 * DAY),
            ("Discovery", BASE + 22 * DAY)])
        _make(db, "Plaid", stage="Closed Lost", applied=BASE + 2 * DAY,
              lost_category="Compensation gap",
              lost_reason="Band topped out 30k under.", history=[
                  ("Staging", BASE), ("Qualification", BASE + 8 * DAY),
                  ("Discovery", BASE + 16 * DAY),
                  ("Closed Lost", BASE + 40 * DAY)])
        _make(db, "Jellyfish", stage="Negotiation", applied=BASE, history=[
            ("Staging", BASE - 6 * DAY), ("Qualification", BASE),
            ("Discovery", BASE + 9 * DAY), ("Negotiation", BASE + 30 * DAY)])
        # No applied date, no history: contributes to `total` and to nothing else.
        _make(db, "LanceDB", stage="Staging")
        db.commit()


_seed()


# --------------------------------------------------------------------------- #
def test_the_page_renders_and_shows_the_headline_intervals():
    resp = client.get("/analytics")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Staging to Qualification" in body
    assert "Applied to Discovery" in body
    assert "Applied to closed" in body
    assert "Drop-off" in body


def test_averages_appear_only_once_three_records_can_answer():
    """Staging→Qualification has 3 contributors; Applied→closed has 1."""
    from app import analytics
    from app.routers.ui import _analytics_apps, STAGE_ORDER_VALUES

    with SessionLocal() as db:
        payload = analytics.overview(_analytics_apps(db), STAGE_ORDER_VALUES)
    by_key = {iv["key"]: iv for iv in payload["intervals"]}

    staging = by_key["staging_to_qualification"]
    assert staging["n"] == 3 and staging["enough"] is True
    assert staging["mean"] == 8.0, staging          # 10, 8, 6
    assert staging["median"] == 8.0

    closing = by_key["applied_to_close"]
    assert closing["n"] == 1 and closing["enough"] is False
    assert closing["mean"] is None, "one record must never be shown as an average"
    assert closing["min"] == 38.0, "but its range is still honest"


def test_the_page_says_not_enough_data_rather_than_showing_a_number():
    body = client.get("/analytics").text
    assert "Not enough data" in body
    assert "Only 1 record can answer this" in body


def test_the_funnel_never_widens():
    """Monotonicity is asserted structurally, not against a fixed count.

    Other tests in this file create applications, so a hardcoded number here
    would pass or fail depending on alphabetical test order -- which is exactly
    the kind of test that gets deleted later for being flaky rather than fixed.
    """
    resp = client.get("/api/analytics/funnel")
    counts = [row["reached"] for row in resp.json()["funnel"]]
    assert counts == sorted(counts, reverse=True), counts

    with SessionLocal() as db:
        staged = db.query(models.JobApplication).filter_by(
            stage=models.Stage.STAGING).count()
    assert staged >= 1, "the fixture needs a Staging application to be meaningful"
    assert counts[0] < db_total(), "an application in Staging is not on the funnel"


def db_total():
    with SessionLocal() as db:
        return db.query(models.JobApplication).count()


def test_off_funnel_names_the_gap_between_total_and_the_first_bar():
    body = client.get("/analytics").text
    assert "not on the funnel at all" in body, (
        "a first bar smaller than the application count reads as a bug "
        "unless the difference is named")


def test_the_api_and_the_page_cannot_disagree():
    """Both read `app/analytics.py`; this pins that they still do."""
    from app import analytics
    from app.routers.ui import _analytics_apps, STAGE_ORDER_VALUES

    api = client.get("/api/analytics/funnel").json()["funnel"]
    with SessionLocal() as db:
        page = analytics.funnel(_analytics_apps(db), STAGE_ORDER_VALUES)
    assert [r["reached"] for r in api] == [r["reached"] for r in page]


def test_durations_endpoint_suppresses_in_the_data_not_the_template():
    payload = client.get("/api/analytics/durations").json()
    closing = [i for i in payload["intervals"]
               if i["key"] == "applied_to_close"][0]
    assert closing["enough"] is False
    assert closing["mean"] is None and closing["median"] is None, (
        "a consumer ignoring `enough` must still be unable to render a "
        "one-record average, because there is no number there")


def test_losses_count_only_currently_lost_applications():
    payload = client.get("/api/analytics/losses").json()
    with SessionLocal() as db:
        lost = db.query(models.JobApplication).filter_by(
            stage=models.Stage.CLOSED_LOST).count()
        live_with_a_category = db.query(models.JobApplication).filter(
            models.JobApplication.stage != models.Stage.CLOSED_LOST,
            models.JobApplication.lost_category.isnot(None)).count()
    assert payload["total_lost"] == lost
    assert live_with_a_category == 0, (
        "moving out of Closed Lost must clear the category, or the breakdown "
        "would count a live pursuit as a loss")
    assert sum(r["count"] for r in payload["rows"]) == lost, (
        "every loss lands in exactly one row, including uncategorised ones")
    assert any(r["category"] == "Compensation gap" for r in payload["rows"])


# --------------------------------------------------------------------------- #
# The Closed Lost fields
# --------------------------------------------------------------------------- #
def _edit(app_id, **form):
    with SessionLocal() as db:
        a = db.get(models.JobApplication, app_id)
        payload = {
            "company_id": str(a.company_id), "title": a.title or "",
            "stage": a.stage.value, "lost_reason": a.lost_reason or "",
            "lost_category": a.lost_category.value if a.lost_category else "",
            "applied_date": "", "created_at": "", "last_activity_date": "",
            "updated_at": "", "notes": "", "context": "", "source": "",
            "manual_forecast": "", "champion": "",
        }
    payload.update(form)
    resp = client.post("/ui/applications/{}/edit".format(app_id),
                       data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def test_closing_lost_stores_both_the_category_and_the_free_text():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Lost Test", stage="Discovery")
        db.commit()
    _edit(app_id, stage="Closed Lost", lost_category="Level or scope mismatch",
          lost_reason="They wanted a manager of managers.")
    with SessionLocal() as db:
        a = db.get(models.JobApplication, app_id)
        assert a.lost_category == models.LostCategory.LEVEL_SCOPE
        assert a.lost_reason == "They wanted a manager of managers."


def test_a_loss_can_be_recorded_without_a_category():
    """Not knowing yet is a real state and must not be forced into a value."""
    with SessionLocal() as db:
        app_id = _make(db, "Acme Unknown Test", stage="Discovery")
        db.commit()
    _edit(app_id, stage="Closed Lost", lost_category="",
          lost_reason="Recruiter went quiet after the panel.")
    with SessionLocal() as db:
        a = db.get(models.JobApplication, app_id)
        assert a.lost_category is None
        assert a.lost_reason.startswith("Recruiter went quiet")


def test_moving_back_out_of_closed_lost_clears_both_fields():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Reopen Test", stage="Closed Lost",
                       lost_category="Compensation gap", lost_reason="too low")
        db.commit()
    _edit(app_id, stage="Discovery")
    with SessionLocal() as db:
        a = db.get(models.JobApplication, app_id)
        assert a.lost_category is None and a.lost_reason is None, (
            "a stale cause on a live pursuit would be counted by the loss "
            "breakdown, which filters on current stage")


# --------------------------------------------------------------------------- #
# The migration off the retired LostReason enum
# --------------------------------------------------------------------------- #
def _raw_lost(app_id):
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT lost_reason, lost_category FROM job_applications "
                 "WHERE id = :id"), {"id": app_id}).fetchone()


def _plant(app_id, stored):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE job_applications SET lost_reason = :v, "
                 "lost_category = NULL WHERE id = :id"),
            {"v": stored, "id": app_id})


def test_migration_translates_only_the_options_that_named_a_cause():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Migrate Ghosted", stage="Closed Lost")
        db.commit()
    # A SQLAlchemy Enum column stored the member NAME.
    _plant(app_id, "GHOSTED")
    migrate_lost_reason()
    reason, category = _raw_lost(app_id)
    assert category == "GHOSTED", "Ghosted named a cause and maps straight over"
    assert reason is None, "and needs no free text, since the category says it"


def test_migration_preserves_a_non_causal_option_as_words():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Migrate Screen", stage="Closed Lost")
        db.commit()
    _plant(app_id, "REJECTED_AFTER_SCREEN")
    migrate_lost_reason()
    reason, category = _raw_lost(app_id)
    assert category is None, (
        "'Rejected after screen' says when, not why -- guessing a cause here "
        "would put fabrication into the loss breakdown")
    assert reason == "Rejected after screen", "but the fact itself survives"


def test_migration_leaves_an_ambiguous_option_for_a_human():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Migrate Declined", stage="Closed Lost")
        db.commit()
    _plant(app_id, "DECLINED_BY_ME")
    migrate_lost_reason()
    reason, category = _raw_lost(app_id)
    assert category is None, (
        "'Declined by me' could be either withdrawal, and the two point in "
        "opposite directions")
    assert reason == "Declined by me"


def test_migration_handles_the_value_form_as_well_as_the_name_form():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Migrate ValueForm", stage="Closed Lost")
        db.commit()
    _plant(app_id, "Role closed / paused")
    migrate_lost_reason()
    _, category = _raw_lost(app_id)
    assert category == "ROLE_CLOSED"


def test_migration_never_overwrites_a_category_a_human_chose():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Migrate Human", stage="Closed Lost")
        db.commit()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE job_applications SET lost_reason = 'GHOSTED', "
                 "lost_category = 'COMPENSATION' WHERE id = :id"),
            {"id": app_id})
    migrate_lost_reason()
    _, category = _raw_lost(app_id)
    assert category == "COMPENSATION", (
        "someone could have edited the record between the deploy and this run")


def test_migration_is_idempotent_and_leaves_free_text_alone():
    with SessionLocal() as db:
        app_id = _make(db, "Acme Migrate Idempotent", stage="Closed Lost")
        db.commit()
    _plant(app_id, "REJECTED_AFTER_ONSITE")
    migrate_lost_reason()
    first = _raw_lost(app_id)
    migrate_lost_reason()
    migrate_lost_reason()
    assert _raw_lost(app_id) == first, "running it again must be a no-op"

    typed = "They went with an internal candidate, per Dana."
    _plant(app_id, typed)
    migrate_lost_reason()
    reason, _ = _raw_lost(app_id)
    assert reason == typed, "text a human typed must never be rewritten"


def test_the_board_renders_a_migrated_row_without_raising():
    """The failure this guards is a LookupError inside a template render.

    An un-migrated enum value reaching an Enum column takes the whole board
    down rather than degrading one card -- the same class of failure as the
    aware/naive datetime hazard, and this project has shipped that one before.
    """
    assert client.get("/board").status_code == 200
    assert client.get("/analytics").status_code == 200


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS  {}".format(t.__name__))
        passed += 1
    print("\n{}/{} analytics route assertions passed.".format(passed, len(tests)))
