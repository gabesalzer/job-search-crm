"""End-to-end tests for the automatic thread read, against the real app.

This file is different in kind from every other test here, and the difference
is worth stating. The rest of the suite is stdlib-only and tests pure functions
(`forecast.py`, `brief.py`, `thread_read.py`) or hand-mirrors ORM logic it
cannot import. That was believed to be forced: FastAPI and SQLAlchemy were
assumed to be uninstallable in the sandbox this project is developed in, so
route and ORM behaviour was reviewed by reading rather than by running.

That assumption was wrong, and it had been wrong for a long time. Every bug
this file pins was found by *running* the app, and none of them were caught by
reading the same code carefully several times -- because each one is an
interaction between three or four pieces that are individually correct.

So: this file boots the real app, against a real (temporary) SQLite database,
with only the network call stubbed. If it cannot import FastAPI it skips
loudly rather than passing silently, because a green suite that quietly stopped
testing the routes would be worse than no suite at all.

Run it like the others: `python3 tests/test_thread_read_routes.py`
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# A fresh database per run, set before anything imports app.database -- which
# reads DATABASE_URL at import time and otherwise defaults to
# ./data/jobsearch.db, i.e. your real one. These tests create and mutate rows,
# so getting this wrong would not fail loudly; it would quietly write test
# threads into your live pipeline. `load_dotenv()` in database.py does not
# override variables already set in the environment, so this wins over .env.
_TMP = tempfile.mkdtemp(prefix="jobsearch-routes-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
# A value is needed because `llm.enabled()` gates the read on one, but no call
# ever leaves: `llm.generate` is replaced below, and the replacement is on the
# module object, so every caller sees the stub.
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    print("      pip install -r requirements.txt --break-system-packages")
    raise SystemExit(0)

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import ui  # noqa: E402
from app.services import llm  # noqa: E402

client = TestClient(app)

# What the stubbed model returns next, and how many times it was asked.
REPLY = {"text": "PERFORMANCE: 55\nENGAGEMENT: 78\nREASON: named a date unprompted"}
CALLS = {"n": 0}


def _fake_generate(system, messages, **kwargs):
    CALLS["n"] += 1
    if isinstance(REPLY["text"], Exception):
        raise REPLY["text"]
    return REPLY["text"], "test-model"


ui.llm.generate = _fake_generate


def _reset(reply="PERFORMANCE: 55\nENGAGEMENT: 78\nREASON: named a date unprompted"):
    REPLY["text"] = reply
    CALLS["n"] = 0


def _a_person():
    """One Person to hang threads off.

    Thread creation requires at least one participant it can identify, either
    picked explicitly or auto-detected from a Gmail-shaped body. These tests
    are about the read, not about people resolution, so they always pick.
    """
    with SessionLocal() as db:
        found = db.query(models.Person).filter_by(email="recruiter@acme.test").first()
        if found:
            return found.id
        employer = models.Company(name="Acme Test Co")
        db.add(employer)
        db.flush()
        person = models.Person(name="A Recruiter", email="recruiter@acme.test",
                               company_id=employer.id)
        db.add(person)
        db.commit()
        return person.id


PERSON_ID = _a_person()


def _new_thread(body="Hi -- can you do Tuesday? Best, a recruiter", **form):
    payload = {"subject": "Re: the role", "body": body,
               "person_ids": str(PERSON_ID)}
    payload.update(form)
    resp = client.post("/ui/email-threads", data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    with SessionLocal() as db:
        return db.query(models.EmailThread).order_by(models.EmailThread.id.desc()).first().id


def _row(thread_id):
    with SessionLocal() as db:
        return db.get(models.EmailThread, thread_id)


def _edit(thread_id, **form):
    t = _row(thread_id)
    payload = {
        "subject": t.subject or "", "body": t.body or "", "participants": "",
        "notes": t.notes or "", "score": "", "score_reason": "",
        "my_performance": "" if t.my_performance is None else str(t.my_performance),
        "employer_engagement": "" if t.employer_engagement is None else str(t.employer_engagement),
    }
    payload.update(form)
    resp = client.post("/ui/email-threads/{}/edit".format(thread_id),
                       data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text


# --------------------------------------------------------------------------- #
def test_saving_a_new_thread_reads_it():
    _reset()
    t = _row(_new_thread())
    assert (t.my_performance, t.employer_engagement) == (55, 78)
    assert t.rating_source == "model"
    assert t.rating_model == "test-model"
    assert t.rated_at is not None
    assert "named a date" in t.rating_note
    assert CALLS["n"] == 1


def test_a_thread_you_rated_yourself_on_create_is_never_read():
    """The feature's central promise, at the earliest point it can be made."""
    _reset()
    t = _row(_new_thread(my_performance="40", employer_engagement="35"))
    assert (t.my_performance, t.employer_engagement) == (40, 35)
    assert t.rating_source is None
    assert CALLS["n"] == 0


