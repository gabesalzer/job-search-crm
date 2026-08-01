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

cases = [
    ("company_edit.html", {"active": "companies", "company": company, "company_types": ["Employer", "Agency", "Both"]}),
    ("posting_edit.html", {"active": "postings", "posting": posting}),
    ("resume_edit.html", {"active": "resumes", "resume": resume}),
    ("application_edit.html", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 3,
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
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
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting],
        "activity": [{**row, "score": None} for row in activity],
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": None, "forecast": forecast_blank,
        "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
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
        "activity_age": 0,
        "forecast": forecast, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        # Same migration story as manual_forecast above: brief, brief_model and
        # brief_generated_at are all NULL on every row predating the column,
        # which is every application currently in the Render database.
        "brief": brief_empty, "brief_error": "",
    }),
    # A pursuit that has gone quiet past the 14-day threshold, which is the
    # branch that renders the staleness warning.
    ("application_edit.html (gone quiet)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 41,
        "forecast": forecast_no_champion, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "brief": brief_stale, "brief_error": "",
    }),
    # A record whose forecast reads high off setup facts alone. The number
    # looks confident and the evidence pill has to say otherwise.
    ("application_edit.html (thin evidence)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": None,
        "forecast": forecast_thin, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
        "brief": brief_off, "brief_error": "API returned 401: invalid x-api-key",
    }),
    # Meetings and threads are on the record, none of them rated. The panel has
    # to say that rather than repeating the empty state's "nothing is linked".
    ("application_edit.html (activity, none of it rated)", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting], "activity": activity,
        "sources": ["Referral", "Recruiter Inbound", "Outbound"],
        "activity_age": 3,
        "forecast": forecast_unrated, "forecast_values": FORECAST_VALUES,
        "forecast_weights": FORECAST_WEIGHTS,
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
        "forecasts": {}, "activity_ages": {}, "default_stage": "Qualification",
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
        "forecasts": {app_obj.id: forecast_blank}, "activity_ages": {},
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
