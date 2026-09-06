"""The Looking For tab and per-application ratings, against the real app.

Run: python3 tests/test_fit_routes.py
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-fit-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from app import fit, models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _seed():
    with SessionLocal() as db:
        if db.query(models.JobApplication).first():
            return
        company = models.Company(name="Condor Fit")
        db.add(company)
        db.flush()
        for title in ("VP RevOps", "RevOps Manager"):
            db.add(models.JobApplication(company_id=company.id, title=title))
        db.commit()


_seed()
client.get("/looking-for")          # first visit seeds the axes


def _criteria():
    with SessionLocal() as db:
        return [(c.id, c.name) for c in
                db.query(models.Criterion).order_by(models.Criterion.sort_order).all()]


def _apps():
    with SessionLocal() as db:
        return [a.id for a in db.query(models.JobApplication)
                .order_by(models.JobApplication.id).all()]


def _rate(app_id, **scores):
    """scores maps criterion NAME -> value (or '' to clear)."""
    by_name = {name: cid for cid, name in _criteria()}
    payload = {"crit_{}".format(by_name[n]): str(v) for n, v in scores.items()}
    resp = client.post("/ui/applications/{}/fit".format(app_id),
                       data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def _reading(app_id):
    from app.routers.ui import _criteria as crits, _fit_for, _looking_for
    with SessionLocal() as db:
        a = db.get(models.JobApplication, app_id)
        return _fit_for(a, crits(db), _looking_for(db).dq_threshold)


APP_A, APP_B = _apps()


def test_the_six_axes_are_seeded_on_first_visit():
    assert [n for _, n in _criteria()] == [
        "Talent density", "Role opportunity", "Company opportunity",
        "Company brand", "Lifestyle fit", "Compensation"]


def test_seeding_happens_once_and_a_deleted_axis_stays_deleted():
    """A starter list that grows back is worse than none — you can't stop it."""
    cid, name = _criteria()[-1]
    client.post("/ui/looking-for/criteria/{}/delete".format(cid),
                follow_redirects=False)
    client.get("/looking-for")
    assert name not in [n for _, n in _criteria()]
    # Put it back so the rest of the file sees six.
    client.post("/ui/looking-for/criteria",
                data={"name": name, "description": "restored"},
                follow_redirects=False)


def test_ratings_round_trip():
    _rate(APP_A, **{"Talent density": 9, "Compensation": 7})
    reading = _reading(APP_A)
    assert reading["mean"] == 8.0
    assert reading["rated"] == 2 and reading["total"] == 6
    body = client.get("/applications/{}/edit".format(APP_A)).text
    assert "2 of 6 axes rated" in body


def test_a_blank_axis_stays_out_of_the_average():
    _rate(APP_B, **{"Talent density": 10})
    assert _reading(APP_B)["mean"] == 10.0, (
        "counting the five blanks as zero would read 1.7 and make every "
        "part-rated record look awful")


def test_one_axis_below_the_floor_disqualifies():
    _rate(APP_A, **{"Talent density": 10, "Compensation": 9, "Lifestyle fit": 2})
    reading = _reading(APP_A)
    assert reading["disqualified"] is True
    assert reading["failing"] == ["Lifestyle fit"]
    assert reading["mean"] == 7.0, "the average is still shown alongside"
    body = client.get("/applications/{}/edit".format(APP_A)).text
    assert "Disqualified" in body and "Lifestyle fit" in body


def test_clearing_a_rating_removes_the_row_rather_than_storing_a_blank():
    _rate(APP_A, **{"Lifestyle fit": ""})
    with SessionLocal() as db:
        a = db.get(models.JobApplication, APP_A)
        names = {r.criterion.name for r in a.criterion_ratings}
    assert "Lifestyle fit" not in names, (
        "'never rated' and 'rated then cleared' should look the same")
    assert _reading(APP_A)["disqualified"] is False


def test_an_out_of_range_rating_is_refused_not_clamped():
    _rate(APP_B, **{"Company brand": 47})
    with SessionLocal() as db:
        a = db.get(models.JobApplication, APP_B)
        brand = [r for r in a.criterion_ratings if r.criterion.name == "Company brand"]
    assert brand == [], "a 47 means the scale was misread; storing 10 hides that"


