"""The Log tab end to end, with the model call stubbed.

The interesting behaviour here is not the happy path — it is that the write
only ever happens for a box that was ticked. Everything else in this app writes
what you typed; this is the one feature where something else decides what to
write, and the tests that matter are the ones proving it cannot do so alone.

`llm.generate` is monkeypatched rather than called. That keeps the suite free
and offline, and it also lets a reply be *deliberately* wrong — proposing a
change to a deleted record, to a field a note may not touch, to a stage that
does not exist — which is the only way to test the refusals against the real
routes rather than against the parser in isolation.

Run: python3 tests/test_log_routes.py
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-log-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import ui as ui_module  # noqa: E402
from app.services import llm  # noqa: E402

client = TestClient(app)

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


# --------------------------------------------------------------------------- #
# A stub standing in for the model
# --------------------------------------------------------------------------- #
_reply = {"text": ""}


def _fake_generate(system, messages, **kwargs):
    if kwargs.get("usage_out") is not None:
        kwargs["usage_out"]["input_tokens"] = 1
    if isinstance(_reply["text"], Exception):
        raise _reply["text"]
    _reply["last_packet"] = messages[0]["content"]
    _reply["last_system"] = system
    return _reply["text"], "claude-test-1"


llm.generate = _fake_generate
ui_module.llm.generate = _fake_generate


def say(changes, unmatched=None, prose="Heard an update."):
    payload = {"changes": changes}
    if unmatched:
        payload["unmatched"] = unmatched
    _reply["text"] = "{}\n```changes\n{}\n```".format(prose, json.dumps(payload))


def _seed():
    with SessionLocal() as db:
        if db.query(models.JobApplication).first():
            return
        condor = models.Company(name="Condor")
        sierra = models.Company(name="Sierra")
        closed = models.Company(name="Northwind")
        db.add_all([condor, sierra, closed])
        db.flush()
        db.add(models.JobApplication(company_id=condor.id,
                                     title="Head of RevOps",
                                     next_steps="Wait for Todd"))
        db.add(models.JobApplication(company_id=sierra.id,
                                     title="RevOps Manager"))
        db.add(models.JobApplication(company_id=closed.id, title="Old role",
                                     stage=models.Stage.CLOSED_LOST))
        db.commit()


_seed()


def _app_id(company):
    with SessionLocal() as db:
        row = (db.query(models.JobApplication)
               .join(models.Company)
               .filter(models.Company.name == company).first())
        return row.id


def _field(app_id, field):
    with SessionLocal() as db:
        return getattr(db.get(models.JobApplication, app_id), field)


def _stage(app_id):
    with SessionLocal() as db:
        return db.get(models.JobApplication, app_id).stage


CONDOR = _app_id("Condor")
SIERRA = _app_id("Sierra")
NORTHWIND = _app_id("Northwind")


def submit(note):
    """Post a note and return the created entry id."""
    resp = client.post("/ui/log", data={"note": note}, follow_redirects=False)
    return int(resp.headers["location"].split("entry_id=")[1])


def proposal(entry_id):
    with SessionLocal() as db:
        return json.loads(db.get(models.LogEntry, entry_id).proposal or "[]")


# --------------------------------------------------------------------------- #
# The page exists and is in the nav
# --------------------------------------------------------------------------- #
page = client.get("/log")
check("the log page renders", page.status_code == 200)
check("...and says nothing is written until you approve",
      "Nothing is written until you" in page.text)
check("Log is in the nav", 'href="/log"' in client.get("/board").text)


# --------------------------------------------------------------------------- #
# Proposing writes nothing
# --------------------------------------------------------------------------- #
say([{"application": CONDOR, "field": "stage", "value": "Discovery",
      "why": "panel being scheduled"},
     {"application": CONDOR, "field": "next_steps",
      "value": "Send Todd the deck by Friday", "why": "committed to it"}])
entry = submit("Condor screen went well, panel next week, sending the deck.")

check("the note is stored", _field(CONDOR, "next_steps") == "Wait for Todd")
check("proposing does not move the stage",
      _stage(CONDOR) == models.Stage.QUALIFICATION)
check("two changes were proposed", len(proposal(entry)) == 2)

review = client.get("/log?entry_id={}".format(entry))
check("the review screen shows the proposed value",
      "Send Todd the deck by Friday" in review.text)
check("the review screen shows the value being replaced",
      "Wait for Todd" in review.text)
check("the review screen shows why, so you can decide without re-reading",
      "committed to it" in review.text)
check("the review screen quotes the note back", "panel next week" in review.text)


# --------------------------------------------------------------------------- #
# Only ticked boxes are written
# --------------------------------------------------------------------------- #
keys = [c["key"] for c in proposal(entry)]
stage_key = [k for k in keys if k.endswith(":stage")][0]
client.post("/ui/log/{}/apply".format(entry), data={"approve": stage_key},
            follow_redirects=False)

check("the approved change is written", _stage(CONDOR) == models.Stage.DISCOVERY)
check("the change that was not ticked is not written",
      _field(CONDOR, "next_steps") == "Wait for Todd")

with SessionLocal() as db:
    row = db.get(models.LogEntry, entry)
    applied = json.loads(row.applied or "[]")
check("the entry records only what was applied", len(applied) == 1)
check("...and is marked applied", row.status == "applied")
check("the raw note is kept after applying", "panel next week" in row.text)

with SessionLocal() as db:
    history = (db.query(models.StageHistory)
               .filter(models.StageHistory.application_id == CONDOR).all())
check("a stage moved by note lands in the funnel history like any other",
      any(h.to_stage == models.Stage.DISCOVERY for h in history))
check("applying a note counts as activity",
      _field(CONDOR, "last_activity_date") is not None)


# --------------------------------------------------------------------------- #
# Applying none keeps the note and says it was reviewed
# --------------------------------------------------------------------------- #
say([{"application": SIERRA, "field": "risks",
      "value": "The VP who owns this is leaving in Q1", "why": "said so"}])
entry = submit("Sierra's VP is on the way out.")
client.post("/ui/log/{}/discard".format(entry), follow_redirects=False)

check("discarding writes nothing", _field(SIERRA, "risks") is None)
with SessionLocal() as db:
    row = db.get(models.LogEntry, entry)
check("a discarded note is marked discarded", row.status == "discarded")
check("...with an empty applied list, not a NULL one — reviewed and rejected "
      "must stay distinguishable from never reviewed",
      row.applied == "[]")
check("the note itself survives being discarded", "VP is on the way out" in row.text)


# --------------------------------------------------------------------------- #
# Appending, and the re-read at write time
# --------------------------------------------------------------------------- #
say([{"application": SIERRA, "field": "notes", "value": "First entry."}])
entry = submit("Note one about Sierra.")
client.post("/ui/log/{}/apply".format(entry),
            data={"approve": proposal(entry)[0]["key"]}, follow_redirects=False)
check("an append onto an empty field is just the value",
      _field(SIERRA, "notes") == "First entry.")

say([{"application": SIERRA, "field": "notes", "value": "Second entry."}])
entry = submit("Note two about Sierra.")
# Edit by hand in between, exactly as a pending note left overnight would meet.
with SessionLocal() as db:
    obj = db.get(models.JobApplication, SIERRA)
    obj.notes = "First entry.\n\nTyped by hand in between."
    db.commit()
client.post("/ui/log/{}/apply".format(entry),
            data={"approve": proposal(entry)[0]["key"]}, follow_redirects=False)
notes = _field(SIERRA, "notes")
check("appending re-reads the field at write time, so a hand edit made "
      "between proposing and approving is not lost",
      "Typed by hand in between." in notes)
check("...and the newest entry is first", notes.startswith("Second entry."))


# --------------------------------------------------------------------------- #
# What a note may not do, tested against the real routes
# --------------------------------------------------------------------------- #
say([{"application": CONDOR, "field": "champion", "value": "true"}])
entry = submit("The hiring manager seemed really into it.")
check("a note cannot set champion, whatever the model proposes",
      proposal(entry) == [])
check("...and the page says what was dropped",
      "champion" in client.get("/log?entry_id={}".format(entry)).text)

say([{"application": CONDOR, "field": "next_steps", "value": ""}])
entry = submit("Nothing to do on Condor now.")
check("a note cannot clear a field", proposal(entry) == [])
check("the field is untouched", _field(CONDOR, "next_steps") == "Wait for Todd")

say([{"application": CONDOR, "field": "stage", "value": "Panel"}])
entry = submit("Condor moved me to a panel round.")
check("a note cannot invent a stage", proposal(entry) == [])

say([{"application": NORTHWIND, "field": "notes", "value": "Reopened?"}])
entry = submit("Northwind came back to me.")
check("a closed application is not offered to a note at all",
      proposal(entry) == [])
# The company name is in the note itself, so the packet naturally contains
# the word. What must be absent is the *record* — its title only appears in
# the application listing.
check("...and the packet never listed it as a record",
      "Old role" not in _reply.get("last_packet", ""))

say([{"application": 4242, "field": "notes", "value": "Ghost."}])
entry = submit("Something about a company I have not applied to.")
check("a change to an application that does not exist is dropped",
      proposal(entry) == [])


# --------------------------------------------------------------------------- #
# The expected close date, end to end
# --------------------------------------------------------------------------- #
from datetime import datetime, timedelta  # noqa: E402

soon = (datetime.utcnow() + timedelta(days=30)).date().isoformat()

say([{"application": SIERRA, "field": "expected_close_date", "value": soon,
      "why": "said they would decide in about a month"}])
entry = submit("Sierra reckon they'll have a decision in about a month.")
check("a close date is proposed", len(proposal(entry)) == 1)
check("proposing it does not set it", _field(SIERRA, "expected_close_date") is None)

page = client.get("/log?entry_id={}".format(entry))
check("the review screen shows the proposed date", soon in page.text)

client.post("/ui/log/{}/apply".format(entry),
            data={"approve": proposal(entry)[0]["key"]}, follow_redirects=False)
check("an approved date is written",
      _field(SIERRA, "expected_close_date").date().isoformat() == soon)

with SessionLocal() as db:
    rows = db.query(models.CloseDateHistory).filter(
        models.CloseDateHistory.application_id == SIERRA).all()
check("a date set by note is logged in the slip history like any other",
      len(rows) == 1 and rows[0].from_date is None)

later = (datetime.utcnow() + timedelta(days=60)).date().isoformat()
say([{"application": SIERRA, "field": "expected_close_date", "value": later,
      "why": "it slipped"}])
entry = submit("Sierra has slipped again, now looking like two months out.")
client.post("/ui/log/{}/apply".format(entry),
            data={"approve": proposal(entry)[0]["key"]}, follow_redirects=False)
with SessionLocal() as db:
    rows = db.query(models.CloseDateHistory).filter(
        models.CloseDateHistory.application_id == SIERRA).all()
check("a date moved by note records both ends of the move",
      len(rows) == 2 and rows[1].from_date is not None)

say([{"application": SIERRA, "field": "expected_close_date", "value": "soon"}])
entry = submit("Sierra said they'd get back to me soon.")
check("a vague date is not turned into a real one", proposal(entry) == [])
check("...and the page explains why rather than staying silent",
      "say the day" in client.get("/log?entry_id={}".format(entry)).text)

check("the packet tells the model what today is",
      datetime.utcnow().date().isoformat() in str(_reply.get("last_system", "")))


# --------------------------------------------------------------------------- #
# Unmatched is passed through, because it is the most useful thing said
# --------------------------------------------------------------------------- #
say([], unmatched=["mentioned a Vercel recruiter — no application on file"])
entry = submit("A Vercel recruiter reached out.")
page = client.get("/log?entry_id={}".format(entry))
check("something the model could not place is shown to you",
      "Vercel recruiter" in page.text)
check("...under a heading that says so", "Couldn't place" in page.text)


# --------------------------------------------------------------------------- #
# The note survives a failed call — the one thing a capture tool must not lose
# --------------------------------------------------------------------------- #
_reply["text"] = llm.LLMError("API returned 500")
entry = submit("Something I said while the API was down.")
with SessionLocal() as db:
    row = db.get(models.LogEntry, entry)
check("a note whose read failed is still stored",
      "while the API was down" in row.text)
check("...stays pending so it can be retried", row.status == "pending")
check("...and carries the error", "500" in (row.rejected or ""))

_reply["text"] = "no fenced block here at all"
entry = submit("A note the model answered in prose.")
check("a reply with no changes block proposes nothing", proposal(entry) == [])
with SessionLocal() as db:
    check("...and is reported rather than looking like a note about nothing",
          "no changes block" in (db.get(models.LogEntry, entry).rejected or ""))


# --------------------------------------------------------------------------- #
# The API door: same three steps, no exception to the review gate
# --------------------------------------------------------------------------- #
say([{"application": CONDOR, "field": "risks",
      "value": "Comp band is light against my number", "why": "said comp came up"}])
resp = client.post("/api/log", json={"text": "Comp came up on Condor and it's light."})
check("the API accepts a note", resp.status_code == 201)
body = resp.json()
check("the API reports what it proposed", body["proposed"] == 1)
check("the API leaves it pending — automation gets no exception to the gate",
      body["status"] == "pending")
check("the API hands back somewhere for a person to go",
      body["review_url"].endswith(str(body["id"])))
check("the API wrote nothing to the record", _field(CONDOR, "risks") is None)

pending = client.get("/api/log/pending").json()
check("a note posted by API shows up as pending",
      any(p["id"] == body["id"] for p in pending))
check("the page surfaces it as waiting on you",
      "Waiting on you" in client.get("/log").text)

client.post("/ui/log/{}/apply".format(body["id"]),
            data={"approve": proposal(body["id"])[0]["key"]},
            follow_redirects=False)
check("and it applies from the page like any other",
      _field(CONDOR, "risks") == "Comp band is light against my number")

check("an empty note is refused by the API",
      client.post("/api/log", json={"text": "   "}).status_code == 400)
check("an empty note in the form is a no-op",
      client.post("/ui/log", data={"note": "  "},
                  follow_redirects=False).headers["location"] == "/log")


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
page = client.get("/log")
check("recent notes are listed", "Recent notes" in page.text)
check("...showing what you actually said, so you can see a double-entry",
      "Comp came up on Condor" in page.text)
check("the history is capped rather than growing without limit",
      page.text.count('class="when"') <= ui_module.LOG_HISTORY)

with SessionLocal() as db:
    victim = db.query(models.LogEntry).order_by(models.LogEntry.id).first().id
client.post("/ui/log/{}/delete".format(victim), follow_redirects=False)
with SessionLocal() as db:
    check("a note can be deleted", db.get(models.LogEntry, victim) is None)


print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
