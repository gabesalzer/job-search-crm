"""Standalone Jinja2 render smoke-test for the new/edited templates.

Doesn't need FastAPI/SQLAlchemy/the app package — just renders each template
with lightweight mock objects that have the same attributes the real
SQLAlchemy models expose, to catch Jinja syntax errors and typo'd attribute
references before deploying.
"""
import pathlib
import sys
from datetime import datetime
from types import SimpleNamespace

import jinja2

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)))


class Enum(SimpleNamespace):
    pass


def enum(value):
    return Enum(value=value)


company = SimpleNamespace(
    id=1, name="Plaid", company_type=enum("Employer"), website="https://plaid.com",
    industry="Fintech", notes="Great culture", applications=[], postings=[], people=[],
)

posting = SimpleNamespace(
    id=2, title="GTM Strategy & Ops", location="Remote", url="https://plaid.com/careers/2",
    jd_text="Do great things", comp_min=150000, comp_max=180000, company=company,
    my_rating=enum("Up"), rating_reason="Good fit",
)

resume = SimpleNamespace(
    id=3, label="Resume v3", content="Experienced operations leader...", source_link=None,
    filename="resume.pdf", notes="metrics-forward", applications=[],
)

app_obj = SimpleNamespace(
    id=4, title="Sr. Operations Manager", company_id=1, company=company, stage=enum("Applied"),
    lost_reason=None, resume_id=3, resume=resume, job_posting_id=2, job_posting=posting,
    applied_date=datetime(2026, 7, 1, 10, 0), notes="Referred by Jane", meetings=[],
    created_at=datetime(2026, 6, 28, 9, 0), updated_at=datetime(2026, 7, 12, 16, 30),
    last_activity_date=datetime(2026, 7, 10, 15, 0),
    email_threads=[], context="Team is 4 people; comp band unclear.", source=enum("Referral"),
    stage_history=[
        SimpleNamespace(id=10, from_stage=None, to_stage=enum("Saved"), changed_at=datetime(2026, 6, 28, 9, 0)),
        SimpleNamespace(id=11, from_stage=enum("Saved"), to_stage=enum("Applied"), changed_at=datetime(2026, 7, 1, 10, 0)),
    ],
)

meeting = SimpleNamespace(
    id=5, title="Interview with Plaid", meeting_type=enum("Hiring Manager"),
    meeting_date=datetime(2026, 7, 10, 15, 0), summary="Went well", transcript="Me: Hi\nThem: Hi",
    notes="", granola_note_id="abc123", granola_link="https://granola.ai/notes/abc123",
    application_id=4, application=app_obj,
    score=70, score_reason="Strong signal on scope", scored_at=datetime(2026, 7, 10, 18, 0),
)
app_obj.meetings = [meeting]  # circular-ish, but fine for a render smoke test

person = SimpleNamespace(
    id=6, name="Jane Doe", company_id=1, company=company, role=enum("Recruiter"),
    email="jane@plaid.com", phone="555-1234", linkedin="https://linkedin.com/in/janedoe",
    is_champion=1, notes="Very responsive", application_id=4, application=app_obj,
    email_threads=[],
)

thread = SimpleNamespace(
    id=7, subject="Re: Sr. Operations Manager role", body="Hi Jane,\n\nThanks for reaching out...",
    participants="jane@plaid.com, me@gmail.com", started_at=datetime(2026, 6, 20, 9, 0),
    last_message_at=datetime(2026, 6, 22, 14, 30), notes="Follow up next week",
    people=[person], application_id=4, application=app_obj,
    score=55, score_reason="Polite but slow to reply", scored_at=datetime(2026, 6, 22, 15, 0),
)
person.email_threads = [thread]
app_obj.email_threads = [thread]

activity = [
    {"type": "Email", "when": thread.last_message_at, "title": thread.subject,
     "sub": ", ".join(p.name for p in thread.people), "url": f"/email-threads/{thread.id}/edit",
     "score": thread.score},
    {"type": "Meeting", "when": meeting.meeting_date, "title": meeting.title,
     "sub": meeting.meeting_type.value, "url": f"/meetings/{meeting.id}/edit",
     "score": meeting.score},
]

# Mirrors what ui._score_rollup() returns: latest reading plus the change from
# the one before it. Kept as a literal so the template can be smoke-tested
# without importing the app package (which needs FastAPI/SQLAlchemy).
score_rollup = {"latest": 70, "previous": 55, "delta": 15, "count": 2}

