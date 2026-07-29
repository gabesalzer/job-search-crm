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
    manual_forecast=enum("Best Case"),
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
    my_performance=75, employer_engagement=80,
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
score_rollup = {"latest": 70, "previous": 55, "delta": 15, "count": 2, "stale_days": 3}

# Mirrors what forecast.automated_forecast() returns. Unlike the rollup, this
# one *could* be produced by importing the real module (it's stdlib-only), but
# it's kept as a literal for the same reason the rollup is: this file's job is
# to prove the templates survive every shape they can be handed, including
# shapes the model would only emit under conditions that are awkward to
# construct. tests/test_forecast.py is where the values themselves are checked.
forecast = {
    "category": "Best Case", "total": 58, "confidence": "ok",
    "reason": "Best Case: meeting quality 78 across 2 scored meetings, referral origin, strong resume/JD overlap.",
    "components": {
        "stage": 12.0, "meetings": 25.8, "fit": 6.2, "source": 15.0,
        "quality": 78.0, "fit_index": 0.194, "fit_band": "Moderate",
        "scored_meetings": 2,
    },
}

# The empty state, which is what every brand-new application renders. Every
# optional component is None here, so this is the case that catches a template
# reaching for `.fit_band|lower` or formatting a null index.
forecast_blank = {
    "category": "Pipeline", "total": 4, "confidence": "none",
    "reason": "Nothing to read yet — no scored meetings, no fit, no source.",
    "components": {
        "stage": 4.0, "meetings": 0.0, "fit": 0.0, "source": 0.0,
        "quality": None, "fit_index": None, "fit_band": None, "scored_meetings": 0,
    },
}

forecast_commit = {
    "category": "Commit", "total": 100, "confidence": "high",
    "reason": "Closed Won — this one is decided.",
    "components": {
        "stage": 0.0, "meetings": 0.0, "fit": 0.0, "source": 0.0,
        "quality": None, "fit_index": None, "fit_band": None, "scored_meetings": 0,
    },
}

FORECAST_VALUES = ["Pipeline", "Best Case", "Commit", "Closed"]

