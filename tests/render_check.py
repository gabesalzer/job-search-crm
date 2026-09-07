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
    # Web-derived, so they arrive with a citation. NULL provenance everywhere is
    # what every row predating the columns carries.
    funding_stage=None, employee_band=None, enrichment_source=None,
    enrichment_note=None, enrichment_url=None, enriched_at=None,
    enrichment_model=None,
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
    lost_reason=None, lost_category=None,
    seniority=None, speciality=None, classification_source=None,
    classification_note=None, classified_at=None, classification_model=None,
    resume_id=3, resume=resume, job_posting_id=2, job_posting=posting,
    applied_date=datetime(2026, 7, 1, 10, 0), notes="Referred by Jane", meetings=[],
    created_at=datetime(2026, 6, 28, 9, 0), updated_at=datetime(2026, 7, 12, 16, 30),
    last_activity_date=datetime(2026, 7, 10, 15, 0),
    email_threads=[], context="Team is 4 people; comp band unclear.", source=enum("Referral"),
    next_steps=None, pain=None, process=None, risks=None,
    criterion_ratings=[],
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
    # The quality pair plus its provenance. This base fixture is a thread you
    # rated yourself: rating_source is None, which is what every row that
    # predates the automatic read also carries.
    my_performance=60, employer_engagement=45,
    rating_source=None, rating_note=None, rated_at=None, rating_model=None,
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

# The forecast fixtures are now produced by importing the real module rather
# than hand-written as literals. app/forecast.py is stdlib-only, so importing
# it here costs nothing, and the literals had already gone stale once: they
# still carried the four-component shape (no `email`, no `champion`, no
# `total_known`) after the model grew to six, which meant this file was
# smoke-testing the templates against a payload the app can no longer emit.
# A fixture that can drift from the thing it stands in for is worse than no
# fixture, because it goes on passing.
#
# The literals' one real advantage -- being able to construct shapes the model
# would only emit under awkward conditions -- is kept by calling the model with
# deliberately awkward inputs below rather than by transcribing its output.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import forecast as forecast_model  # noqa: E402

# The ordinary case: a mid-process pursuit with a couple of rated meetings.
forecast = forecast_model.automated_forecast(
    stage="Discovery", source="Referral",
    meetings=[{"when": datetime(2026, 3, 10), "my_performance": 70,
               "employer_engagement": 80},
              {"when": datetime(2026, 3, 20), "my_performance": 75,
               "employer_engagement": 80}],
    threads=[{"when": datetime(2026, 3, 22), "my_performance": 60,
              "employer_engagement": 65}],
    resume_text=" ".join(["revenue operations forecast pipeline salesforce"] * 12),
    jd_text=" ".join(["revenue operations forecast pipeline territory"] * 12),
    champion=True,
)

# The empty state, which is what every brand-new application renders. Every
# optional component is None here, so this is the case that catches a template
# reaching for `.fit_band|lower` or formatting a null index.
forecast_blank = forecast_model.automated_forecast(stage="Qualification", source=None)

# The closed short-circuit, which builds its components a different way and is
# the branch most likely to be missing a key a template reaches for.
forecast_commit = forecast_model.automated_forecast(stage="Closed Won", source=None)

# Setup facts only: a perfect `total_known` with nothing behind it, capped to
# Best Case by the confidence gate. This is the shape that reads confident and
# isn't, so the panel has to render the "thin evidence" pill on it.
forecast_thin = forecast_model.automated_forecast(
    stage="Negotiation", source="Referral", champion=True)

# A deliberate "no champion" -- the tri-state's middle case, where the template
# must not print the "not assessed" note.
forecast_no_champion = forecast_model.automated_forecast(
    stage="Discovery", source="Outbound", champion=False,
    meetings=[{"when": datetime(2026, 3, 10), "my_performance": 20,
               "employer_engagement": 15}],
)

# Activity exists and none of it is rated. Scores exactly like the empty state
# and must not read like it: `forecast_blank` renders "nothing is linked here"
# while this one has to render "these are here and you haven't judged them".
# The two branches are indistinguishable on `scored_meetings`/`scored_threads`,
# which is why the counts exist and why both shapes are rendered.
forecast_unrated = forecast_model.automated_forecast(
    stage="Discovery", source="Referral",
    meetings=[{"when": datetime(2026, 3, 10), "my_performance": None,
               "employer_engagement": None, "score": None}],
    threads=[{"when": datetime(2026, 3, 22), "my_performance": None,
              "employer_engagement": None, "score": None},
             {"when": datetime(2026, 3, 25), "my_performance": None,
              "employer_engagement": None, "score": None}],
)