def test_a_thread_with_no_body_is_not_read():
    _reset()
    t = _row(_new_thread(body=""))
    assert CALLS["n"] == 0
    assert t.rating_source is None


def test_an_ordinary_save_does_not_re_read_or_steal_ownership():
    """The form pre-fills these inputs and posts them back on every save. If an
    unchanged round-trip counted as a hand edit, fixing a subject line would
    silently relabel the model's reading as your own judgment."""
    _reset()
    tid = _new_thread()
    _edit(tid, subject="Re: the role (corrected)")
    t = _row(tid)
    assert t.subject.endswith("(corrected)")
    assert t.rating_source == "model"
    assert (t.my_performance, t.employer_engagement) == (55, 78)
    assert CALLS["n"] == 1, "an unchanged body must not cost a second API call"


def test_changing_a_number_takes_ownership_and_drops_the_note():
    _reset()
    tid = _new_thread()
    _edit(tid, my_performance="40")
    t = _row(tid)
    assert t.my_performance == 40
    assert t.employer_engagement == 78
    assert t.rating_source is None
    assert t.rating_note is None
    assert t.rated_at is None and t.rating_model is None


def test_clearing_both_numbers_while_editing_the_body_does_not_refill_them():
    """The three-correct-steps bug. Clearing releases ownership; the cleared
    pair then looks unrated; the body change fires a read into it -- and the
    numbers you just deleted come straight back. Each step is right and the
    composition breaks the one promise the feature makes."""
    _reset()
    tid = _new_thread()
    _edit(tid, my_performance="", employer_engagement="",
          body="Hi -- can you do Tuesday? Best, a recruiter (edited)")
    t = _row(tid)
    assert (t.my_performance, t.employer_engagement) == (None, None)
    assert t.rating_source is None
    assert CALLS["n"] == 1, "the read must be skipped on a save that touched the ratings"


def test_editing_the_body_alone_does_re_read():
    _reset()
    tid = _new_thread()
    REPLY["text"] = "PERFORMANCE: 20\nENGAGEMENT: 15\nREASON: went cold"
    _edit(tid, body="Actually, we are pausing the search.")
    t = _row(tid)
    assert (t.my_performance, t.employer_engagement) == (20, 15)
    assert CALLS["n"] == 2


def test_a_human_rating_survives_a_body_edit():
    _reset()
    tid = _new_thread(my_performance="40", employer_engagement="35")
    _edit(tid, body="A completely different thread body now.")
    t = _row(tid)
    assert (t.my_performance, t.employer_engagement) == (40, 35)
    assert CALLS["n"] == 0


def test_a_declined_read_is_stored_as_a_decline_not_as_nothing():
    _reset("PERFORMANCE: NONE\nENGAGEMENT: NONE\nREASON: pure calendar logistics")
    t = _row(_new_thread())
    assert (t.my_performance, t.employer_engagement) == (None, None)
    assert t.rating_source == "model"
    assert "logistics" in t.rating_note


def test_an_unparseable_reply_writes_nothing_at_all():
    """Not even the reason, and specifically not a fake decline."""
    _reset("PERFORMANCE: quite good\nENGAGEMENT: N/A\nREASON: hard to say")
    t = _row(_new_thread())
    assert t.rating_source is None
    assert t.rating_note is None
    assert (t.my_performance, t.employer_engagement) == (None, None)