# The Brief panel's four states. Worth covering all of them because three are
# failure-ish paths that a happy-path-only check would never touch, and one of
# them (no key) is what every visitor to the public repo actually sees.
brief_written = {
    "enabled": True,
    "text": (
        "## How this started\n"
        "A referral from a former colleague, who passed the resume directly to the hiring manager.\n"
        "\n"
        "## What's happened so far\n"
        "A recruiter screen established the scope, and a panel followed three weeks later.\n"
    ),
    "generated_at": datetime(2026, 3, 26, 8, 15),
    "model": "claude-sonnet-5",
    "changed_since": 0,
}
# Same brief, but activity has landed since it was written -- the panel has to
# say so rather than presenting stale prose as current.
brief_stale = {**brief_written, "changed_since": 2}
# Key present, nothing generated yet: the empty state with the button.
brief_empty = {
    "enabled": True, "text": None, "generated_at": None, "model": None, "changed_since": 0,
}
# No ANTHROPIC_API_KEY. The whole panel switches off and must not reference
# `generated_at`, which is None here.
brief_off = {
    "enabled": False, "text": None, "generated_at": None, "model": None, "changed_since": 0,
}

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
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "brief": brief_written, "brief_error": "",
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
        "score_rollup": None, "forecast": forecast_blank,
        "forecast_values": FORECAST_VALUES,
        "brief": brief_empty, "brief_error": "",
    }),
    # Every date null. This is the case the Dates block exists for: the fields
    # have to render as empty-but-present inputs with a "— not set" marker,
    # rather than disappearing. strftime on a None would blow up here, so this
    # also guards the `if f[2]` branches in the loop.
    ("application_edit.html (no dates set)", {
        "active": "board",
        # manual_forecast is None here too: an existing row predates the column,
        # and ensure_schema()'s ADD COLUMN leaves it NULL rather than applying
        # the Python-side Pipeline default. Every application already in the
        # Render database will render through this branch on first load.
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__, "applied_date": None, "created_at": None,
            "updated_at": None, "last_activity_date": None, "stage_history": [],
            "manual_forecast": None,
        }),
        "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": score_rollup,
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        # Same migration story as manual_forecast above: brief, brief_model and
        # brief_generated_at are all NULL on every row predating the column,
        # which is every application currently in the Render database.
        "brief": brief_empty, "brief_error": "",
    }),
    # A downward move, to exercise the other branch of the trend pill.
    ("application_edit.html (cooling)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": {"latest": 30, "previous": 70, "delta": -40, "count": 3,
                         "stale_days": 41},
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "brief": brief_stale, "brief_error": "",
    }),
    # A single scored activity: there's no prior reading, so delta is None and
    # the trend pill has to be skipped without blowing up on the comparison.
    ("application_edit.html (first score)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "score_rollup": {"latest": 40, "previous": None, "delta": None, "count": 1,
                         "stale_days": None},
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "brief": brief_off, "brief_error": "API returned 401: invalid x-api-key",
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
            "my_performance": None, "employer_engagement": None,
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
    # A meeting rated on one axis only and one rated zero. Both are legal and
    # both are easy to lose: `{% if m.my_performance %}` would hide the zero,
    # which is the reading that says "that went badly" -- the opposite of the
    # blank it would be mistaken for.
    ("meetings.html (half-rated and zero)", {
        "active": "meetings",
        "meetings": [
            SimpleNamespace(**{**meeting.__dict__, "my_performance": None,
                               "employer_engagement": 40}),
            SimpleNamespace(**{**meeting.__dict__, "my_performance": 0,
                               "employer_engagement": 0, "score": 0}),
        ],
        "applications": [app_obj],
        "meeting_types": ["Hiring Manager"], "granola_enabled": True,
    }),
    ("board.html", {
        "active": "board", "stages": ["Staging", "Qualification", "Discovery"],
        "grouped": {"Staging": [], "Qualification": [app_obj], "Discovery": []},
        "rollups": {app_obj.id: score_rollup},
        "forecasts": {app_obj.id: forecast},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
    }),
    # A card whose latest reading is old enough to be flagged, and one sitting
    # in Staging. These are the two states the card renders differently from
    # the ordinary case, so both are worth a smoke test.
    ("board.html (stale reading, staged card)", {
        "active": "board", "stages": ["Staging", "Qualification"],
        "grouped": {"Staging": [app_obj], "Qualification": [app_obj]},
        "rollups": {app_obj.id: {"latest": 45, "previous": 70, "delta": -25,
                                 "count": 4, "stale_days": 41}},
        "forecasts": {app_obj.id: forecast_blank},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
    }),
    # Nothing scored anywhere: every card has to skip the score row entirely
    # rather than render an empty one. `rollups.get()` on a missing id is the
    # branch under test.
    ("board.html (nothing scored)", {
        "active": "board", "stages": ["Staging", "Qualification"],
        "grouped": {"Staging": [app_obj], "Qualification": []},
        "rollups": {}, "forecasts": {}, "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
    }),
    # A card whose manual forecast is unset -- the state of every application
    # already in the database, since ADD COLUMN backfills NULL. The card must
    # skip the "≠" disagreement chip rather than comparing None to a string.
    ("board.html (no manual forecast)", {
        "active": "board", "stages": ["Qualification"],
        "grouped": {"Qualification": [
            SimpleNamespace(**{**app_obj.__dict__, "manual_forecast": None})
        ]},
        "rollups": {}, "forecasts": {app_obj.id: forecast_blank},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
    }),
    # An undated latest reading: stale_days is None, which must render as no
    # age at all rather than as a huge number.
    ("board.html (undated reading)", {
        "active": "board", "stages": ["Qualification"],
        "grouped": {"Qualification": [app_obj]},
        "rollups": {app_obj.id: {"latest": 60, "previous": None, "delta": None,
                                 "count": 1, "stale_days": None}},
        "forecasts": {app_obj.id: forecast_commit},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
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