cases = [
    ("company_edit.html", {"active": "companies", "company": company, "company_types": ["Employer", "Agency", "Both"]}),
    ("posting_edit.html", {"active": "postings", "posting": posting}),
    ("resume_edit.html", {"active": "resumes", "resume": resume}),
    ("application_edit.html", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": score_rollup,
    }),
    # Nothing scored yet: the rollup is None and the widget should stay hidden
    # rather than rendering an empty box -- the common state for a brand-new
    # application, so it's the one most worth smoke-testing.
    ("application_edit.html (unscored)", {
        "active": "board",
        "app_obj": SimpleNamespace(**{**app_obj.__dict__, "source": None, "context": None}),
        "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting],
        "activity": [{**row, "score": None} for row in activity],
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": None,
    }),
    # Every date null. This is the case the Dates block exists for: the fields
    # have to render as empty-but-present inputs with a "— not set" marker,
    # rather than disappearing. strftime on a None would blow up here, so this
    # also guards the `if f[2]` branches in the loop.
    ("application_edit.html (no dates set)", {
        "active": "board",
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__, "applied_date": None, "created_at": None,
            "updated_at": None, "last_activity_date": None, "stage_history": [],
        }),
        "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": score_rollup,
    }),
    # A downward move, to exercise the other branch of the trend pill.
    ("application_edit.html (cooling)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": {"latest": 30, "previous": 70, "delta": -40, "count": 3},
    }),
    # A single scored activity: there's no prior reading, so delta is None and
    # the trend pill has to be skipped without blowing up on the comparison.
    ("application_edit.html (first score)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": {"latest": 40, "previous": None, "delta": None, "count": 1},
    }),
    ("meeting_edit.html", {
        "active": "meetings", "meeting": meeting, "applications": [app_obj],
        "meeting_types": ["Hiring Manager", "Technical"], "granola_enabled": True,
    }),
    ("meeting_edit.html (granola disabled)", {
        "active": "meetings", "meeting": meeting, "applications": [app_obj],
        "meeting_types": ["Hiring Manager", "Technical"], "granola_enabled": False,
    }),
    # score=None is the default state for most meetings, and `0` is a legal
    # score that must not be confused with "unscored" -- both branches of the
    # `is not none` checks in the template need to render.
    ("meeting_edit.html (unscored)", {
        "active": "meetings",
        "meeting": SimpleNamespace(**{
            **meeting.__dict__, "score": None, "score_reason": None, "scored_at": None,
        }),
        "applications": [app_obj],
        "meeting_types": ["Hiring Manager", "Technical"], "granola_enabled": False,
    }),
    ("meeting_edit.html (zero score)", {
        "active": "meetings",
        "meeting": SimpleNamespace(**{**meeting.__dict__, "score": 0}),
        "applications": [app_obj],
        "meeting_types": ["Hiring Manager", "Technical"], "granola_enabled": False,
    }),
    ("companies.html", {"active": "companies", "companies": [company], "company_types": ["Employer"]}),
    ("postings.html", {"active": "postings", "postings": [posting], "companies": [company]}),
    ("resumes.html", {"active": "resumes", "resumes": [resume]}),
    ("meetings.html", {
        "active": "meetings", "meetings": [meeting], "applications": [app_obj],
        "meeting_types": ["Hiring Manager"], "granola_enabled": True,
    }),
    ("board.html", {
        "active": "board", "stages": ["Saved", "Applied"],
        "grouped": {"Saved": [], "Applied": [app_obj]}, "companies": [company],
        "resumes": [resume], "postings": [posting],
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
    }),
    ("people.html", {
        "active": "people", "people": [person], "companies": [company],
        "applications": [app_obj], "person_roles": ["Recruiter", "Hiring Manager"],
    }),
    ("people.html (no companies)", {
        "active": "people", "people": [], "companies": [], "applications": [],
        "person_roles": ["Recruiter"],
    }),
    ("person_edit.html", {
        "active": "people", "person": person, "companies": [company],
        "applications": [app_obj], "person_roles": ["Recruiter", "Hiring Manager"],
    }),
    ("email_threads.html", {
        "active": "emails", "threads": [thread], "people": [person], "applications": [app_obj],
        "preselect_person_id": None, "preselect_application_id": None,
    }),
    ("email_threads.html (preselected person)", {
        "active": "emails", "threads": [thread], "people": [person], "applications": [app_obj],
        "preselect_person_id": person.id, "preselect_application_id": None,
    }),
    ("email_threads.html (empty)", {
        "active": "emails", "threads": [], "people": [], "applications": [],
        "preselect_person_id": None, "preselect_application_id": None,
    }),
    ("email_thread_edit.html", {
        "active": "emails", "thread": thread, "people": [person], "applications": [app_obj],
        "selected_person_ids": {person.id},
    }),
    ("email_thread_edit.html (no one linked)", {
        "active": "emails",
        "thread": SimpleNamespace(**{**thread.__dict__, "people": []}),
        "people": [person], "applications": [app_obj], "selected_person_ids": set(),
    }),
    ("email_thread_edit.html (unscored)", {
        "active": "emails",
        "thread": SimpleNamespace(**{
            **thread.__dict__, "score": None, "score_reason": None, "scored_at": None,
        }),
        "people": [person], "applications": [app_obj], "selected_person_ids": {person.id},
    }),
]

failures = 0
for name, ctx in cases:
    template_name = name.split(" ")[0]
    try:
        html = env.get_template(template_name).render(**ctx)
        assert "{%" not in html and "{{" not in html, "unrendered Jinja syntax leaked into output"
        print(f"OK   {name}  ({len(html)} chars)")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")

sys.exit(1 if failures else 0)