def test_a_half_parsed_reply_writes_nothing_at_all():
    _reset("PERFORMANCE: 61\nENGAGEMENT: high\nREASON: quick replies")
    t = _row(_new_thread())
    assert (t.my_performance, t.employer_engagement) == (None, None)
    assert t.rating_source is None


def test_an_api_failure_does_not_cost_you_the_save():
    _reset(llm.LLMError("API returned 401: invalid x-api-key"))
    tid = _new_thread(body="a real thread body")
    t = _row(tid)
    assert t.subject == "Re: the role", "the thread itself must still have saved"
    assert t.body == "a real thread body"
    assert t.rating_source is None


def test_a_non_llm_exception_does_not_roll_back_the_users_edit():
    """`llm.generate` promises LLMError and cannot fully deliver -- a 200 whose
    body is not JSON raises ValueError out of `resp.json()`. Anything escaping
    reaches the handler before its commit and takes the whole edit with it."""
    _reset()
    tid = _new_thread()
    REPLY["text"] = ValueError("Expecting value: line 1 column 1 (char 0)")
    _edit(tid, subject="Fixed subject", notes="and a note",
          my_performance="", employer_engagement="",
          body="a body edit that triggers the read")
    t = _row(tid)
    assert t.subject == "Fixed subject", "the edit must survive a crashing read"
    assert t.notes == "and a note"


def test_the_read_button_backfills_an_old_thread():
    _reset()
    tid = _new_thread(body="")           # no body: never read
    with SessionLocal() as db:           # give it one behind the app's back,
        t = db.get(models.EmailThread, tid)   # standing in for a pre-feature row
        t.body = "Thanks for applying -- we would like to set up a call."
        db.commit()
    resp = client.post("/ui/email-threads/{}/read".format(tid), follow_redirects=False)
    assert resp.status_code == 303
    assert "read_error" not in resp.headers["location"]
    t = _row(tid)
    assert t.rating_source == "model"
    assert (t.my_performance, t.employer_engagement) == (55, 78)


def test_the_read_button_reports_why_it_did_nothing():
    """A silent no-op makes the button look broken. Every refusal is a message."""
    _reset()
    tid = _new_thread(body="")
    resp = client.post("/ui/email-threads/{}/read".format(tid), follow_redirects=False)
    assert "read_error" in resp.headers["location"], "an empty thread must say so"

    rated = _new_thread(my_performance="40", employer_engagement="35")
    resp = client.post("/ui/email-threads/{}/read".format(rated), follow_redirects=False)
    assert "read_error" in resp.headers["location"], "an already-rated thread must say so"
    assert _row(rated).my_performance == 40


def test_the_edit_page_renders_every_read_state():
    _reset()
    tid = _new_thread()
    body = client.get("/email-threads/{}/edit".format(tid)).text
    assert "read automatically" in body

    _edit(tid, my_performance="40")
    body = client.get("/email-threads/{}/edit".format(tid)).text
    assert "your own judgment" in body

    body = client.get("/email-threads/{}/edit?read_error=boom".format(tid)).text
    assert "boom" in body


def _an_application(title="Batch Read Co"):
    with SessionLocal() as db:
        company = models.Company(name=title)
        db.add(company)
        db.flush()
        appn = models.JobApplication(company_id=company.id, title="RevOps Lead")
        db.add(appn)
        db.commit()
        return appn.id