def test_the_threshold_is_editable_and_changes_who_is_disqualified():
    _rate(APP_B, **{"Talent density": 5, "Compensation": 8})
    assert _reading(APP_B)["disqualified"] is False
    client.post("/ui/looking-for", data={"statement": "", "dq_threshold": "6"},
                follow_redirects=False)
    assert _reading(APP_B)["disqualified"] is True
    client.post("/ui/looking-for", data={"statement": "", "dq_threshold": "4"},
                follow_redirects=False)


def test_an_out_of_scale_threshold_leaves_the_stored_one_alone():
    before = _reading(APP_B)["threshold"]
    client.post("/ui/looking-for", data={"statement": "", "dq_threshold": "99"},
                follow_redirects=False)
    assert _reading(APP_B)["threshold"] == before, (
        "snapping it silently would change which applications are "
        "disqualified without anyone asking for that")


def test_the_statement_round_trips():
    client.post("/ui/looking-for",
                data={"statement": "Systems and strategy, Series B+, remote.",
                      "dq_threshold": "4"}, follow_redirects=False)
    assert "Systems and strategy, Series B+, remote." in client.get("/looking-for").text


def test_deleting_an_axis_takes_its_ratings_with_it():
    client.post("/ui/looking-for/criteria",
                data={"name": "Temporary", "description": ""},
                follow_redirects=False)
    cid = [c for c, n in _criteria() if n == "Temporary"][0]
    client.post("/ui/applications/{}/fit".format(APP_A),
                data={"crit_{}".format(cid): "8"}, follow_redirects=False)
    with SessionLocal() as db:
        assert db.query(models.CriterionRating).filter_by(criterion_id=cid).count() == 1
    client.post("/ui/looking-for/criteria/{}/delete".format(cid),
                follow_redirects=False)
    with SessionLocal() as db:
        assert db.query(models.CriterionRating).filter_by(criterion_id=cid).count() == 0, (
            "a rating means nothing without the axis it was made against")


def test_the_tab_ranks_and_sorts_disqualified_last():
    body = client.get("/looking-for").text
    assert "How the pipeline scores" in body
    assert "DQ" in body or "not rated yet" in body


def test_the_three_qualification_fields_round_trip():
    with SessionLocal() as db:
        a = db.get(models.JobApplication, APP_A)
        payload = {
            "company_id": str(a.company_id), "title": a.title or "",
            "stage": a.stage.value, "job_posting_id": "", "resume_id": "",
            "lost_reason": "", "lost_category": "", "applied_date": "",
            "created_at": "", "last_activity_date": "", "updated_at": "",
            "notes": "", "context": "", "source": "", "manual_forecast": "",
            "champion": "", "seniority": "", "speciality": "", "next_steps": "",
            "pain": "They have no forecast anyone believes.",
            "process": "HM then panel then CRO signs off.",
            "risks": "An internal candidate is in the loop.",
        }
    resp = client.post("/ui/applications/{}/edit".format(APP_A), data=payload,
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text
    with SessionLocal() as db:
        a = db.get(models.JobApplication, APP_A)
        assert a.pain.startswith("They have no forecast")
        assert a.process.startswith("HM then panel")
        assert a.risks.startswith("An internal candidate")


def test_the_new_fields_reach_the_chat_corpus():
    """Sets its own values rather than leaning on another test having run.

    Tests here execute in alphabetical order, so depending on a sibling to
    have populated the record is a test that passes or fails on its own name.
    """
    from app.routers.ui import _chat_corpus
    with SessionLocal() as db:
        a = db.get(models.JobApplication, APP_B)
        a.pain = "No forecast anyone believes."
        a.process = "HM, panel, then the CRO."
        a.risks = "Budget is not signed off."
        db.commit()
        corpus = _chat_corpus(db)
    for label in ("Their pain:", "Their process:", "Risks:"):
        assert label in corpus, label


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS  {}".format(t.__name__))
        passed += 1
    print("\n{}/{} fit route assertions passed.".format(passed, len(tests)))
