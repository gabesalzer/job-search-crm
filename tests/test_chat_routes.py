"""End-to-end tests for the chat, against the real app.

Same shape and same reasoning as `test_thread_read_routes.py`: boot the real
app against a temporary SQLite database with only the network call stubbed.
The corpus builder itself is covered by `test_chat.py` with literals; what can
only be checked by running is the part in between -- that the ORM adapter walks
every relationship into the shape the builder expects, that the cached block
actually reaches the client, and that a failed call doesn't lose your question.

The ORM adapter is exactly the kind of code that reads correct and isn't. Four
real bugs in the thread-read feature were found this way and none by reading.

Run: python3 tests/test_chat_routes.py
"""
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-chat-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
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

client = TestClient(app)

# Everything the stub was handed on the last call, so the assertions can look
# at what would actually have gone over the wire.
SENT = {"system": None, "messages": None, "kwargs": None}
REPLY = {"text": "Two have gone quiet.", "usage": {"cache_read_input_tokens": 88_000}}
CALLS = {"n": 0}


def _fake_generate(system, messages, **kwargs):
    CALLS["n"] += 1
    SENT["system"] = system
    SENT["messages"] = messages
    SENT["kwargs"] = kwargs
    if isinstance(REPLY["text"], Exception):
        raise REPLY["text"]
    out = kwargs.get("usage_out")
    if out is not None and REPLY["usage"]:
        out.update(REPLY["usage"])
    return REPLY["text"], "test-model"


ui.llm.generate = _fake_generate


def _reset(reply="Two have gone quiet."):
    REPLY["text"] = reply
    CALLS["n"] = 0
    SENT.update({"system": None, "messages": None, "kwargs": None})
    with SessionLocal() as db:
        db.query(models.ChatMessage).delete()
        db.commit()