def test_the_batch_button_reads_every_unrated_thread_on_an_application():
    """The whole point: getting email out of 0.0/10 without re-uploading
    anything. The forecast itself needs no regenerating -- it is arithmetic
    recomputed on every page load -- so what the button fetches is evidence."""
    _reset()
    app_id = _an_application("Batch Read Co A")
    for _ in range(3):
        _new_thread(application_id=str(app_id))
    CALLS["n"] = 0  # creation already read them; pretend they predate the feature
    with SessionLocal() as db:
        appn = db.get(models.JobApplication, app_id)
        for t in appn.email_threads:
            t.my_performance = t.employer_engagement = None
            t.rating_source = t.rating_note = t.rating_model = None
            t.rated_at = None
        db.commit()

    resp = client.post("/ui/applications/{}/read-threads".format(app_id),
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "read_result" in resp.headers["location"]
    assert CALLS["n"] == 3

    with SessionLocal() as db:
        appn = db.get(models.JobApplication, app_id)
        assert all(t.rating_source == "model" for t in appn.email_threads)
        out = ui._forecast_for(appn)
    assert out["components"]["email"] > 0, "email must no longer read 0.0/10"


def test_pressing_the_batch_button_twice_does_not_buy_the_same_answer_again():
    """Idempotence is the difference between a button you can lean on and one
    that quietly costs money every time you reload a page and press it."""
    _reset()
    app_id = _an_application("Batch Read Co B")
    _new_thread(application_id=str(app_id))
    CALLS["n"] = 0
    client.post("/ui/applications/{}/read-threads".format(app_id), follow_redirects=False)
    first = CALLS["n"]
    resp = client.post("/ui/applications/{}/read-threads".format(app_id),
                       follow_redirects=False)
    assert CALLS["n"] == first, "an already-read thread must not be re-read"
    assert "nothing+to+read" in resp.headers["location"].replace("%20", "+")


def test_the_batch_button_never_touches_a_rating_you_entered():
    _reset()
    app_id = _an_application("Batch Read Co C")
    _new_thread(application_id=str(app_id), my_performance="40", employer_engagement="35")
    CALLS["n"] = 0
    client.post("/ui/applications/{}/read-threads".format(app_id), follow_redirects=False)
    assert CALLS["n"] == 0
    with SessionLocal() as db:
        appn = db.get(models.JobApplication, app_id)
        t = appn.email_threads[0]
        assert (t.my_performance, t.employer_engagement) == (40, 35)
        assert t.rating_source is None


def test_a_declined_batch_read_says_so_rather_than_looking_broken():
    """Email stays at zero after a successful press, which is indistinguishable
    from a no-op unless the page says what happened."""
    _reset("PERFORMANCE: NONE\nENGAGEMENT: NONE\nREASON: scheduling only")
    app_id = _an_application("Batch Read Co D")
    _new_thread(application_id=str(app_id))
    with SessionLocal() as db:
        appn = db.get(models.JobApplication, app_id)
        for t in appn.email_threads:
            t.rating_source = None
        db.commit()
    resp = client.post("/ui/applications/{}/read-threads".format(app_id),
                       follow_redirects=False)
    assert "no+signal" in resp.headers["location"].replace("%20", "+")


def test_the_button_only_renders_when_it_would_do_something():
    _reset()
    app_id = _an_application("Batch Read Co E")
    tid = _new_thread(application_id=str(app_id))
    body = client.get("/applications/{}/edit".format(app_id)).text
    assert "unrated email" not in body, "already read on save; nothing to offer"

    with SessionLocal() as db:
        t = db.get(models.EmailThread, tid)
        t.my_performance = t.employer_engagement = None
        t.rating_source = None
        db.commit()
    body = client.get("/applications/{}/edit".format(app_id)).text
    assert "Read 1 unrated email thread" in body


def test_the_forecast_reads_a_model_written_rating_like_any_other():
    """The point of the whole exercise: provenance is a fact about the record,
    not a variable in the arithmetic. `forecast.py` stays stdlib-only and never
    learns that a model exists."""
    _reset()
    with SessionLocal() as db:
        company = models.Company(name="Acme Route Test")
        db.add(company)
        db.flush()
        appn = models.JobApplication(company_id=company.id, title="RevOps Lead")
        db.add(appn)
        db.commit()
        app_id = appn.id
    _new_thread(application_id=str(app_id))
    with SessionLocal() as db:
        appn = db.get(models.JobApplication, app_id)
        out = ui._forecast_for(appn)
    assert out["components"]["email"] > 0
    assert out["components"]["scored_threads"] == 1
    assert out["components"]["email_quality"] == 0.6 * 78 + 0.4 * 55


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS  {}".format(t.__name__))
        passed += 1
    print("\n{}/{} thread-read route assertions passed.".format(passed, len(tests)))