FORECAST_WEIGHTS = {
    "stage": forecast_model.W_STAGE,
    "meetings": forecast_model.W_MEETINGS,
    "email": forecast_model.W_EMAIL,
    "fit": forecast_model.W_FIT,
    "source": forecast_model.W_SOURCE,
    "champion": forecast_model.W_CHAMPION,
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

from app import analytics as analytics_model  # noqa: E402

ANALYTICS_STAGES = ["Qualification", "Discovery", "Takehome",
                    "Executive Signoff", "Negotiation", "Closed Won"]


def _ah(to_stage, when):
    return {"from_stage": None, "to_stage": to_stage, "changed_at": when}


# Enough dated history that at least one interval clears the sample floor and
# at least one does not -- both branches of the tile render in a single case.
analytics_populated = analytics_model.overview([
    {"id": 1, "company": "Condor", "title": "RevOps Lead", "stage": "Discovery",
     "source": "Referral", "applied_date": datetime(2026, 5, 13),
     "lost_category": None, "stage_history": [
         _ah("Staging", datetime(2026, 5, 1)),
         _ah("Qualification", datetime(2026, 5, 11)),
         _ah("Discovery", datetime(2026, 5, 23))]},
    {"id": 2, "company": "Plaid", "title": "Ops Manager", "stage": "Closed Lost",
     "source": "Outbound", "applied_date": datetime(2026, 5, 3),
     "lost_category": "Compensation gap", "stage_history": [
         _ah("Staging", datetime(2026, 5, 1)),
         _ah("Qualification", datetime(2026, 5, 9)),
         _ah("Discovery", datetime(2026, 5, 17)),
         _ah("Closed Lost", datetime(2026, 6, 10))]},
    {"id": 3, "company": "Jellyfish", "title": "GTM Lead", "stage": "Negotiation",
     "source": "Referral", "applied_date": datetime(2026, 5, 1),
     "lost_category": None, "stage_history": [
         _ah("Staging", datetime(2026, 4, 25)),
         _ah("Qualification", datetime(2026, 5, 1)),
         _ah("Discovery", datetime(2026, 5, 10)),
         _ah("Negotiation", datetime(2026, 5, 31))]},
    # Closed lost with no category: the row the breakdown must show rather than
    # drop, or the categorised losses read as the whole story.
    {"id": 4, "company": "LanceDB", "title": "RevOps", "stage": "Closed Lost",
     "source": None, "applied_date": None, "lost_category": None,
     "stage_history": [_ah("Qualification", datetime(2026, 5, 5))]},
], ANALYTICS_STAGES)

analytics_empty = analytics_model.overview([], ANALYTICS_STAGES)

analytics_thin = analytics_model.overview([
    {"id": 1, "company": "Condor", "title": "RevOps Lead", "stage": "Discovery",
     "source": "Referral", "applied_date": datetime(2026, 5, 13),
     "lost_category": None, "stage_history": [
         _ah("Qualification", datetime(2026, 5, 11)),
         _ah("Discovery", datetime(2026, 5, 23))]},
    # In Staging, so on no funnel rung at all -- drives the "not on the funnel"
    # sentence that stops a short first bar reading as a bug.
    {"id": 2, "company": "Sierra", "title": "Ops", "stage": "Staging",
     "source": None, "applied_date": None, "lost_category": None,
     "stage_history": []},
], ANALYTICS_STAGES)

# Read from the real module so the template is smoke-tested against the exact
# vocabulary the app offers, not a transcription of it.
from app import classify as classify_model  # noqa: E402

FUNDING_STAGES_FIXTURE = classify_model.FUNDING_STAGES
EMPLOYEE_BANDS_FIXTURE = classify_model.EMPLOYEE_BANDS
SENIORITY_FIXTURE = classify_model.SENIORITY_VALUES
SPECIALITY_FIXTURE = classify_model.SPECIALITY_VALUES

from app import fit as fit_model  # noqa: E402
from app import fields as fields_model  # noqa: E402
from app import logspec as logspec_model  # noqa: E402

# The Settings page's own half. Built from the real catalogue rather than from
# literals, so a field added there is rendered by this check on the next run
# without anyone remembering to add a fixture -- which is the whole reason the
# catalogue is one list in one place.
SETTINGS_BASE = {
    "active": "settings",
    "definition_rows": fields_model.rows(writable=logspec_model.WRITABLE),
    "definition_groups": fields_model.groups(),
    "writers": fields_model.WRITERS,
    "looking_for": SimpleNamespace(statement=None, dq_threshold=4),
    "criteria": [], "threshold": 4, "ranked": [],
    "scale_min": fit_model.SCALE_MIN, "scale_max": fit_model.SCALE_MAX,
    "saved": "",
}

# Built from the real module, like the forecast fixtures, so the template is
# smoke-tested against the shape the app actually emits.
CRITERIA_FIXTURE = [
    SimpleNamespace(id=i + 1, name=name, description=blurb, sort_order=i)
    for i, (name, blurb) in enumerate(fit_model.STARTER_CRITERIA)
]
FIT_ROWS = [
    {"criterion": c, "name": c.name,
     "score": [9, 7, None, 8, 2, None][i], "note": None}
    for i, c in enumerate(CRITERIA_FIXTURE)
]
# Part-rated and disqualified at once -- the two states that have to read
# differently from each other and from "not rated at all".
FIT_READING = fit_model.score(FIT_ROWS, threshold=4)
FIT_UNRATED = fit_model.score(
    [{"criterion": c, "name": c.name, "score": None} for c in CRITERIA_FIXTURE],
    threshold=4)

APP_EDIT_BASE = {
    "active": "board", "stages": ["Saved", "Applied", "Closed Lost"],
    "lost_categories": ["Compensation gap", "Other"],
    "companies": [company], "resumes": [resume], "postings": [posting],
    "activity": activity, "sources": ["Referral", "Recruiter Inbound", "Outbound"],
    "activity_age": 3, "forecast": forecast, "forecast_values": FORECAST_VALUES,
    "forecast_weights": FORECAST_WEIGHTS,
    "read_result": "", "read_enabled": True, "unread_threads": 0,
    "declined_threads": 0, "brief": brief_empty, "brief_error": "",
    "classify_result": "",
    "fit_rows": FIT_ROWS, "fit": FIT_READING,
    "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
    "seniority_values": SENIORITY_FIXTURE,
    "speciality_values": SPECIALITY_FIXTURE,
    # No expected close date is the normal state and must render as silence,
    # not as overdue.
    "close_state": {"date": None, "days_over": None, "overdue": False,
                    "closed": False, "slips": 0},
    "close_history": [],
}

CLOSE_OVERDUE = {"date": datetime(2026, 8, 20), "days_over": 17,
                 "overdue": True, "closed": False, "slips": 2}
CLOSE_AHEAD = {"date": datetime(2026, 10, 31), "days_over": -55,
               "overdue": False, "closed": False, "slips": 0}
CLOSE_TODAY = {"date": datetime(2026, 9, 6), "days_over": 0, "overdue": False,
               "closed": False, "slips": 1}
CLOSE_DONE = {"date": datetime(2026, 6, 30), "days_over": 68, "overdue": False,
              "closed": True, "slips": 3}
CLOSE_SLIPS = [
    {"changed_at": datetime(2026, 8, 1), "from_date": datetime(2026, 7, 31),
     "to_date": datetime(2026, 9, 30), "moved": 61},
    {"changed_at": datetime(2026, 7, 2), "from_date": datetime(2026, 6, 30),
     "to_date": datetime(2026, 7, 31), "moved": 31},
    {"changed_at": datetime(2026, 6, 1), "from_date": None,
     "to_date": datetime(2026, 6, 30), "moved": None},
]

INSIGHTS_BASE = {
    "active": "insights", "stage_order": ANALYTICS_STAGES,
    "chips": [], "rejected": [], "filtered": False, "compare_by": None,
    "comparison": [], "query": {}, "query_string": "",
    "messages": [], "chat_enabled": True, "model_name": "claude-sonnet-5",
    "error": "",
}


# --------------------------------------------------------------------------- #
# Log fixtures
# --------------------------------------------------------------------------- #
NOW = datetime(2026, 9, 6, 14, 30)

LOG_ENTRY = SimpleNamespace(
    id=1,
    text=("Talked to Todd at Condor — the screen went well and they're setting "
          "up a panel for next week. Comp came up on Sierra and it's light "
          "against my number. I said I'd send the RevOps deck by Friday."),
    prose="Heard a stage move on Condor and a comp risk on Sierra.",
    status="pending", origin="web", created_at=NOW,
)

LOG_BASE = {
    "active": "log", "entry": None, "changes": [], "unmatched": [],
    "rejected": [], "recent": [], "pending": [], "unread": [], "failed": False,
    "enabled": True, "error": "", "applied": "",
}

cases = [
    ("company_edit.html", {"active": "companies", "company": company,
                          "company_types": ["Employer", "Agency", "Both"],
                          "funding_stages": FUNDING_STAGES_FIXTURE,
                          "employee_bands": EMPLOYEE_BANDS_FIXTURE,
                          "lookup_result": "", "lookup_enabled": True}),
    ("posting_edit.html", {"active": "postings", "posting": posting}),
    ("resume_edit.html", {"active": "resumes", "resume": resume}),
    ("application_edit.html", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 3,
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "read_result": "", "read_enabled": True, "classify_result": "",
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
        "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"], "unread_threads": 0, "declined_threads": 0,
        "brief": brief_written, "brief_error": "",
    }),
    # Nothing rated yet, and no activity at all, so the age is None and the
    # staleness warning has to stay hidden rather than comparing None to 14 --
    # the common state for a brand-new application, so it's the one most worth
    # smoke-testing.
    ("application_edit.html (unscored)", {
        "active": "board",
        "app_obj": SimpleNamespace(**{**app_obj.__dict__, "source": None, "context": None}),
        "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting],
        "activity": [{**row, "score": None} for row in activity],
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": None, "forecast": forecast_blank,
        "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "read_result": "", "read_enabled": True, "classify_result": "",
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
        "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"], "unread_threads": 0, "declined_threads": 0,
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
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 0,
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "read_result": "", "read_enabled": True, "classify_result": "",
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
        "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"], "unread_threads": 0, "declined_threads": 0,
        # Same migration story as manual_forecast above: brief, brief_model and
        # brief_generated_at are all NULL on every row predating the column,
        # which is every application currently in the Render database.
        "brief": brief_empty, "brief_error": "",
    }),
    # A pursuit that has gone quiet past the 14-day threshold, which is the
    # branch that renders the staleness warning.
    ("application_edit.html (gone quiet)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 41,
        "forecast": forecast_no_champion, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "read_result": "", "read_enabled": True, "classify_result": "",
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
        "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"], "unread_threads": 0, "declined_threads": 0,
        "brief": brief_stale, "brief_error": "",
    }),
    # A record whose forecast reads high off setup facts alone. The number
    # looks confident and the evidence pill has to say otherwise.
    ("application_edit.html (thin evidence)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": None,
        "forecast": forecast_thin, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "read_result": "", "read_enabled": True, "classify_result": "",
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
        "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"], "unread_threads": 0, "declined_threads": 0,
        "brief": brief_off, "brief_error": "API returned 401: invalid x-api-key",
    }),
    # Meetings and threads are on the record, none of them rated. The panel has
    # to say that rather than repeating the empty state's "nothing is linked".
    ("application_edit.html (activity, none of it rated)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 3,
        "forecast": forecast_unrated, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        # The state the button exists for: threads linked, none read. Also the
        # only case that renders the result banner, which reports what a press
        # did even on success -- a thread the model declined leaves Email at
        # zero, which looks exactly like the button not working.
        "read_result": "rated 2 threads; found no signal in 1 (left blank on purpose, "
                       "which keeps email out of the score rather than dragging it down)",
        "classify_result": "",
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"],
        "read_enabled": True, "unread_threads": 3, "declined_threads": 0,
        "brief": brief_off, "brief_error": "",
    }),
    # Read, and it honestly found nothing. Email still reads 0.0/10, which is
    # indistinguishable from "nobody looked" unless the panel says otherwise --
    # the exact ambiguity fixed one level down and reintroduced here by the
    # feature that made this state possible.
    ("application_edit.html (threads read, no signal found)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_categories": ["Compensation gap", "Ghosted, never told me", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 3,
        "forecast": forecast_unrated, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "read_result": "", "read_enabled": True, "classify_result": "",
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
        "fit_threshold": 4, "fit_scale_min": 1, "fit_scale_max": 10,
        "seniority_values": ["Director+", "Manager"],
        "speciality_values": ["Systems", "Strategy", "Systems + Strategy"],
        "unread_threads": 0, "declined_threads": 2,
        "brief": brief_off, "brief_error": "",
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
        "forecasts": {app_obj.id: forecast},
        "activity_ages": {app_obj.id: 3},
        "fits": {}, "closes": {},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
    }),
    # A card whose latest activity is old enough to be flagged, and one sitting
    # in Staging. These are the two states the card renders differently from
    # the ordinary case, so both are worth a smoke test.
    ("board.html (stale card, staged card)", {
        "active": "board", "stages": ["Staging", "Qualification"],
        "grouped": {"Staging": [app_obj], "Qualification": [app_obj]},
        "forecasts": {app_obj.id: forecast_blank},
        "activity_ages": {app_obj.id: 41},
        "fits": {}, "closes": {},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
    }),
    # No forecast keyed for this card at all: it has to skip the score row
    # entirely rather than render an empty one. `forecasts.get()` on a missing
    # id is the branch under test.
    ("board.html (nothing scored)", {
        "active": "board", "stages": ["Staging", "Qualification"],
        "grouped": {"Staging": [app_obj], "Qualification": []},
        "forecasts": {}, "activity_ages": {}, "fits": {}, "default_stage": "Qualification",
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
        "forecasts": {app_obj.id: forecast_blank}, "activity_ages": {}, "fits": {},
        "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
    }),
    # An application whose activity carries no usable date at all: the age is
    # None, which must render as no age rather than as a huge number.
    ("board.html (undated activity)", {
        "active": "board", "stages": ["Qualification"],
        "grouped": {"Qualification": [app_obj]},
        "activity_ages": {app_obj.id: None},
        "forecasts": {app_obj.id: forecast_commit},
        "fits": {}, "closes": {},
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
        "read_error": "", "read_enabled": True, "has_human_rating": True,
    }),
    ("email_thread_edit.html (no one linked)", {
        "active": "emails",
        "thread": SimpleNamespace(**{**thread.__dict__, "people": []}),
        "people": [person], "applications": [app_obj], "selected_person_ids": set(),
        "read_error": "", "read_enabled": True, "has_human_rating": True,
    }),
    ("email_thread_edit.html (unscored)", {
        "active": "emails",
        "thread": SimpleNamespace(**{
            **thread.__dict__, "score": None, "score_reason": None, "scored_at": None,
            "my_performance": None, "employer_engagement": None,
        }),
        "people": [person], "applications": [app_obj], "selected_person_ids": {person.id},
        "read_error": "", "read_enabled": True, "has_human_rating": False,
    }),
    # The four states of the automatic read. All four render a different
    # sentence in the same box, and three of them are paths a happy-path-only
    # check would never touch.
    ("email_thread_edit.html (read automatically)", {
        "active": "emails",
        "thread": SimpleNamespace(**{
            **thread.__dict__,
            "my_performance": 55, "employer_engagement": 78,
            "rating_source": "model",
            "rating_note": "recruiter answered the comp question unprompted and named a date",
            "rated_at": datetime(2026, 6, 23, 8, 15),
            "rating_model": "claude-sonnet-5",
        }),
        "people": [person], "applications": [app_obj], "selected_person_ids": {person.id},
        "read_error": "", "read_enabled": True, "has_human_rating": False,
    }),
    # The model declined both fields. Numbers blank, note present, still
    # unmistakably a completed read rather than a thread nobody has touched.
    ("email_thread_edit.html (read, declined to score)", {
        "active": "emails",
        "thread": SimpleNamespace(**{
            **thread.__dict__,
            "my_performance": None, "employer_engagement": None,
            "rating_source": "model",
            "rating_note": "three messages of calendar logistics, nothing evaluative",
            "rated_at": datetime(2026, 6, 23, 8, 15),
            "rating_model": "claude-sonnet-5",
        }),
        "people": [person], "applications": [app_obj], "selected_person_ids": {person.id},
        "read_error": "", "read_enabled": True, "has_human_rating": False,
    }),
    ("email_thread_edit.html (never read, with an error)", {
        "active": "emails",
        "thread": SimpleNamespace(**{
            **thread.__dict__,
            "my_performance": None, "employer_engagement": None,
            "rating_source": None, "rating_note": None,
            "rated_at": None, "rating_model": None,
        }),
        "people": [person], "applications": [app_obj], "selected_person_ids": {person.id},
        "read_error": "API returned 401: invalid x-api-key",
        "read_enabled": True, "has_human_rating": False,
    }),
    # No key at all — what anyone cloning the public repo sees.
    ("email_thread_edit.html (reading disabled)", {
        "active": "emails",
        "thread": SimpleNamespace(**{
            **thread.__dict__,
            "my_performance": None, "employer_engagement": None,
            "rating_source": None, "rating_note": None,
            "rated_at": None, "rating_model": None,
        }),
        "people": [person], "applications": [app_obj], "selected_person_ids": {person.id},
        "read_error": "", "read_enabled": False, "has_human_rating": False,
    }),
    # A card carrying both numbers, and one that fails your own floor. The DQ
    # state is the loudest thing a card can say, so it gets its own case.
    ("board.html (fit and forecast together)", {
        "active": "board", "stages": ["Discovery"],
        "grouped": {"Discovery": [app_obj]},
        "forecasts": {app_obj.id: forecast},
        "activity_ages": {app_obj.id: 3},
        "fits": {app_obj.id: fit_model.score(
            [{"name": "Comp", "score": 8}, {"name": "Scope", "score": 7}],
            threshold=4)},
        "default_stage": "Discovery", "companies": [company],
        "resumes": [resume], "postings": [posting], "sources": ["Referral"],
    }),
    ("board.html (disqualified card)", {
        "active": "board", "stages": ["Discovery"],
        "grouped": {"Discovery": [app_obj]},
        "forecasts": {app_obj.id: forecast},
        "activity_ages": {app_obj.id: 3},
        "fits": {app_obj.id: fit_model.score(
            [{"name": "Comp", "score": 9},
             {"name": "Lifestyle fit", "score": 2}], threshold=4)},
        "default_stage": "Discovery", "companies": [company],
        "resumes": [resume], "postings": [posting], "sources": ["Referral"],
    }),
    # --- Settings: field definitions, and the folded Looking For -----------
    ("settings.html", {
        **SETTINGS_BASE,
        "looking_for": SimpleNamespace(
            statement="Systems and strategy, Series B or later, remote.",
            dq_threshold=4),
        "criteria": CRITERIA_FIXTURE, "threshold": 4,
        "ranked": fit_model.rank([
            {"id": 4, "company": "Condor", "title": "VP RevOps",
             "stage": "Discovery", "ratings": FIT_ROWS},
            {"id": 5, "company": "Plaid", "title": "Ops Manager",
             "stage": "Qualification",
             "ratings": [{"name": c.name, "score": None} for c in CRITERIA_FIXTURE]},
        ], threshold=4),
    }),
    # No axes defined and nothing to rank -- what a fresh database renders
    # before the seed, and after deleting every axis.
    ("settings.html (no axes yet)", {
        **SETTINGS_BASE,
        "looking_for": SimpleNamespace(statement=None, dq_threshold=None),
        "criteria": [], "threshold": 4, "ranked": [],
    }),
    # A definition you have rewritten: the row has to show your wording, mark
    # it as yours, and still carry the shipped one so a reset has a target.
    ("settings.html (a definition edited)", {
        **SETTINGS_BASE,
        "definition_rows": fields_model.rows(
            overrides={"pain": "Only what the employer is trying to fix.",
                       "champion": "My own note, which the model never sees."},
            writable=logspec_model.WRITABLE),
    }),
    # Disqualified, and part-rated at the same time.
    ("application_edit.html (fit, disqualified)", {
        **APP_EDIT_BASE, "app_obj": app_obj,
        "fit_rows": FIT_ROWS, "fit": FIT_READING,
    }),
    # Nothing rated: must read as unrated rather than as a zero score.
    ("application_edit.html (fit, nothing rated)", {
        **APP_EDIT_BASE, "app_obj": app_obj,
        "fit_rows": [{"criterion": c, "name": c.name, "score": None, "note": None}
                     for c in CRITERIA_FIXTURE],
        "fit": FIT_UNRATED,
    }),
    # No axes exist, so the panel points at the tab instead of rendering a form.
    ("application_edit.html (no axes defined)", {
        **APP_EDIT_BASE, "app_obj": app_obj, "fit_rows": [],
        "fit": fit_model.score([], threshold=4),
    }),
    # --- Next steps on the card --------------------------------------------
    # A long one, because the card is 264px wide and the failure mode is a
    # sentence wrapping to three lines and pushing the forecast below the fold.
    ("board.html (next steps set)", {
        "active": "board", "stages": ["Qualification", "Discovery"],
        "grouped": {
            "Qualification": [SimpleNamespace(**{
                **app_obj.__dict__,
                "next_steps": "Send Todd the revised deck and ask for the "
                              "panel date before Friday"})],
            "Discovery": [SimpleNamespace(**{**app_obj.__dict__,
                                            "next_steps": "Prep the takehome"})],
        },
        "forecasts": {app_obj.id: forecast}, "activity_ages": {app_obj.id: 3},
        "fits": {}, "closes": {},
        "default_stage": "Qualification", "companies": [company],
        "resumes": [resume], "postings": [posting], "sources": ["Referral"],
    }),
    # Blank on every card, which is the state the board spends most of its life
    # in -- the line has to disappear rather than leave a gap or a bare arrow.
    ("board.html (no next steps)", {
        "active": "board", "stages": ["Qualification"],
        "grouped": {"Qualification": [SimpleNamespace(**{**app_obj.__dict__,
                                                        "next_steps": None})]},
        "forecasts": {}, "activity_ages": {}, "fits": {}, "default_stage": "Qualification",
        "companies": [company], "resumes": [resume], "postings": [posting],
        "sources": ["Referral"],
    }),
    ("application_edit.html (next steps set)", {
        **APP_EDIT_BASE,
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__,
            "next_steps": "Chase the recruiter for a panel date."}),
    }),
    # --- Classification and enrichment states ------------------------------
    # The panel's four branches. Three are states a happy-path check never
    # reaches, and two of them ("read it and declined" vs "nobody has looked")
    # render identical fields — the exact ambiguity fixed once for email
    # threads and re-created here by a second feature that can decline.
    ("application_edit.html (classified automatically)", {
        **APP_EDIT_BASE,
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__,
            "seniority": enum("Director+"), "speciality": enum("Systems + Strategy"),
            "classification_source": "model",
            "classification_note": "Owns the function and names both tooling and territory design.",
            "classified_at": datetime(2026, 8, 14, 9, 0),
            "classification_model": "claude-sonnet-5",
        }),
        "classify_result": "read the posting as Director+ / Systems + Strategy",
    }),
    # Read, and it declined both — most often an IC role, which is neither
    # value. Both fields blank, but for a recorded reason.
    ("application_edit.html (classified, declined both)", {
        **APP_EDIT_BASE,
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__,
            "seniority": None, "speciality": None,
            "classification_source": "model",
            "classification_note": "Reads as an individual-contributor analyst role.",
            "classified_at": datetime(2026, 8, 14, 9, 0),
            "classification_model": "claude-sonnet-5",
        }),
    }),
    # Values you typed. Must say so, because the automatic pass will not
    # overwrite them and the page has to explain why nothing changes.
    ("application_edit.html (classified by hand)", {
        **APP_EDIT_BASE,
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__,
            "seniority": enum("Manager"), "speciality": enum("Strategy"),
            "classification_source": None, "classification_note": None,
            "classified_at": None, "classification_model": None,
        }),
    }),
    # No posting linked, so there is no description to read.
    ("application_edit.html (nothing to classify)", {
        **APP_EDIT_BASE,
        "app_obj": SimpleNamespace(**{
            **app_obj.__dict__, "job_posting": None, "job_posting_id": None,
        }),
    }),
    ("company_edit.html (looked up)", {
        "active": "companies", "company_types": ["Employer"],
        "funding_stages": FUNDING_STAGES_FIXTURE,
        "employee_bands": EMPLOYEE_BANDS_FIXTURE,
        "lookup_result": "read Series B and 51-200 employees from https://plaid.com/about",
        "lookup_enabled": True,
        "company": SimpleNamespace(**{
            **company.__dict__,
            "funding_stage": enum("Series B"), "employee_band": enum("51-200"),
            "enrichment_source": "model",
            "enrichment_note": "About page says 'since our Series B' and 'a team of 60'.",
            "enrichment_url": "https://plaid.com/about",
            "enriched_at": datetime(2026, 8, 14, 9, 0),
            "enrichment_model": "claude-sonnet-5",
        }),
    }),
    # Fetched the page and it said nothing. Both fields blank *on purpose* —
    # which looks exactly like the button not working unless the panel says so.
    ("company_edit.html (looked, site says nothing)", {
        "active": "companies", "company_types": ["Employer"],
        "funding_stages": FUNDING_STAGES_FIXTURE,
        "employee_bands": EMPLOYEE_BANDS_FIXTURE,
        "lookup_result": "read https://plaid.com and found nothing it states "
                         "about funding or headcount (left blank rather than guessed)",
        "lookup_enabled": True,
        "company": SimpleNamespace(**{
            **company.__dict__,
            "funding_stage": None, "employee_band": None,
            "enrichment_source": "model",
            "enrichment_note": "Marketing homepage with no company facts on it.",
            "enrichment_url": "https://plaid.com",
            "enriched_at": datetime(2026, 8, 14, 9, 0),
            "enrichment_model": "claude-sonnet-5",
        }),
    }),
    # No website recorded: the button has to disable rather than fail on press.
    ("company_edit.html (no website)", {
        "active": "companies", "company_types": ["Employer"],
        "funding_stages": FUNDING_STAGES_FIXTURE,
        "employee_bands": EMPLOYEE_BANDS_FIXTURE,
        "lookup_result": "", "lookup_enabled": True,
        "company": SimpleNamespace(**{**company.__dict__, "website": None}),
    }),
    # No key at all — what anyone cloning the public repo sees.
    ("company_edit.html (lookups disabled)", {
        "active": "companies", "company_types": ["Employer"],
        "funding_stages": FUNDING_STAGES_FIXTURE,
        "employee_bands": EMPLOYEE_BANDS_FIXTURE,
        "lookup_result": "", "lookup_enabled": False, "company": company,
    }),
    # --- Insights (analytics + chat, merged) -------------------------------
    # Fixtures come from the real modules rather than hand-written literals,
    # for the same reason the forecast ones do: a transcribed payload drifts
    # from what the app emits and goes on passing while it does.
    ("insights.html", {**INSIGHTS_BASE, **analytics_populated,
                       "unfiltered_total": analytics_populated["total"],
                       "messages": [
                           SimpleNamespace(id=1, role="user", model=None, usage=None,
                                           view_spec=None, created_at=datetime(2026, 8, 7, 9, 0),
                                           content="Compare referrals against outbound."),
                           SimpleNamespace(id=2, role="assistant", model="claude-sonnet-5",
                                           usage='{"cache_read_input_tokens": 88000}',
                                           view_spec='{"compare_by": "source"}',
                                           created_at=datetime(2026, 8, 7, 9, 0, 12),
                                           content=("Referrals reach Discovery more often.\n\n"
                                                    "The sample is small either way.")),
                       ]}),
    # Nothing recorded at all -- what a fresh clone opens on, and the state
    # where every figure would divide by zero if it were computed.
    ("insights.html (empty)", {**INSIGHTS_BASE, **analytics_empty,
                               "unfiltered_total": 0}),
    # Records exist but almost none can be timed. This is the state Gabe's own
    # pipeline is in, so it is the one that has to read well.
    ("insights.html (below the sample floor)", {**INSIGHTS_BASE, **analytics_thin,
                                                "unfiltered_total": analytics_thin["total"]}),
    # A filter is on: chips draw, the count says how much of the pipeline is
    # hidden, and every chip carries the link that removes it.
    ("insights.html (filtered)", {
        **INSIGHTS_BASE, **analytics_thin, "unfiltered_total": 9, "filtered": True,
        "chips": [{"param": "source", "label": "source is Referral",
                   "href": "/insights"},
                  {"param": "since", "label": "applied on or after 2026-06-01",
                   "href": "/insights?source=Referral"}],
        "query": {"source": "Referral", "since": "2026-06-01"},
        "query_string": "source=Referral&since=2026-06-01",
    }),
    # The filter matched nothing. Must not read as an empty database, and the
    # chat has to survive so you can ask your way back out.
    ("insights.html (filtered to nothing)", {
        **INSIGHTS_BASE, **analytics_empty, "unfiltered_total": 9, "filtered": True,
        "chips": [{"param": "company", "label": "company is Northwind",
                   "href": "/insights"}],
    }),
    # Part of a proposed view was refused -- the path a hallucinated value takes.
    ("insights.html (view partly rejected)", {
        **INSIGHTS_BASE, **analytics_thin,
        "unfiltered_total": analytics_thin["total"],
        "rejected": ["source: no record has 'Carrier Pigeon' — that filter was dropped.",
                     "'vibes' isn't a field on an application."],
    }),
    # A comparison. Renders as a table because N funnels of two would each draw
    # a confident bar chart over a sample too small to average.
    ("insights.html (compared by source)", {
        **INSIGHTS_BASE, **analytics_populated,
        "unfiltered_total": analytics_populated["total"],
        "compare_by": "source",
        "comparison": [
            {"name": "Referral", "total": 3,
             "intervals": analytics_populated["intervals"],
             "funnel": analytics_populated["funnel"]},
            {"name": "Not recorded", "total": 1,
             "intervals": analytics_empty["intervals"],
             "funnel": analytics_empty["funnel"]},
        ],
    }),
    # No API key: the composer switches off and the charts are unaffected.
    ("insights.html (asking disabled)", {**INSIGHTS_BASE, **analytics_populated,
                                         "unfiltered_total": 4,
                                         "chat_enabled": False}),
    # A failed call leaves a user turn with no answer under it, and an
    # assistant row predating the `model` column renders without provenance.
    ("insights.html (error, unanswered question)", {
        **INSIGHTS_BASE, **analytics_thin,
        "unfiltered_total": analytics_thin["total"],
        "error": "The request timed out after 180s.",
        "messages": [SimpleNamespace(id=1, role="user", content="Draft a note to Todd.",
                                     model=None, usage=None, view_spec=None,
                                     created_at=None),
                     SimpleNamespace(id=2, role="assistant", content="Older answer.",
                                     model=None, usage=None, view_spec=None,
                                     created_at=None)],
    }),
    # --- Log: the review screen ---------------------------------------------
    # The load-bearing case. A replaced field must show what is being lost
    # alongside what replaces it, an appended one must not (nothing is lost),
    # and an empty current value must read as empty rather than as a gap.
    ("log.html (changes to review)", {
        **LOG_BASE,
        "entry": LOG_ENTRY,
        "changes": [
            {"key": "3:stage", "application_id": 3, "company": "Condor",
             "title": "Head of RevOps", "field": "stage", "label": "stage",
             "mode": "set", "current": "Qualification", "value": "Discovery",
             "why": "said a panel is being scheduled for next week"},
            {"key": "3:next_steps", "application_id": 3, "company": "Condor",
             "title": "Head of RevOps", "field": "next_steps",
             "label": "next steps", "mode": "set", "current": "Wait for Todd",
             "value": "Send Todd the RevOps deck before Friday",
             "why": "committed to sending the deck"},
            {"key": "7:risks", "application_id": 7, "company": "Sierra",
             "title": "RevOps Manager", "field": "risks", "label": "risks",
             "mode": "set", "current": "",
             "value": "Comp band is light against my number",
             "why": "comp came up and it was light"},
            {"key": "7:notes", "application_id": 7, "company": "Sierra",
             "title": "RevOps Manager", "field": "notes", "label": "notes",
             "mode": "append", "current": "Applied via referral.",
             "value": "Went quiet after the screen.", "why": "no reply"},
        ],
        "unmatched": ["mentioned a Vercel recruiter — no application on file"],
        "rejected": ["'champion' isn't a field a note can change, so it was "
                     "dropped."],
    }),
    # Nothing proposed and nothing to review: the empty page you land on.
    ("log.html (nothing logged yet)", LOG_BASE),
    # A note read as chatter. There is an entry but no changes, which must read
    # as "nothing to record" rather than as a broken review screen.
    ("log.html (note with no changes)", {
        **LOG_BASE,
        "entry": SimpleNamespace(**{**LOG_ENTRY.__dict__,
                                    "prose": "Nothing here to record."}),
        "recent": [LOG_ENTRY],
    }),
    # The API path: something queued while you were away.
    ("log.html (pending from the API)", {
        **LOG_BASE,
        "pending": [SimpleNamespace(
            id=9, text="Voice memo from the walk home about Condor.",
            status="pending", origin="api", created_at=NOW, prose=None)],
        "recent": [SimpleNamespace(
            id=9, text="Voice memo from the walk home about Condor.",
            status="pending", origin="api", created_at=NOW, prose=None)],
    }),
    # A note that could not be read at all. Must offer a retry and a delete
    # rather than leaving you on a screen with nothing to act on.
    ("log.html (note that failed to read)", {
        **LOG_BASE,
        "entry": SimpleNamespace(**{**LOG_ENTRY.__dict__, "status": "failed",
                                    "prose": None}),
        "failed": True,
        "rejected": ["API returned 400: This API key is not scoped to a "
                     "workspace."],
        "unread": [SimpleNamespace(
            id=12, text="An older note from while the API was down.",
            status="failed", origin="web", created_at=NOW, prose=None)],
    }),
    # No key set: notes are still kept, but nothing is read from them.
    ("log.html (reading disabled)", {**LOG_BASE, "enabled": False}),
    # Both banners at once, which is what an apply-then-error looks like.
    ("log.html (applied and errored)", {
        **LOG_BASE, "applied": "Condor (stage, next steps)",
        "error": "API returned 500: overloaded_error",
    }),
    # --- Expected close date -------------------------------------------------
    # Past due and already slipped twice: the loudest state, and the reason the
    # field is on the card at all.
    ("application_edit.html (close date overdue)", {
        **APP_EDIT_BASE, "app_obj": app_obj,
        "close_state": CLOSE_OVERDUE, "close_history": CLOSE_SLIPS,
    }),
    # Still ahead, never moved: must read as information, not as a warning.
    ("application_edit.html (close date ahead)", {
        **APP_EDIT_BASE, "app_obj": app_obj, "close_state": CLOSE_AHEAD,
        "close_history": [CLOSE_SLIPS[-1]],
    }),
    # Landing today. The plural-handling case: "0 days out" would be wrong.
    ("application_edit.html (close date today)", {
        **APP_EDIT_BASE, "app_obj": app_obj, "close_state": CLOSE_TODAY,
        "close_history": [CLOSE_SLIPS[-1]],
    }),
    # Closed after running past its date: shown, never nagged about.
    ("application_edit.html (close date, since closed)", {
        **APP_EDIT_BASE, "app_obj": app_obj, "close_state": CLOSE_DONE,
        "close_history": CLOSE_SLIPS,
    }),
    # A date that was set and then cleared -- to_date is NULL, which the slip
    # table has to render as words rather than as a blank cell.
    ("application_edit.html (close date cleared)", {
        **APP_EDIT_BASE, "app_obj": app_obj,
        "close_state": {"date": None, "days_over": None, "overdue": False,
                        "closed": False, "slips": 1},
        "close_history": [
            {"changed_at": datetime(2026, 8, 9), "from_date": datetime(2026, 9, 30),
             "to_date": None, "moved": None},
            CLOSE_SLIPS[-1],
        ],
    }),
    # The card, overdue: the marker rides in the score row beside the age.
    ("board.html (close date overdue)", {
        "active": "board", "stages": ["Qualification", "Discovery"],
        "grouped": {"Qualification": [app_obj], "Discovery": []},
        "forecasts": {app_obj.id: forecast}, "activity_ages": {app_obj.id: 3},
        "fits": {}, "closes": {app_obj.id: CLOSE_OVERDUE},
        "default_stage": "Qualification", "companies": [company],
        "resumes": [resume], "postings": [posting], "sources": ["Referral"],
    }),
    # The card, still ahead: a quiet tag on the meta row, no colour.
    ("board.html (close date ahead)", {
        "active": "board", "stages": ["Qualification", "Discovery"],
        "grouped": {"Qualification": [app_obj], "Discovery": []},
        "forecasts": {app_obj.id: forecast}, "activity_ages": {app_obj.id: 3},
        "fits": {}, "closes": {app_obj.id: CLOSE_AHEAD},
        "default_stage": "Qualification", "companies": [company],
        "resumes": [resume], "postings": [posting], "sources": ["Referral"],
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