def _ask(question):
    resp = client.post("/ui/chat", data={"question": question},
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp


def _messages():
    with SessionLocal() as db:
        return [(m.role, m.content, m.model, m.usage)
                for m in db.query(models.ChatMessage)
                .order_by(models.ChatMessage.id).all()]


def _corpus():
    """The corpus block's text, as the route would build it."""
    return SENT["system"][1]["text"]


def _seed():
    """One application carrying every relationship the adapter walks.

    Deliberately mixed-awareness: `meeting_date` is naive (as a form-entered
    date comes back) while `rated_at` and the `_utcnow` columns are aware. That
    mix is the hazard that has taken a page down before, and it only exists
    once real ORM rows are involved -- which is why it is seeded here rather
    than in the stdlib test.
    """
    with SessionLocal() as db:
        if db.query(models.JobApplication).first():
            return
        company = models.Company(name="Condor")
        db.add(company)
        db.flush()
        posting = models.JobPosting(company_id=company.id, title="RevOps Lead",
                                    jd_text="Own forecasting and pipeline hygiene.",
                                    location="Remote")
        resume = models.Resume(label="Resume v3", content="Operations leader")
        db.add_all([posting, resume])
        db.flush()
        appn = models.JobApplication(
            company_id=company.id, title="RevOps Lead",
            stage=models.Stage.DISCOVERY, job_posting_id=posting.id,
            resume_id=resume.id, applied_date=datetime(2026, 7, 1),
            context="Team is four people.", champion=True,
            source=models.ApplicationSource.REFERRAL,
        )
        db.add(appn)
        db.flush()
        person = models.Person(name="Dana Reyes", email="dana@condor.test",
                               company_id=company.id, application_id=appn.id,
                               role=models.PersonRole.RECRUITER)
        db.add(person)
        db.add(models.Meeting(
            application_id=appn.id, title="Panel",
            meeting_type=models.MeetingType.HIRING_MANAGER,
            meeting_date=datetime(2026, 7, 29),
            transcript="Dana: how would you rebuild the forecast?",
            summary="Went well", my_performance=70, employer_engagement=80,
        ))
        thread = models.EmailThread(
            application_id=appn.id, subject="Next steps",
            body="We will come back to you next week.",
            last_message_at=datetime(2026, 7, 30),
            my_performance=55, employer_engagement=78,
            rating_source="model", rating_note="named a date unprompted",
            rated_at=datetime.now(timezone.utc), rating_model="test-model",
        )
        db.add(thread)
        db.commit()


_seed()


# --------------------------------------------------------------------------- #
def test_the_page_renders_before_anything_is_asked():
    _reset()
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert "Nothing asked yet" in resp.text
    assert CALLS["n"] == 0, "loading the page must not cost an API call"


def test_asking_stores_both_turns_and_shows_the_answer():
    _reset()
    _ask("Which pursuits have gone quiet?")
    rows = _messages()
    assert [r[0] for r in rows] == ["user", "assistant"]
    assert rows[0][1] == "Which pursuits have gone quiet?"
    assert rows[1][1] == "Two have gone quiet."
    assert rows[1][2] == "test-model", "the model that answered is recorded"
    assert json.loads(rows[1][3])["cache_read_input_tokens"] == 88_000
    body = client.get("/chat").text
    assert "Two have gone quiet." in body


def test_the_corpus_block_is_marked_cacheable():
    _reset()
    _ask("anything")
    system = SENT["system"]
    assert isinstance(system, list) and len(system) == 2
    assert "cache_control" not in system[0], "instructions stay uncached"
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_the_adapter_walks_every_relationship_onto_the_record():
    _reset()
    _ask("anything")
    corpus = _corpus()
    for expected in [
        "Condor",                                   # company
        "RevOps Lead",                              # title and posting title
        "Stage: Discovery",
        "Source: Referral",
        "Champion inside: yes",
        "Resume used: Resume v3",
        "Context: Team is four people.",
        "forecasting and pipeline hygiene",         # JD text
        "Dana Reyes",                               # person
        "rebuild the forecast",                     # meeting transcript
        "come back to you next week",               # thread body
        "Rating written by: an automatic read",     # thread rating provenance
        "Stage change",                             # stage history, auto-written
    ]:
        assert expected in corpus, "missing from the corpus: {}".format(expected)


def test_mixed_aware_and_naive_dates_do_not_raise():
    """The seeded row has a naive meeting_date and an aware rated_at.

    Sorting or subtracting across the two raises TypeError inside the corpus
    builder, which in a route means a 500 on a page that looks unrelated. This
    is the only test that exercises the mix with real columns.
    """
    _reset()
    _ask("anything")            # would have raised on the way in
    assert "quiet " in _corpus()


def test_history_is_replayed_and_the_new_question_is_last():
    _reset()
    _ask("first question")
    _ask("second question")
    roles = [m["role"] for m in SENT["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert SENT["messages"][0]["content"] == "first question"
    assert SENT["messages"][-1]["content"] == "second question"


def test_a_failed_call_keeps_the_question():
    """The row is written before the call, so a timeout doesn't lose it.

    Retyping a long question because the model timed out is a second insult on
    top of the first, and the row is true either way -- you did ask it.
    """
    _reset()
    _ask("a question that will be answered")
    REPLY["text"] = ui.llm.LLMError("API returned 429: rate limited")
    resp = _ask("a question that will fail")
    assert "error=" in resp.headers["location"]
    rows = _messages()
    assert rows[-1] == ("user", "a question that will fail", None, None)
    body = client.get(resp.headers["location"]).text
    assert "rate limited" in body

    # And asking again must not send two user turns back to back -- the shape
    # only a failed call can produce, and the one the API rejects.
    REPLY["text"] = "recovered"
    _ask("asking again")
    roles = [m["role"] for m in SENT["messages"]]
    assert roles == ["user", "assistant", "user"], roles
    assert "a question that will fail" in SENT["messages"][-1]["content"]


def test_an_empty_question_costs_nothing():
    _reset()
    before = CALLS["n"]
    client.post("/ui/chat", data={"question": "   "}, follow_redirects=False)
    assert CALLS["n"] == before
    assert _messages() == []


def test_clearing_deletes_the_conversation():
    _reset()
    _ask("something quotable about a third party")
    assert len(_messages()) == 2
    resp = client.post("/ui/chat/clear", follow_redirects=False)
    assert resp.status_code == 303
    assert _messages() == []


def test_the_nav_link_is_present_on_another_page():
    resp = client.get("/board")
    assert 'href="/chat"' in resp.text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS  {}".format(t.__name__))
        passed += 1
    print("\n{}/{} chat route assertions passed.".format(passed, len(tests)))
