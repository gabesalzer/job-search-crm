"""Next steps: where it lives, and that it survives a round trip.

The interesting part is not the column. It is that this field was originally
proposed on JobPosting, and the reason it is on JobApplication instead is
structural rather than aesthetic — so the structure is what gets pinned here.
If someone later moves it back, these fail and say why.

Run: python3 tests/test_next_steps.py
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-nextsteps-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _seed():
    with SessionLocal() as db:
        if db.query(models.JobApplication).first():
            return
        company = models.Company(name="Condor Next")
        db.add(company)
        db.flush()
        posting = models.JobPosting(company_id=company.id, title="RevOps Lead",
                                    jd_text="Own the forecast.")
        db.add(posting)
        db.flush()
        # One with a posting, one without. The second is the case that decided
        # which object this field belongs to.
        db.add(models.JobApplication(company_id=company.id, title="With posting",
                                     job_posting_id=posting.id))
        db.add(models.JobApplication(company_id=company.id, title="No posting"))
        db.commit()


_seed()


def _apps():
    with SessionLocal() as db:
        return [a.id for a in db.query(models.JobApplication)
                .order_by(models.JobApplication.id).all()]


WITH_POSTING, WITHOUT_POSTING = _apps()


def _row(app_id):
    with SessionLocal() as db:
        return db.get(models.JobApplication, app_id)


def _edit(app_id, **form):
    a = _row(app_id)
    payload = {
        "company_id": str(a.company_id), "title": a.title or "",
        "stage": a.stage.value,
        "job_posting_id": str(a.job_posting_id) if a.job_posting_id else "",
        "resume_id": "", "lost_reason": "", "lost_category": "",
        "applied_date": "", "created_at": "", "last_activity_date": "",
        "updated_at": "", "notes": "", "context": "", "source": "",
        "manual_forecast": "", "champion": "", "seniority": "", "speciality": "",
        "next_steps": a.next_steps or "",
    }
    payload.update(form)
    resp = client.post("/ui/applications/{}/edit".format(app_id),
                       data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def test_it_lives_on_the_application_not_the_posting():
    """Pinned because it was first proposed on JobPosting.

    The board renders Applications and the posting link is optional, so a
    posting-side field would be unreachable from any card without one. And
    ARCHITECTURE.md keeps the objects apart partly because one posting can
    carry several applications -- re-applying with a new resume, or a reposted
    role -- which would make a posting-side next step shared between attempts.
    """
    assert hasattr(models.JobApplication, "next_steps")
    assert not hasattr(models.JobPosting, "next_steps"), (
        "putting it here would make it shared across re-applications and "
        "invisible on every card with no posting linked")


def test_an_application_with_no_posting_can_still_carry_one():
    """The case that settled the object choice."""
    _edit(WITHOUT_POSTING, next_steps="Find a referral before applying.")
    a = _row(WITHOUT_POSTING)
    assert a.job_posting_id is None
    assert a.next_steps == "Find a referral before applying."


def test_it_round_trips_through_the_edit_form():
    _edit(WITH_POSTING, next_steps="Send Todd the deck by Thursday.")
    assert _row(WITH_POSTING).next_steps == "Send Todd the deck by Thursday."
    body = client.get("/applications/{}/edit".format(WITH_POSTING)).text
    assert "Send Todd the deck by Thursday." in body


def test_clearing_it_stores_null_rather_than_an_empty_string():
    _edit(WITH_POSTING, next_steps="something")
    _edit(WITH_POSTING, next_steps="   ")
    a = _row(WITH_POSTING)
    assert a.next_steps is None or a.next_steps.strip() == "", a.next_steps


def test_the_board_shows_it_and_hides_it_when_empty():
    _edit(WITH_POSTING, next_steps="Chase the panel date")
    _edit(WITHOUT_POSTING, next_steps="")
    body = client.get("/board").text
    assert "Chase the panel date" in body
    # The marker only renders inside a card that has a value, so exactly one
    # card should carry the class.
    assert body.count('class="next"') == 1, (
        "a card with no next step must render no line at all, not an empty "
        "one with a bare arrow")


def test_a_long_next_step_is_not_truncated_in_the_data():
    """Truncation is a CSS concern; the stored value stays whole.

    The card clips with an ellipsis and puts the full text in the title
    attribute, so the tooltip enhances rather than gates -- and the edit page
    always has the complete value.
    """
    long = ("Send Todd the revised deck, ask for the panel date, and confirm "
            "the comp band before Friday so this does not slip another week.")
    _edit(WITH_POSTING, next_steps=long)
    assert _row(WITH_POSTING).next_steps == long
    body = client.get("/board").text
    assert 'title="{}"'.format(long) in body, "the whole value reaches the tooltip"


def test_it_reaches_the_chat_corpus():
    """Otherwise 'what did I commit to doing' cannot be answered from it."""
    from app.routers.ui import _chat_corpus
    _edit(WITH_POSTING, next_steps="Chase the panel date")
    with SessionLocal() as db:
        corpus = _chat_corpus(db)
    assert "Next steps I set myself: Chase the panel date" in corpus


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS  {}".format(t.__name__))
        passed += 1
    print("\n{}/{} next-steps assertions passed.".format(passed, len(tests)))
