"""Server-rendered UI (Jinja2).

A thin presentation layer over the exact same models and database the JSON API
uses. Form posts here just create/update rows and redirect back to the page;
the drag-to-change-stage on the board calls the JSON API directly.
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from .. import analytics as analytics_model
from .. import brief as brief_model
from .. import chat as chat_model
from .. import classify
from .. import fields as fields_model
from .. import fit
from .. import forecast as forecast_model
from .. import logspec
from .. import models
from .. import thread_read
from .. import viewspec
from ..database import get_db
from ..services import granola, llm, scrape
from ..services.email_parse import parse_gmail_export
from ..services.resume_extract import extract_text

# templates/ lives next to app/, resolved relative to this file so it works
# regardless of the current working directory.
TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)

STAGE_VALUES = [s.value for s in models.Stage]          # ordered; Closed Lost last
# The funnel rungs only -- Staging and Closed Lost are deliberately absent
# (see the note above models.STAGE_ORDER).
STAGE_ORDER_VALUES = [s.value for s in models.STAGE_ORDER]
COMPANY_TYPES = [t.value for t in models.CompanyType]
LOST_CATEGORY_VALUES = [c.value for c in models.LostCategory]
PERSON_ROLE_VALUES = [r.value for r in models.PersonRole]
APPLICATION_SOURCE_VALUES = [s.value for s in models.ApplicationSource]
FORECAST_VALUES = [f.value for f in models.ForecastCategory]
SENIORITY_VALUES = [v.value for v in models.Seniority]
SPECIALITY_VALUES = [v.value for v in models.Speciality]
FUNDING_STAGE_VALUES = [v.value for v in models.FundingStage]
EMPLOYEE_BAND_VALUES = [v.value for v in models.EmployeeBand]

# The forecast's component budgets, read straight off the model so the
# breakdown panel can print "12.0/25" without the denominators being retyped
# into a template. They were hardcoded there once and went stale the moment the
# weights were rebalanced, which turned an explanation of the number into a
# contradiction of it.
FORECAST_WEIGHTS = {
    "stage": forecast_model.W_STAGE,
    "meetings": forecast_model.W_MEETINGS,
    "email": forecast_model.W_EMAIL,
    "fit": forecast_model.W_FIT,
    "source": forecast_model.W_SOURCE,
    "champion": forecast_model.W_CHAMPION,
}


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def _parse_score(value: Optional[str]) -> Optional[int]:
    """Parse a 0-100 win-likelihood score off a form field.

    Blank means "not scored" and is a first-class answer, not an error -- most
    meetings and threads never get a number, and forcing a default would put
    fabricated readings into what is meant to become calibration data.

    Out-of-range input is **clamped**, not rejected. This is the whole
    validation story for `score`: there is no CHECK constraint backing it,
    because `ensure_schema()` only ever issues ADD COLUMN and could never add
    one to the existing tables. Guarding at the only door that writes the
    column keeps the invariant real without a migration path we don't have.
    Garbage that isn't a number at all (a stray letter) is treated as "not
    scored" rather than a 500 -- a typo in an optional field shouldn't lose
    the rest of the form.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        score = int(float(text))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


def _apply_score(obj, raw_score: Optional[str], reason: str) -> None:
    """Write a score + reason onto a Meeting or Email Thread, stamping
    `scored_at` when the number itself moves.

    Both objects carry the identical (score, score_reason, scored_at) trio, so
    they share one writer -- if the stamping rule ever changes it changes in
    one place and the two activity types can't drift apart.

    `scored_at` is re-stamped only when the *number* changes. Fixing a typo in
    the reason text isn't a new judgment, and re-dating it would corrupt the
    ordering the trend rollup reads. Clearing the score clears the timestamp
    with it, so an unscored activity never carries a stale "scored on" date.
    """
    new_score = _parse_score(raw_score)
    if new_score is None:
        obj.score = None
        obj.score_reason = None
        obj.scored_at = None
        return
    if new_score != obj.score or obj.scored_at is None:
        obj.scored_at = datetime.now(timezone.utc)
    obj.score = new_score
    obj.score_reason = (reason or "").strip() or None


def _apply_activity_quality(obj, my_performance: str, employer_engagement: str) -> None:
    """Write the two halves of an activity's quality: how well you did, and how
    interested they were.

    Takes a Meeting or an EmailThread. They carry the same pair of columns with
    the same meaning, and the forecast reads one shape over both, so the writer
    is shared too -- a second near-identical function is exactly how the two
    would drift apart.

    Reuses _parse_score because these share its scale and its rules exactly --
    0-100, clamped rather than rejected, blank meaning "no judgment formed"
    rather than zero. That last distinction is the whole reason these aren't
    defaulted: a call where you didn't rate your own performance and a call you
    rated 0 are opposite claims, and the forecast reads them as such.

    Unlike `score` these carry no `*_at` stamp. `scored_at` exists because
    score calibration later needs to know when a judgment was formed; these two
    are attributes of the activity, read at the activity's own date. A
    timestamp nobody reads is a column that can only rot.
    """
    obj.my_performance = _parse_score(my_performance)
    obj.employer_engagement = _parse_score(employer_engagement)


def _has_human_rating(thread) -> bool:
    """Has a person put a number in this thread's quality pair.

    `rating_source` is NULL for a human value and "model" for a read one, so
    "there is a value and nobody has claimed it for the model" is the test. It
    also answers correctly for every row that predates the column: before the
    automatic read existed, every value in these fields was typed by hand, so
    NULL-means-human is a fact about history rather than an assumption.

    One case it deliberately cannot distinguish: clearing both halves of a
    model-written pair leaves a thread that looks exactly like one nobody ever
    rated, so a later body edit will read it again. Storing "the human said
    blank" would need a fourth state on top of a tri-state, and the cheaper
    answer is that re-reads only fire on a body change or a button press, so a
    thread you cleared stays cleared unless you change the messages in it.
    """
    if thread.rating_source == "model":
        return False
    return thread.my_performance is not None or thread.employer_engagement is not None


def _hand_edit_claims_the_rating(thread, before_perf, before_eng) -> bool:
    """Transfer ownership of the pair to you the moment you change it.

    Returns whether ownership moved, which the caller needs in order to skip
    the automatic read on the same request. Without that, clearing both numbers
    while also editing the body put the model's numbers straight back: the
    clear correctly released ownership, `_has_human_rating` then correctly saw
    an unrated thread, and the body change fired a read into it. Three correct
    steps composing into exactly the behaviour the feature promises never
    happens. A save in which you touched these fields is a save where your
    judgment wins, whatever else changed alongside it.

    The edit form pre-fills these inputs with whatever is stored, including a
    model-written pair, and submits them back unchanged on an ordinary save.
    Without this check every save of an unrelated field -- fixing a subject
    line, relinking a person -- would silently relabel a machine's reading as
    your own judgment, which is precisely the confusion `rating_source` was
    added to prevent.

    So: identical values leave ownership alone, and a changed value takes it.
    The model's note goes with the numbers, because a justification for a 78
    is not a justification for the 40 you replaced it with.
    """
    if (thread.my_performance == before_perf
            and thread.employer_engagement == before_eng):
        return False
    thread.rating_source = None
    thread.rating_note = None
    thread.rated_at = None
    thread.rating_model = None
    return True


def _read_thread_now(thread) -> Optional[str]:
    """Ask the model to rate one thread, and write the result. Returns an error.

    Returns None on success (including the success case where the model
    declined to score and only a note was stored), or a human-readable string
    when the call or the parse failed. Nothing is written on failure.

    This is the only place in the app that sends data to a third party without
    a button press behind it -- it fires when you save a thread. Three guards
    keep that narrow: it does nothing without a key, nothing without a body,
    and nothing at all if you have already formed your own view. A human
    rating is never overwritten, by this or by anything else.
    """
    if not llm.enabled():
        return "Automatic reading is off — no ANTHROPIC_API_KEY is set."
    if not (thread.body or "").strip():
        return "There are no messages in this thread to read."
    if _has_human_rating(thread):
        return "You have already rated this thread; a read would not overwrite it."

    payload = thread_read.build_read_payload(
        subject=thread.subject,
        body=thread.body,
        participants=thread.participants,
        started_at=thread.started_at,
        last_message_at=thread.last_message_at,
        company=(thread.application.company.name
                 if thread.application and thread.application.company else None),
        role_title=thread.application.title if thread.application else None,
        stage=(thread.application.stage.value
               if thread.application and thread.application.stage else None),
        context=thread.application.context if thread.application else None,
    )
    try:
        text, model_used = llm.generate(
            thread_read.SYSTEM_PROMPT,
            thread_read.build_messages(payload),
            # Three lines back, so the token ceiling is a runaway guard rather
            # than a shape constraint. The timeout is short because you are
            # sitting on a save, not watching a Brief render: a form that
            # appears to hang for three minutes reads as a crashed app, and
            # losing the reading is much cheaper than that.
            max_tokens=300,
            timeout=60,
        )
    except llm.LLMError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- see below
        # Deliberately broad. `llm.generate` promises LLMError but cannot fully
        # deliver: a 200 response whose body is not JSON raises ValueError out
        # of `resp.json()`, and anything escaping here would propagate out of
        # the POST handler *before* its `db.commit()`, rolling back the user's
        # entire edit. Losing a reading is a small failure; losing the subject
        # line they just fixed because a proxy returned an HTML error page is
        # not, and it is not a failure they could connect to a cause.
        return "The read failed unexpectedly: {}".format(exc)

    perf, eng, note, understood = thread_read.parse_read(text)
    if not understood:
        # Nothing recognisable came back, or only half of it did. Write nothing
        # rather than a guess -- a wrong number here propagates into the
        # forecast, the board colour and every comparison across applications,
        # and it does not look wrong. A half-parsed reply is the worse case:
        # `_rating` promotes a lone surviving number to the whole reading.
        return "The model's reply could not be read as a rating."

    thread.my_performance = perf
    thread.employer_engagement = eng
    thread.rating_note = note
    thread.rating_source = "model"
    # Aware, matching every other `_utcnow`-stamped column. `_naive_utc()`
    # flattens it before anything compares it against a form-entered date.
    thread.rated_at = datetime.now(timezone.utc)
    thread.rating_model = model_used
    return None


def _has_human_classification(app_obj) -> bool:
    """True when either classification was typed rather than derived."""
    return (app_obj.classification_source is None
            and (app_obj.seniority is not None or app_obj.speciality is not None))


def _hand_edit_claims_the_classification(app_obj, before_sen, before_spec) -> bool:
    """Take ownership of the classification when the submitted values differ.

    Exact mirror of `_hand_edit_claims_the_rating`, and it exists for the same
    bug: without it, clearing a value while also changing the linked posting
    would put the model's answer straight back on the next save. Returning
    whether it claimed lets the caller skip the automatic classification on
    that same request.
    """
    if (app_obj.seniority, app_obj.speciality) == (before_sen, before_spec):
        return False
    app_obj.classification_source = None
    app_obj.classification_note = None
    app_obj.classification_model = None
    app_obj.classified_at = None
    return True


def _classify_application_now(app_obj) -> Optional[str]:
    """Classify one application from its job description. Returns an error.

    None on success, a readable string otherwise, and nothing is written on
    failure. Same contract and the same broad `except` as `_read_thread_now`:
    this can fire during a save, and an exception escaping here would roll back
    the whole edit rather than just losing a classification.
    """
    if not llm.enabled():
        return "Automatic classification is off — no ANTHROPIC_API_KEY is set."
    if _has_human_classification(app_obj):
        return ("You have already classified this one; an automatic read "
                "would not overwrite it.")

    posting = app_obj.job_posting
    jd = (posting.jd_text if posting else None) or ""
    # A title alone is thin but not nothing -- "VP, Revenue Operations" is a
    # legitimate Director+ read. What is genuinely unclassifiable is a record
    # with neither, and that refuses rather than guessing.
    if not jd.strip() and not (app_obj.title or "").strip():
        return ("There is no job description or title here to classify. Link a "
                "posting, or type a title.")

    packet = classify.build_posting_packet(
        title=app_obj.title or (posting.title if posting else None),
        company=app_obj.company.name if app_obj.company else None,
        location=posting.location if posting else None,
        jd_text=jd,
    )
    try:
        text, model_used = llm.generate(
            classify.posting_system_prompt(),
            classify.build_posting_messages(packet),
            max_tokens=classify.MAX_TOKENS,
            timeout=60,
        )
    except llm.LLMError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- see _read_thread_now
        return "The classification failed unexpectedly: {}".format(exc)

    seniority, speciality, note, understood = classify.parse_posting_reply(text)
    if not understood:
        return "The model's reply could not be read as a classification."

    app_obj.seniority = models.Seniority(seniority) if seniority else None
    app_obj.speciality = models.Speciality(speciality) if speciality else None
    app_obj.classification_note = note
    app_obj.classification_source = "model"
    app_obj.classified_at = datetime.now(timezone.utc)
    app_obj.classification_model = model_used
    return None


def _has_human_enrichment(company) -> bool:
    return (company.enrichment_source is None
            and (company.funding_stage is not None
                 or company.employee_band is not None))


def _enrich_company_now(company) -> Optional[str]:
    """Read funding stage and headcount off the company's own website.

    Never fires on its own. It costs a Firecrawl fetch and an API call per
    press, and — more to the point — it is the only feature in this app whose
    output cannot be checked against anything already on the record. A step
    that needs that much judgment gets a button.

    Only text actually fetched from the site can produce a value here. The
    prompt forbids answering from what the model remembers, because a recalled
    funding round is frequently stale, cannot be cited, and would be stored
    beside a URL it did not come from.
    """
    if not llm.enabled():
        return "Lookups are off — no ANTHROPIC_API_KEY is set."
    if not (company.website or "").strip():
        return ("No website is recorded for this company, so there is nothing "
                "to read. Add one above and save first.")
    if _has_human_enrichment(company):
        return ("You have already filled these in; a lookup would not "
                "overwrite your values.")

    try:
        page = scrape.scrape_page_text(company.website)
    except Exception as exc:  # noqa: BLE001 -- any fetch failure, incl. httpx
        return "Couldn't fetch the site: {}".format(exc)

    packet = classify.build_company_packet(
        name=company.name, url=page["url"], page_text=page["text"])
    try:
        text, model_used = llm.generate(
            classify.company_system_prompt(),
            classify.build_company_messages(packet),
            max_tokens=classify.MAX_TOKENS,
            timeout=60,
        )
    except llm.LLMError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return "The lookup failed unexpectedly: {}".format(exc)

    stage, band, note, understood = classify.parse_company_reply(text)
    if not understood:
        return "The model's reply could not be read as a lookup result."

    company.funding_stage = models.FundingStage(stage) if stage else None
    company.employee_band = models.EmployeeBand(band) if band else None
    company.enrichment_note = note
    company.enrichment_source = "model"
    # The page it actually read, which may be /about rather than the homepage.
    # A value is only as checkable as the page behind it.
    company.enrichment_url = page["url"]
    company.enriched_at = datetime.now(timezone.utc)
    company.enrichment_model = model_used
    return None


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Flatten a datetime to naive UTC so mixed rows can be compared.

    Form-entered dates come back naive; columns stamped by `_utcnow` come back
    aware. Comparing the two raises TypeError, and the mix is unavoidable
    because both kinds live in the same tables.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _activity_age(app_obj: models.JobApplication) -> Optional[int]:
    """Days since the most recent thing that actually happened on this pursuit.

    What survives of the old score rollup. That function derived a second
    number -- "where does this application stand" -- from the hand-entered
    `score` on each activity, and it sat next to the automated forecast with no
    stated rule for which one won. Two numbers answering the same question is a
    tax on every glance, and the disagreement between them only taught you
    something if you already knew which to trust. The score itself did not
    disappear: it moved *inside* the forecast as the fallback reading for an
    activity whose performance and engagement fields are blank. See
    `forecast._rating`.

    The age is the one thing the rollup carried that the forecast genuinely
    cannot. A number with no age on it lies by omission -- an 80 from six weeks
    ago and an 80 from yesterday are the same digits describing completely
    different situations -- and that matters most on the board, where a column
    of them gets scanned at once and the confident-looking old one is exactly
    the card that misleads.

    Measured from the date the activity *happened* (`meeting_date` for a
    meeting, `last_message_at` then `started_at` for a thread), falling back to
    `scored_at` only when the activity carries no date of its own. An earlier
    version of this measured from `scored_at` first, which meant it reported
    how long ago you did data entry rather than how long the pursuit had been
    quiet -- a fact about your evening, not about the job.

    Counts every activity, not just rated ones. "Nothing has happened in 21
    days" is true whether or not you got around to rating what happened.

    Returns None when there is no activity, or none of it carries a usable
    date -- which is a different claim from "today".
    """
    dates = []
    for m in app_obj.meetings:
        dates.append(_naive_utc(m.meeting_date or m.scored_at))
    for t in app_obj.email_threads:
        dates.append(_naive_utc(t.last_message_at or t.started_at or t.scored_at))
    usable = [d for d in dates if d is not None]
    if not usable:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max((now - max(usable)).days, 0)


CLOSED_STAGES = (models.Stage.CLOSED_WON, models.Stage.CLOSED_LOST)


def _close_state(app_obj: models.JobApplication) -> dict:
    """How the expected close date is doing: set, due soon, or past due.

    Kept here rather than in a stdlib-only module, which is a departure worth
    justifying. The other reasoning modules exist because their logic is worth
    exercising with literals and because they encode a what-leaves-the-box
    decision; this is four lines of date arithmetic over one column, and
    inventing a module for it would buy a hand-written mirror in a test file --
    exactly the pattern that left `test_applied_analytics.py` proving the
    behaviour of code that no longer existed. Now that TestClient runs in the
    sandbox, this is covered by driving the real routes instead.

    `overdue` is only ever True on an *open* application. A closed pursuit that
    ran past its expected date is not a thing to chase; it is a thing that
    happened, and colouring it red on a Closed Lost card would be nagging about
    the past. The date still renders, because "we said June and it closed in
    September" is worth seeing.

    `days_over` is negative for a date still ahead, so one number carries both
    directions and the template does not need two.

    Returns `date: None` for an application with no expected date, which is the
    normal state and must read as "no view formed" rather than as overdue.
    """
    due = _naive_utc(app_obj.expected_close_date)
    if due is None:
        return {"date": None, "days_over": None, "overdue": False,
                "closed": app_obj.stage in CLOSED_STAGES, "slips": 0}
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    days_over = (today.date() - due.date()).days
    closed = app_obj.stage in CLOSED_STAGES
    return {
        "date": due,
        "days_over": days_over,
        "overdue": days_over > 0 and not closed,
        "closed": closed,
        # How many times this date has already moved. The count rather than the
        # rows, because the board wants "slipped twice" and only the edit page
        # wants the dates themselves.
        "slips": max(len(app_obj.close_date_history) - 1, 0),
    }


def _close_history(app_obj: models.JobApplication) -> List[dict]:
    """The slip log, newest first, with each move's size in days.

    The size is computed rather than stored: two dates and subtraction cannot
    disagree with each other, and a stored delta could.
    """
    rows = []
    for row in sorted(app_obj.close_date_history,
                      key=lambda r: _naive_utc(r.changed_at) or datetime.min,
                      reverse=True):
        moved = None
        frm, to = _naive_utc(row.from_date), _naive_utc(row.to_date)
        if frm is not None and to is not None:
            moved = (to.date() - frm.date()).days
        rows.append({
            "changed_at": row.changed_at,
            "from_date": row.from_date,
            "to_date": row.to_date,
            "moved": moved,
        })
    return rows


def _forecast_for(app_obj: models.JobApplication) -> dict:
    """Gather the six Forecast inputs off an Application.

    This is the only function in the app that knows both the ORM and the
    forecast model, and it deliberately does nothing but read fields. All the
    judgment lives in app/forecast.py, which imports no SQLAlchemy and is
    therefore directly exercised by tests rather than mirrored in them.

    Like the score rollup it replaced, this runs at display time and stores
    nothing. A forecast column would be a snapshot that quietly rots the moment
    you rescore a meeting or swap the resume, and the failure mode of a stale
    forecast is the worst kind: it looks exactly like a fresh one.

    Every date goes through `_naive_utc`. The forecast picks the latest activity
    with `max()` over the `when` values, and meeting dates arrive naive (typed
    into a form) while thread timestamps can arrive aware (stamped by
    `_utcnow`). Comparing the two raises TypeError, and because the raise would
    happen inside a template render it would take the whole application page
    down rather than degrade. Flattening here is cheap and total.

    `score` is passed alongside the decomposed pair rather than instead of it.
    The forecast prefers performance/engagement and falls back to the
    hand-entered score only when both are blank -- which is every activity in
    the database as it stands, so dropping it here would have made the model
    blind to the entire existing history on the day it shipped.
    """
    def _activity(obj, when):
        return {
            "when": _naive_utc(when),
            "my_performance": obj.my_performance,
            "employer_engagement": obj.employer_engagement,
            "score": obj.score,
        }

    return forecast_model.automated_forecast(
        stage=app_obj.stage.value if app_obj.stage else None,
        source=app_obj.source.value if app_obj.source else None,
        meetings=[
            _activity(m, m.meeting_date or m.scored_at)
            for m in app_obj.meetings
        ],
        threads=[
            _activity(t, t.last_message_at or t.started_at or t.scored_at)
            for t in app_obj.email_threads
        ],
        resume_text=app_obj.resume.content if app_obj.resume else None,
        jd_text=app_obj.job_posting.jd_text if app_obj.job_posting else None,
        champion=app_obj.champion,
    )


def _brief_payload_for(app_obj: models.JobApplication) -> str:
    """Flatten an Application into the plain-text packet sent to the API.

    Mirrors `_forecast_for`: the ORM walking happens here, and `brief.py` stays
    pure data-in/text-out. Unlike the forecast, this one reads nearly the whole
    record -- transcripts, email bodies, the JD -- so it is worth being able to
    see in one place exactly what leaves the box.
    """
    posting = None
    if app_obj.job_posting:
        posting = {
            "title": app_obj.job_posting.title,
            "url": app_obj.job_posting.url,
            "location": app_obj.job_posting.location,
            "posted_date": app_obj.job_posting.posted_date,
            "first_seen_at": app_obj.job_posting.first_seen_at,
            "jd_text": app_obj.job_posting.jd_text,
        }
    return brief_model.build_brief_payload(
        company=app_obj.company.name if app_obj.company else None,
        title=app_obj.title,
        stage=app_obj.stage.value if app_obj.stage else None,
        source=app_obj.source.value if app_obj.source else None,
        applied_date=app_obj.applied_date,
        context=app_obj.context,
        notes=app_obj.notes,
        resume_label=app_obj.resume.label if app_obj.resume else None,
        posting=posting,
        people=[
            {
                "name": p.name,
                "role": p.role.value if p.role else None,
                "email": p.email,
                "is_champion": p.is_champion,
            }
            for p in app_obj.people
        ],
        stage_history=[
            {
                "changed_at": h.changed_at,
                "from_stage": h.from_stage.value if h.from_stage else None,
                "to_stage": h.to_stage.value if h.to_stage else None,
            }
            for h in app_obj.stage_history
        ],
        meetings=[
            {
                "meeting_date": m.meeting_date,
                "title": m.title,
                "meeting_type": m.meeting_type.value if m.meeting_type else None,
                "summary": m.summary,
                "transcript": m.transcript,
                "notes": m.notes,
                "score": m.score,
                "score_reason": m.score_reason,
                "my_performance": m.my_performance,
                "employer_engagement": m.employer_engagement,
            }
            for m in app_obj.meetings
        ],
        email_threads=[
            {
                "subject": t.subject,
                "body": t.body,
                "participants": t.participants,
                "started_at": t.started_at,
                "last_message_at": t.last_message_at,
                "notes": t.notes,
                "score": t.score,
                "score_reason": t.score_reason,
                "my_performance": t.my_performance,
                "employer_engagement": t.employer_engagement,
                # Passed so the packet can label whose reading it is. See
                # brief._rating_label.
                "rating_source": t.rating_source,
            }
            for t in app_obj.email_threads
        ],
    )


def _brief_state(app_obj: models.JobApplication) -> dict:
    """What the Brief panel needs to render, including whether it's out of date.

    `stale` is the honest part. A stored brief is a photograph, and the whole
    risk of storing one is that last month's photograph looks exactly like this
    morning's. Anything logged against the application after the brief was
    written makes it a description of a pursuit that has since moved, and the
    panel says so rather than letting old prose read as current.

    Comparison is against each row's `updated_at`, not its event date, because
    the question here is "has the record changed since I summarized it" -- and
    backdating a meeting you logged today is still new information.
    """
    generated_at = app_obj.brief_generated_at
    changed_since = 0
    if generated_at is not None:
        cutoff = _naive_utc(generated_at)
        for row in list(app_obj.meetings) + list(app_obj.email_threads):
            touched = _naive_utc(row.updated_at or row.created_at)
            if touched is not None and touched > cutoff:
                changed_since += 1
    return {
        "enabled": llm.enabled(),
        "text": app_obj.brief,
        "generated_at": generated_at,
        "model": app_obj.brief_model,
        "changed_since": changed_since,
    }


@router.get("/")
def root():
    return RedirectResponse(url="/board")


# --------------------------------------------------------------------------- #
# Pipeline (kanban board)
# --------------------------------------------------------------------------- #
@router.get("/board")
def board(request: Request, db: Session = Depends(get_db)):
    # The forecast reads every meeting and thread on each card plus the resume
    # and the posting. Load all four up front: without this the board issues
    # four extra queries per application just to render the numbers, and that
    # cost grows with the pipeline.
    #
    # Pulling full resume and JD text for a board view looks expensive. It
    # mostly isn't -- selectinload issues one query per relationship for the
    # whole page, not per card, and resumes are shared across applications so
    # the distinct set is small. The fit index is recomputed per card on every
    # load rather than cached, which is the trade the whole forecast makes: a
    # derived number that can never be stale beats one that's cheap to read and
    # quietly wrong.
    apps = (
        db.query(models.JobApplication)
        .options(
            selectinload(models.JobApplication.meetings),
            selectinload(models.JobApplication.email_threads),
            selectinload(models.JobApplication.resume),
            selectinload(models.JobApplication.job_posting),
            selectinload(models.JobApplication.criterion_ratings),
            selectinload(models.JobApplication.close_date_history),
        )
        .all()
    )
    board_criteria = _criteria(db)
    board_threshold = _looking_for(db).dq_threshold

    grouped: dict[str, list] = {s: [] for s in STAGE_VALUES}
    for app_obj in apps:
        grouped.setdefault(app_obj.stage.value, []).append(app_obj)
    return templates.TemplateResponse(request, "board.html", {
        "active": "board",
        "stages": STAGE_VALUES,
        "grouped": grouped,
        # The one number, keyed by id so a card can look up its own without the
        # template calling into Python. The card shows the score, the category
        # and the age; the six-part breakdown behind it lives on the edit page,
        # because a board is for scanning and a breakdown on every card would
        # bury the one thing you came here to see.
        "forecasts": {a.id: _forecast_for(a) for a in apps},
        # Rides alongside the score because the forecast has no sense of age.
        # Same keying.
        "activity_ages": {a.id: _activity_age(a) for a in apps},
        # Fit, beside the forecast, because they are the two halves of one
        # decision: the forecast is whether they want you, fit is whether you
        # want them. Either alone tells you to spend time on the wrong record —
        # a high forecast on something you would turn down is the classic way
        # to lose a month. Criteria are read once for the whole page rather
        # than per card.
        "fits": {a.id: _fit_for(a, board_criteria, board_threshold)
                 for a in apps},
        # When you expect to know. On the card because the overdue state is the
        # only reason to put a date somewhere you scan rather than somewhere
        # you read -- a date you have to open a record to check is a date you
        # find out about too late.
        "closes": {a.id: _close_state(a) for a in apps},
        # The board's stage picker defaults to the same stage the column does.
        # Leaving it on whatever happens to be first in the list would quietly
        # make Staging the default for every new record.
        "default_stage": models.DEFAULT_STAGE.value,
        "sources": APPLICATION_SOURCE_VALUES,
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "resumes": db.query(models.Resume).order_by(models.Resume.label).all(),
        "postings": db.query(models.JobPosting)
        .order_by(models.JobPosting.last_seen_at.desc())
        .all(),
    })


@router.post("/ui/applications")
def create_application_ui(
    company_id: int = Form(...),
    title: str = Form(""),
    # "Saved" was a stage in the pre-July-2026 enum and stopped being a valid
    # value at the migration -- a post without a stage field would have raised
    # ValueError on models.Stage(stage). The form always sends one, so this
    # never fired, but the fallback should be a stage that actually exists.
    stage: str = Form(models.DEFAULT_STAGE.value),
    resume_id: Optional[str] = Form(None),
    job_posting_id: Optional[str] = Form(None),
    source: str = Form(""),
    db: Session = Depends(get_db),
):
    app_obj = models.JobApplication(
        company_id=company_id,
        title=title or None,
        stage=models.Stage(stage),
        resume_id=int(resume_id) if resume_id else None,
        job_posting_id=int(job_posting_id) if job_posting_id else None,
        source=models.ApplicationSource(source) if source else None,
    )
    db.add(app_obj)
    db.commit()
    return RedirectResponse(url="/board", status_code=303)


def _activity_timeline(app_obj: models.JobApplication) -> list[dict]:
    """Merge Meetings and Email Threads into one chronologically-sorted list
    for the Application page. Purely a display-layer merge (no new table,
    no schema change) -- each side keeps its own shape, we just normalize
    both into a common {type, when, title, sub, url, score} dict and sort by the
    timestamp that best represents "most recent activity" for that row:
    meeting_date for a Meeting, last_message_at for an Email Thread (so a
    thread with a fresh reply surfaces near the top, not buried at the date
    it started).
    """
    rows: list[dict] = []
    for m in app_obj.meetings:
        rows.append({
            "type": "Meeting",
            "when": m.meeting_date,
            "title": m.title or "Untitled meeting",
            "sub": m.meeting_type.value if m.meeting_type else None,
            "url": f"/meetings/{m.id}/edit",
            "score": m.score,
        })
    for t in app_obj.email_threads:
        rows.append({
            "type": "Email",
            "when": t.last_message_at or t.started_at,
            "title": t.subject or "Untitled thread",
            "sub": ", ".join(p.name for p in t.people) or None,
            "url": f"/email-threads/{t.id}/edit",
            "score": t.score,
        })
    rows.sort(key=lambda r: r["when"] or datetime.min, reverse=True)
    return rows


@router.get("/applications/{application_id}/edit")
def edit_application_page(
    application_id: int,
    request: Request,
    # Set by the brief refresh route when generation failed, so the reason
    # survives the redirect. There's no flash-message machinery in this app and
    # one query param is cheaper than adding some.
    brief_error: str = "",
    # Same mechanism for the batch thread read below. It reports what it did
    # even on success, because "nothing visibly changed" is a legitimate and
    # confusing outcome -- a thread the model declined to score leaves the
    # Email component at zero, which looks identical to the button not working.
    read_result: str = "",
    # Same mechanism again for the classification button.
    classify_result: str = "",
    db: Session = Depends(get_db),
):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    return templates.TemplateResponse(request, "application_edit.html", {
        "read_result": read_result,
        "read_enabled": llm.enabled(),
        "classify_result": classify_result,
        "fit_rows": _fit_rows(app_obj, _criteria(db)),
        "fit": _fit_for(app_obj, _criteria(db),
                        _looking_for(db).dq_threshold),
        "fit_threshold": fit.threshold_of(_looking_for(db).dq_threshold),
        "fit_scale_min": fit.SCALE_MIN,
        "fit_scale_max": fit.SCALE_MAX,
        "seniority_values": SENIORITY_VALUES,
        "speciality_values": SPECIALITY_VALUES,
        # How many linked threads a read would actually touch. Drives whether
        # the button renders at all, so it can never appear on an application
        # where pressing it would do nothing.
        "unread_threads": len(_threads_awaiting_a_read(app_obj)),
        # Threads a read has already answered by declining to score. Without
        # this the panel cannot tell "nobody has looked" from "something looked
        # and found nothing", because `email_quality` is None in both -- which
        # is the same collapse-two-causes-into-one-sentence bug that was fixed
        # one level down for meetings and threads, reintroduced one level up by
        # the feature that made the second case possible.
        "declined_threads": sum(
            1 for t in app_obj.email_threads
            if t.rating_source == "model"
            and t.my_performance is None and t.employer_engagement is None
        ),
        "active": "board",
        "app_obj": app_obj,
        "stages": STAGE_VALUES,
        "lost_categories": LOST_CATEGORY_VALUES,
        "sources": APPLICATION_SOURCE_VALUES,
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "resumes": db.query(models.Resume).order_by(models.Resume.label).all(),
        "postings": db.query(models.JobPosting)
        .order_by(models.JobPosting.last_seen_at.desc())
        .all(),
        "activity": _activity_timeline(app_obj),
        "activity_age": _activity_age(app_obj),
        # When you expect to know, and every time that answer has moved.
        "close_state": _close_state(app_obj),
        "close_history": _close_history(app_obj),
        # The two forecasts, side by side and deliberately independent. The
        # automated one is derived here and stored nowhere; the manual one is a
        # column only you write. Where they disagree is the interesting part, so
        # neither is allowed to overwrite or defer to the other.
        "forecast": _forecast_for(app_obj),
        "forecast_values": FORECAST_VALUES,
        # Component budgets, passed rather than written into the template. The
        # breakdown prints "12.0/30" next to each component, and hardcoding the
        # denominators there means a weights change in forecast.py silently
        # turns the panel into a lie -- which had already happened once.
        "forecast_weights": FORECAST_WEIGHTS,
        "brief": _brief_state(app_obj),
        "brief_error": brief_error,
    })


MAX_THREADS_PER_BATCH = 12


def _threads_awaiting_a_read(app_obj) -> list:
    """The threads on an application that a batch read would actually touch.

    Three exclusions, and the third is the one worth explaining.

    A thread with no body has nothing to read. A thread you rated yourself is
    never touched by anything, here or elsewhere. And a thread that has
    *already been read* is skipped too -- including one the model deliberately
    declined to score, because a decline is an answer and re-asking the same
    question of the same text is just buying the same answer again.

    That last exclusion is what makes the button idempotent: press it twice and
    the second press honestly reports that there is nothing left to do, rather
    than spending money to rewrite values it just wrote. Forcing a fresh read
    of one thread is still possible -- *Read it again* on the thread itself --
    which is the right place for it, since wanting a second opinion is a
    judgment about one conversation rather than about the application.
    """
    return [
        t for t in app_obj.email_threads
        if (t.body or "").strip()
        and not _has_human_rating(t)
        and t.rating_source != "model"
    ]


@router.post("/ui/applications/{application_id}/read-threads")
def read_application_threads_ui(application_id: int, db: Session = Depends(get_db)):
    """Read every unrated email thread on this application, in one press.

    This is deliberately *not* a "regenerate forecast" button, though that is
    what it looks like from the panel it sits in. There is nothing to
    regenerate: the forecast is arithmetic over stored values, recomputed on
    every page load and saved nowhere, so a button that recomputed it would be
    a reload with extra steps. What is actually missing when the Email
    component reads zero is not a computation, it is evidence -- so the button
    goes and gets the evidence.

    Per-thread guards are unchanged, because they live in `_read_thread_now`
    rather than here: a thread you rated yourself is skipped, and so is one
    with no body. That is the point of the batch being a loop over the single
    case rather than its own implementation.

    Bounded at `MAX_THREADS_PER_BATCH` because each thread is a separate call
    that blocks the response. Twelve is already a long wait; an application
    with more than that reports how many are left rather than silently doing
    part of the job, since a batch that quietly stopped early would look
    exactly like one that found nothing to do.
    """
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    candidates = _threads_awaiting_a_read(app_obj)
    remaining = max(len(candidates) - MAX_THREADS_PER_BATCH, 0)

    read = declined = failed = 0
    first_error = ""
    for thread in candidates[:MAX_THREADS_PER_BATCH]:
        error = _read_thread_now(thread)
        if error:
            failed += 1
            first_error = first_error or error
        elif thread.my_performance is None and thread.employer_engagement is None:
            declined += 1
        else:
            read += 1
    db.commit()

    bits = []
    if read:
        bits.append("rated {} thread{}".format(read, "" if read == 1 else "s"))
    if declined:
        bits.append(
            "found no signal in {} (left blank on purpose, which keeps email "
            "out of the score rather than dragging it down)".format(declined)
        )
    if failed:
        bits.append("{} failed — {}".format(failed, first_error))
    if remaining:
        bits.append("{} more still to read; press again".format(remaining))
    if not bits:
        bits.append("nothing to read — every thread here is either rated by you already or has no messages")

    return RedirectResponse(
        url="/applications/{}/edit?read_result={}".format(
            application_id, quote("; ".join(bits))),
        status_code=303,
    )


@router.post("/ui/applications/{application_id}/brief/refresh")
def refresh_brief_ui(application_id: int, db: Session = Depends(get_db)):
    """Generate the brief and store it. The only paid call in the app.

    Wired to an explicit button rather than to page load or to a save hook. A
    brief that regenerated whenever the record changed would spend money on
    your behalf, quietly, at a rate set by how much you happened to be editing
    that evening -- and most edits (fixing a date, correcting a title) don't
    change the story enough to be worth re-buying the prose.

    Failures are caught and redirected back with the reason rather than raising:
    a 500 on the edit page would hide the entire application behind a stack
    trace because one optional panel couldn't reach an API.
    """
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    destination = "/applications/{}/edit".format(application_id)
    try:
        payload = _brief_payload_for(app_obj)
        text, model_used = llm.generate(
            brief_model.SYSTEM_PROMPT, brief_model.build_messages(payload)
        )
    except llm.LLMError as exc:
        return RedirectResponse(
            url="{}?brief_error={}".format(destination, quote(str(exc))),
            status_code=303,
        )
    app_obj.brief = text
    app_obj.brief_model = model_used
    app_obj.brief_generated_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(url=destination, status_code=303)


@router.post("/ui/applications/{application_id}/edit")
def update_application_ui(
    application_id: int,
    company_id: int = Form(...),
    title: str = Form(""),
    stage: str = Form(...),
    resume_id: Optional[str] = Form(None),
    job_posting_id: Optional[str] = Form(None),
    lost_reason: str = Form(""),
    lost_category: str = Form(""),
    applied_date: str = Form(""),
    expected_close_date: str = Form(""),
    created_at: str = Form(""),
    last_activity_date: str = Form(""),
    updated_at: str = Form(""),
    notes: str = Form(""),
    context: str = Form(""),
    next_steps: str = Form(""),
    pain: str = Form(""),
    process: str = Form(""),
    risks: str = Form(""),
    source: str = Form(""),
    manual_forecast: str = Form(""),
    champion: str = Form(""),
    seniority: str = Form(""),
    speciality: str = Form(""),
    db: Session = Depends(get_db),
):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    # Captured before anything is assigned, so the automatic classification
    # below can tell a posting change from an ordinary save.
    posting_before = app_obj.job_posting_id
    class_before = (app_obj.seniority, app_obj.speciality)

    app_obj.company_id = company_id
    app_obj.title = title or None
    app_obj.resume_id = int(resume_id) if resume_id else None
    app_obj.job_posting_id = int(job_posting_id) if job_posting_id else None
    app_obj.seniority = models.Seniority(seniority) if seniority else None
    app_obj.speciality = models.Speciality(speciality) if speciality else None
    app_obj.applied_date = _parse_dt(applied_date)
    # Assigning this fires `_record_close_date_change`, which appends the slip
    # row. Nothing here has to remember to log it -- see the listener.
    app_obj.expected_close_date = _parse_dt(expected_close_date)
    app_obj.notes = notes or None
    app_obj.context = context or None
    app_obj.next_steps = next_steps or None
    app_obj.pain = pain or None
    app_obj.process = process or None
    app_obj.risks = risks or None
    # Blank stays NULL rather than defaulting to a source -- "we never recorded
    # how this one started" and "this one was outbound" are different facts,
    # and collapsing them would quietly bias any later source-conversion read.
    app_obj.source = models.ApplicationSource(source) if source else None
    # Your call, and only yours -- nothing else in the app writes this column.
    # The Automated Forecast sits next to it on the page and is computed fresh
    # on every render; it never reaches over and "corrects" this one, because a
    # machine that silently agrees with itself has told you nothing.
    #
    # Blank clears to NULL rather than snapping back to the Pipeline default.
    # New rows are born Pipeline (the column default), which is honest because
    # the category literally means "no signal"; but if you deliberately empty
    # the field, writing Pipeline back would be the app overruling you.
    app_obj.manual_forecast = (
        models.ForecastCategory(manual_forecast) if manual_forecast else None
    )
    # Tri-state, arriving as a string because an HTML form has no way to send
    # a real None. Empty string is "not assessed" and stays NULL; "yes" and
    # "no" are both real answers. This cannot use a bare truthiness test --
    # `bool("no")` is True, and the whole point of the column is that a
    # deliberate no is different from a blank.
    app_obj.champion = {"yes": True, "no": False}.get(champion)

    new_stage = models.Stage(stage)
    if new_stage != app_obj.stage:
        app_obj.stage = new_stage  # triggers the StageHistory event listener
        app_obj.last_activity_date = datetime.now(timezone.utc)
    # Both Closed Lost fields are cleared when the record is not Closed Lost,
    # so a pursuit dragged back out of the column cannot carry a stale cause
    # into the loss breakdown, which filters on current stage.
    if new_stage == models.Stage.CLOSED_LOST:
        app_obj.lost_reason = lost_reason.strip() or None
        app_obj.lost_category = (
            models.LostCategory(lost_category) if lost_category else None)
    else:
        app_obj.lost_reason = None
        app_obj.lost_category = None

    # --- Hand-correctable timestamps -------------------------------------- #
    # Every date on the record is editable, because the date a thing was
    # *recorded* here is routinely later than the date it happened -- you log
    # Monday's rejection on Thursday. A tracker whose dates you can't correct
    # measures your data-entry habits instead of your job search.
    #
    # created_at and last_activity_date are plain assignments. Note that
    # last_activity_date is applied *after* the stage block above, so an
    # explicit edit wins over the automatic "now" stamp a stage change sets.
    new_created = _parse_dt(created_at)
    if new_created:
        app_obj.created_at = new_created
    new_activity = _parse_dt(last_activity_date)
    if new_activity:
        app_obj.last_activity_date = new_activity

    # updated_at needs care. The column carries onupdate=_utcnow, which fires
    # only when the column is absent from the UPDATE's SET clause -- so an
    # explicit assignment does win. But the form round-trips the current value
    # on every save, and blindly assigning it back would freeze updated_at
    # forever: "last modified" would quietly become "whatever was in the box."
    # So only override when the submitted value actually differs from what's
    # stored; otherwise leave the column alone and let onupdate stamp now.
    new_updated = _parse_dt(updated_at)
    if new_updated and not _same_to_the_minute(new_updated, app_obj.updated_at):
        app_obj.updated_at = new_updated

    # --- Automatic classification ----------------------------------------- #
    # Fires only when the *linked posting changed*, which is the moment new job
    # description text arrives and the old classification stops describing it.
    # Deliberately not on every save: editing a note or fixing a date must not
    # cost an API call, and the previous answer is still a correct answer to a
    # JD nobody touched. Same guard shape as the thread read, which fires on a
    # body change rather than on any save.
    #
    # And never on a request where you set the values yourself. Without that,
    # clearing a field while also relinking the posting would put the model's
    # answer straight back -- the exact bug found in the thread read.
    claimed = _hand_edit_claims_the_classification(app_obj, *class_before)
    if not claimed and app_obj.job_posting_id != posting_before:
        _classify_application_now(app_obj)

    db.commit()
    return RedirectResponse(url="/board", status_code=303)


@router.post("/ui/applications/{application_id}/classify")
def classify_application_ui(application_id: int, db: Session = Depends(get_db)):
    """Classify now, on request, for a record the automatic pass skipped.

    The automatic pass only fires when the linked posting changes, so an
    application that predates the feature, or one whose posting was linked
    before it existed, needs a way to ask. Also the way to re-run one after
    clearing a hand-entered value.
    """
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    error = _classify_application_now(app_obj)
    if error:
        db.rollback()
        message = error
    else:
        db.commit()
        parts = []
        if app_obj.seniority:
            parts.append(app_obj.seniority.value)
        if app_obj.speciality:
            parts.append(app_obj.speciality.value)
        message = ("read the posting as {}".format(" / ".join(parts)) if parts
                   else "read the posting and could not place it in either "
                        "picklist (left blank rather than guessed)")
    return RedirectResponse(
        url="/applications/{}/edit?classify_result={}".format(
            application_id, quote(message)),
        status_code=303)


@router.post("/ui/applications/{application_id}/delete")
def delete_application_ui(application_id: int, db: Session = Depends(get_db)):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    db.delete(app_obj)  # cascades to stage_history and meetings
    db.commit()
    return RedirectResponse(url="/board", status_code=303)


@router.post("/ui/stage-history/{history_id}/edit")
def update_stage_history_ui(
    history_id: int,
    changed_at: str = Form(...),
    db: Session = Depends(get_db),
):
    """Correct when a stage change actually happened. The board/edit-form
    stage change always logs `changed_at` as the moment you clicked/dragged
    in the app -- which is often later than the real-world transition. This
    lets you fix that after the fact without touching the stage itself
    (from_stage/to_stage stay as recorded; only the timestamp changes).
    """
    history = _get_or_404(db, models.StageHistory, history_id)
    new_dt = _parse_dt(changed_at)
    if new_dt:
        history.changed_at = new_dt
    db.commit()
    return RedirectResponse(url=f"/applications/{history.application_id}/edit", status_code=303)


# --------------------------------------------------------------------------- #
# Postings (triage + rating loop)
# --------------------------------------------------------------------------- #
@router.get("/postings")
def postings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "postings.html", {
        "active": "postings",
        "postings": db.query(models.JobPosting).order_by(models.JobPosting.last_seen_at.desc()).all(),
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
    })


# Job-board / ATS domains — for these, the posting URL's domain is the board,
# not the employer, so we don't infer a company website from it.
_ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "jobvite.com", "smartrecruiters.com",
    "bamboohr.com", "breezy.hr", "workable.com", "icims.com", "teamtailor.com",
)


def _company_website(source_url: Optional[str]) -> Optional[str]:
    """Infer a company website from a posting URL.

    When the posting is hosted on the company's own domain (e.g.
    ``plaid.com/careers/...``), that domain *is* the company site. Skipped for
    ATS/job-board domains, where the domain is the board rather than the employer.
    """
    if not source_url:
        return None
    parsed = urlparse(
        source_url if re.match(r"^https?://", source_url, re.I) else "https://" + source_url
    )
    host = (parsed.netloc or "").lower().split(":")[0]
    if not host or any(host == d or host.endswith("." + d) for d in _ATS_DOMAINS):
        return None
    if host.startswith("www."):
        host = host[4:]
    return f"https://{host}"


def _find_or_create_company(
    name: str, db: Session, source_url: Optional[str] = None
) -> models.Company:
    """Look up a company by name (case-insensitive), creating it if missing.

    This is what makes the flow posting-first: you add a posting and the
    company (Account) is created automatically if it doesn't exist yet — no
    need to set up the company beforehand. When we can infer a website from the
    posting URL, we set it on creation (and backfill it onto an existing company
    that doesn't have one yet).
    """
    name = (name or "").strip() or "Unknown company"
    website = _company_website(source_url)
    existing = (
        db.query(models.Company)
        .filter(models.Company.name.ilike(name))
        .first()
    )
    if existing:
        if website and not existing.website:
            existing.website = website
        return existing
    company = models.Company(
        name=name, company_type=models.CompanyType.EMPLOYER, website=website
    )
    db.add(company)
    db.flush()  # assigns company.id within this transaction
    return company


def _domain_of(email: str) -> Optional[str]:
    email = (email or "").strip().lower()
    return email.split("@", 1)[1] if "@" in email else None


def _company_name_from_domain(domain: str) -> str:
    """Turn "condorsoftware.com" into "Condorsoftware" -- a rough guess, not
    a real company-name lookup service. Good enough as a starting point; the
    Company is fully editable afterward like any auto-created record here."""
    label = domain.split(".")[0]
    return re.sub(r"[-_]+", " ", label).strip().title() or domain


def _find_or_create_company_by_domain(email: str, db: Session) -> models.Company:
    """Find a company whose website matches this email's domain, or create
    one. Checked by domain (not name) because that's the only signal an email
    address gives us -- and it's also how a person's auto-created company
    should be found again if a second person at the same company emails you
    later. Existing companies win over creating a duplicate.
    """
    domain = _domain_of(email)
    if domain:
        for company in db.query(models.Company).filter(models.Company.website.isnot(None)):
            host = urlparse(
                company.website if re.match(r"^https?://", company.website, re.I)
                else "https://" + company.website
            ).netloc.lower().split(":")[0]
            if host.startswith("www."):
                host = host[4:]
            if host == domain:
                return company
    name = _company_name_from_domain(domain) if domain else "Unknown company"
    company = models.Company(
        name=name,
        company_type=models.CompanyType.EMPLOYER,
        website=f"https://{domain}" if domain else None,
    )
    db.add(company)
    db.flush()
    return company


def _find_or_create_person_by_email(
    email: str, db: Session, name: Optional[str] = None, application_id: Optional[int] = None
) -> models.Person:
    """Look up a Person by email (case-insensitive), creating one if missing.
    Email is the dedup key: the same address always resolves to the same
    Person record, regardless of which thread or upload mentions it. Existing
    people are returned as-is (never overwritten) so a later, blanker upload
    can't clobber details you've already filled in by hand.
    """
    email = (email or "").strip().lower()
    existing = db.query(models.Person).filter(models.Person.email.ilike(email)).first()
    if existing:
        return existing
    company = _find_or_create_company_by_domain(email, db)
    person = models.Person(
        name=(name or "").strip() or email,
        company_id=company.id,
        application_id=application_id,
        role=models.PersonRole.OTHER,
        email=email,
    )
    db.add(person)
    db.flush()
    return person


def _to_float(value: str):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.post("/ui/postings")
def create_posting_ui(
    company_name: str = Form(...),
    title: str = Form(...),
    location: str = Form(""),
    url: str = Form(""),
    jd_text: str = Form(""),
    comp_min: str = Form(""),
    comp_max: str = Form(""),
    db: Session = Depends(get_db),
):
    company = _find_or_create_company(company_name, db, source_url=url)
    db.add(models.JobPosting(
        company_id=company.id,
        title=title,
        location=location or None,
        url=url or None,
        jd_text=jd_text or None,
        comp_min=_to_float(comp_min),
        comp_max=_to_float(comp_max),
    ))
    db.commit()
    return RedirectResponse(url="/postings", status_code=303)


@router.post("/ui/postings/{posting_id}/rate")
def rate_posting_ui(
    posting_id: int,
    rating: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    posting = db.get(models.JobPosting, posting_id)
    if posting:
        posting.my_rating = models.Rating(rating)
        posting.rating_reason = reason or None
        posting.rated_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/postings", status_code=303)


@router.get("/postings/{posting_id}/edit")
def edit_posting_page(posting_id: int, request: Request, db: Session = Depends(get_db)):
    posting = _get_or_404(db, models.JobPosting, posting_id)
    return templates.TemplateResponse(request, "posting_edit.html", {
        "active": "postings",
        "posting": posting,
    })


@router.post("/ui/postings/{posting_id}/edit")
def update_posting_ui(
    posting_id: int,
    company_name: str = Form(...),
    title: str = Form(...),
    location: str = Form(""),
    url: str = Form(""),
    jd_text: str = Form(""),
    comp_min: str = Form(""),
    comp_max: str = Form(""),
    db: Session = Depends(get_db),
):
    posting = _get_or_404(db, models.JobPosting, posting_id)
    company = _find_or_create_company(company_name, db, source_url=url)
    posting.company_id = company.id
    posting.title = title
    posting.location = location or None
    posting.url = url or None
    posting.jd_text = jd_text or None
    posting.comp_min = _to_float(comp_min)
    posting.comp_max = _to_float(comp_max)
    db.commit()
    return RedirectResponse(url="/postings", status_code=303)


@router.post("/ui/postings/{posting_id}/delete")
def delete_posting_ui(posting_id: int, db: Session = Depends(get_db)):
    posting = _get_or_404(db, models.JobPosting, posting_id)
    db.delete(posting)  # applications pointing here just lose the link (SET NULL)
    db.commit()
    return RedirectResponse(url="/postings", status_code=303)


# --------------------------------------------------------------------------- #
# Companies
# --------------------------------------------------------------------------- #
@router.get("/companies")
def companies_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "companies.html", {
        "active": "companies",
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "company_types": COMPANY_TYPES,
    })


@router.post("/ui/companies")
def create_company_ui(
    name: str = Form(...),
    company_type: str = Form("Employer"),
    website: str = Form(""),
    industry: str = Form(""),
    db: Session = Depends(get_db),
):
    db.add(models.Company(
        name=name,
        company_type=models.CompanyType(company_type),
        website=website or None,
        industry=industry or None,
    ))
    db.commit()
    return RedirectResponse(url="/companies", status_code=303)


@router.get("/companies/{company_id}/edit")
def edit_company_page(company_id: int, request: Request,
                      lookup_result: str = "", db: Session = Depends(get_db)):
    company = _get_or_404(db, models.Company, company_id)
    return templates.TemplateResponse(request, "company_edit.html", {
        "active": "companies",
        "company": company,
        "company_types": COMPANY_TYPES,
        "funding_stages": FUNDING_STAGE_VALUES,
        "employee_bands": EMPLOYEE_BAND_VALUES,
        # Reports what a press did even on success, because "the site does not
        # say" is a legitimate outcome that leaves both fields blank -- which
        # looks exactly like the button not working.
        "lookup_result": lookup_result,
        "lookup_enabled": llm.enabled(),
    })


@router.post("/ui/companies/{company_id}/enrich")
def enrich_company_ui(company_id: int, db: Session = Depends(get_db)):
    """Read funding stage and headcount off the company's own site.

    A button, never automatic: it costs a fetch and an API call, and it is the
    one derived field in this app that cannot be checked against anything
    already on the record.
    """
    company = _get_or_404(db, models.Company, company_id)
    error = _enrich_company_now(company)
    if error:
        db.rollback()
        return RedirectResponse(
            url="/companies/{}/edit?lookup_result={}".format(
                company_id, quote(error)),
            status_code=303)
    db.commit()

    found = []
    if company.funding_stage:
        found.append(company.funding_stage.value)
    if company.employee_band:
        found.append("{} employees".format(company.employee_band.value))
    message = ("read {} from {}".format(" and ".join(found), company.enrichment_url)
               if found else
               "read {} and found nothing it states about funding or headcount "
               "(left blank rather than guessed)".format(company.enrichment_url))
    return RedirectResponse(
        url="/companies/{}/edit?lookup_result={}".format(company_id, quote(message)),
        status_code=303)


@router.post("/ui/companies/{company_id}/edit")
def update_company_ui(
    company_id: int,
    name: str = Form(...),
    company_type: str = Form("Employer"),
    website: str = Form(""),
    industry: str = Form(""),
    notes: str = Form(""),
    funding_stage: str = Form(""),
    employee_band: str = Form(""),
    db: Session = Depends(get_db),
):
    company = _get_or_404(db, models.Company, company_id)
    before = (company.funding_stage, company.employee_band)

    company.name = name
    company.company_type = models.CompanyType(company_type)
    company.website = website or None
    company.industry = industry or None
    company.notes = notes or None
    company.funding_stage = (
        models.FundingStage(funding_stage) if funding_stage else None)
    company.employee_band = (
        models.EmployeeBand(employee_band) if employee_band else None)

    # Typing in either field takes ownership of both, and drops the machine's
    # note, model and source URL along with it. Same rule as every other
    # provenance pair in this app: a value you set is never overwritten, and it
    # must not go on carrying a citation that no longer describes it.
    if (company.funding_stage, company.employee_band) != before:
        company.enrichment_source = None
        company.enrichment_note = None
        company.enrichment_url = None
        company.enrichment_model = None
        company.enriched_at = None

    db.commit()
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/ui/companies/{company_id}/delete")
def delete_company_ui(company_id: int, db: Session = Depends(get_db)):
    company = _get_or_404(db, models.Company, company_id)
    db.delete(company)  # cascades to its postings, applications, and people
    db.commit()
    return RedirectResponse(url="/companies", status_code=303)


# --------------------------------------------------------------------------- #
# Resumes
# --------------------------------------------------------------------------- #
@router.get("/resumes")
def resumes_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "resumes.html", {
        "active": "resumes",
        "resumes": db.query(models.Resume).order_by(models.Resume.created_at.desc()).all(),
    })


@router.post("/ui/resumes")
def create_resume_ui(
    label: str = Form(...),
    source_link: str = Form(""),
    notes: str = Form(""),
    pasted_text: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Create a resume version. If a file is uploaded, extract its text;
    otherwise use pasted text. Either way, `content` holds plain text for
    analysis, and `source_link` can reference the original (e.g. a Drive URL).
    """
    text_content = (pasted_text or "").strip()
    filename = None
    if file is not None and file.filename:
        filename = file.filename
        data = file.file.read()
        if data:
            try:
                extracted = extract_text(filename, data)
            except Exception:
                extracted = ""
            if extracted:
                text_content = extracted

    db.add(models.Resume(
        label=label,
        content=text_content or None,
        source_link=source_link or None,
        filename=filename,
        notes=notes or None,
    ))
    db.commit()
    return RedirectResponse(url="/resumes", status_code=303)


@router.get("/resumes/{resume_id}/edit")
def edit_resume_page(resume_id: int, request: Request, db: Session = Depends(get_db)):
    resume = _get_or_404(db, models.Resume, resume_id)
    return templates.TemplateResponse(request, "resume_edit.html", {
        "active": "resumes",
        "resume": resume,
    })


@router.post("/ui/resumes/{resume_id}/edit")
def update_resume_ui(
    resume_id: int,
    label: str = Form(...),
    source_link: str = Form(""),
    notes: str = Form(""),
    pasted_text: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Update a resume version. Re-uploading a file re-extracts and replaces
    the text; otherwise the pasted-text box (pre-filled with the current
    extracted text) is the source of truth, so you can hand-fix extraction
    glitches without re-uploading anything.
    """
    resume = _get_or_404(db, models.Resume, resume_id)
    text_content = (pasted_text or "").strip()
    if file is not None and file.filename:
        resume.filename = file.filename
        data = file.file.read()
        if data:
            try:
                extracted = extract_text(file.filename, data)
            except Exception:
                extracted = ""
            if extracted:
                text_content = extracted

    resume.label = label
    resume.content = text_content or None
    resume.source_link = source_link or None
    resume.notes = notes or None
    db.commit()
    return RedirectResponse(url="/resumes", status_code=303)


@router.post("/ui/resumes/{resume_id}/delete")
def delete_resume_ui(resume_id: int, db: Session = Depends(get_db)):
    resume = _get_or_404(db, models.Resume, resume_id)
    db.delete(resume)  # applications using it just lose the link (SET NULL)
    db.commit()
    return RedirectResponse(url="/resumes", status_code=303)


# --------------------------------------------------------------------------- #
# Meetings (interviews / calls, optionally imported from Granola)
# --------------------------------------------------------------------------- #
def _parse_dt(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)  # handles YYYY-MM-DD and ...THH:MM
    except ValueError:
        return None


def _same_to_the_minute(a, b) -> bool:
    """Compare two datetimes at the resolution the form can actually express.

    `<input type="datetime-local">` renders and submits YYYY-MM-DDTHH:MM, so a
    stored timestamp's seconds and microseconds never survive the round trip.
    Comparing raw datetimes would therefore report a difference on every save
    for any value that was set programmatically (which all of them are, since
    the defaults use datetime.now). Truncating both sides to the minute is what
    makes "did the user actually change this field?" answerable.
    """
    if a is None or b is None:
        return a is b
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


def _extract_upload_text(file: Optional[UploadFile]) -> str:
    """Extract text from an uploaded file (PDF/DOCX/TXT), reusing the same
    extractor Resume upload uses. Returns "" if there's no file, an empty
    file, or extraction fails -- callers fall back to pasted text either way.
    """
    if file is None or not file.filename:
        return ""
    data = file.file.read()
    if not data:
        return ""
    try:
        return extract_text(file.filename, data)
    except Exception:
        return ""


@router.get("/meetings")
def meetings_page(
    request: Request, application_id: Optional[int] = None, db: Session = Depends(get_db)
):
    return templates.TemplateResponse(request, "meetings.html", {
        "active": "meetings",
        "meetings": db.query(models.Meeting).order_by(models.Meeting.created_at.desc()).all(),
        "applications": db.query(models.JobApplication).all(),
        "meeting_types": [t.value for t in models.MeetingType],
        "preselect_application_id": application_id,
        "granola_enabled": granola.enabled(),
    })


@router.post("/ui/meetings")
def create_meeting_ui(
    application_id: int = Form(...),
    title: str = Form(""),
    meeting_type: str = Form(""),
    meeting_date: str = Form(""),
    summary: str = Form(""),
    transcript: str = Form(""),
    notes: str = Form(""),
    granola_note_id: str = Form(""),
    granola_link: str = Form(""),
    score: str = Form(""),
    score_reason: str = Form(""),
    my_performance: str = Form(""),
    employer_engagement: str = Form(""),
    db: Session = Depends(get_db),
):
    meeting = models.Meeting(
        application_id=application_id,
        title=title or None,
        meeting_type=models.MeetingType(meeting_type) if meeting_type else None,
        meeting_date=_parse_dt(meeting_date),
        summary=summary or None,
        transcript=transcript or None,
        notes=notes or None,
        granola_note_id=granola_note_id or None,
        granola_link=granola_link or None,
    )
    _apply_score(meeting, score, score_reason)
    _apply_activity_quality(meeting, my_performance, employer_engagement)
    db.add(meeting)
    db.commit()
    return RedirectResponse(url="/meetings", status_code=303)


@router.get("/meetings/{meeting_id}/edit")
def edit_meeting_page(meeting_id: int, request: Request, db: Session = Depends(get_db)):
    meeting = _get_or_404(db, models.Meeting, meeting_id)
    return templates.TemplateResponse(request, "meeting_edit.html", {
        "active": "meetings",
        "meeting": meeting,
        "applications": db.query(models.JobApplication).all(),
        "meeting_types": [t.value for t in models.MeetingType],
        "granola_enabled": granola.enabled(),
    })


@router.post("/ui/meetings/{meeting_id}/edit")
def update_meeting_ui(
    meeting_id: int,
    application_id: int = Form(...),
    title: str = Form(""),
    meeting_type: str = Form(""),
    meeting_date: str = Form(""),
    summary: str = Form(""),
    transcript: str = Form(""),
    notes: str = Form(""),
    granola_note_id: str = Form(""),
    granola_link: str = Form(""),
    score: str = Form(""),
    score_reason: str = Form(""),
    my_performance: str = Form(""),
    employer_engagement: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update a meeting. This is also how you (re)attach a Granola transcript:
    the edit page carries the same "Load Granola notes / Import selected"
    controls as creation — picking a note there overwrites the title/summary/
    transcript/date/link fields below before you save, so a meeting that was
    imported before a Granola fix (or matched to the wrong note) can be
    re-imported without deleting and recreating it.
    """
    meeting = _get_or_404(db, models.Meeting, meeting_id)
    meeting.application_id = application_id
    meeting.title = title or None
    meeting.meeting_type = models.MeetingType(meeting_type) if meeting_type else None
    meeting.meeting_date = _parse_dt(meeting_date)
    meeting.summary = summary or None
    meeting.transcript = transcript or None
    meeting.notes = notes or None
    meeting.granola_note_id = granola_note_id or None
    meeting.granola_link = granola_link or None
    _apply_score(meeting, score, score_reason)
    _apply_activity_quality(meeting, my_performance, employer_engagement)
    db.commit()
    return RedirectResponse(url="/meetings", status_code=303)


@router.post("/ui/meetings/{meeting_id}/delete")
def delete_meeting_ui(meeting_id: int, db: Session = Depends(get_db)):
    meeting = _get_or_404(db, models.Meeting, meeting_id)
    db.delete(meeting)
    db.commit()
    return RedirectResponse(url="/meetings", status_code=303)


# --------------------------------------------------------------------------- #
# People (Contacts): recruiters, hiring managers, interviewers, referrals.
# Their "own" employer (company_id) is deliberately independent of whichever
# application they're optionally tied to -- an agency recruiter's employer is
# the agency, not the company you're interviewing at. See ARCHITECTURE.md.
# --------------------------------------------------------------------------- #
@router.get("/people")
def people_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "people.html", {
        "active": "people",
        "people": db.query(models.Person).order_by(models.Person.name).all(),
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "applications": db.query(models.JobApplication).all(),
        "person_roles": PERSON_ROLE_VALUES,
    })


@router.post("/ui/people")
def create_person_ui(
    name: str = Form(...),
    company_id: int = Form(...),
    application_id: Optional[str] = Form(None),
    role: str = Form("Recruiter"),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin: str = Form(""),
    is_champion: Optional[str] = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    db.add(models.Person(
        name=name,
        company_id=company_id,
        application_id=int(application_id) if application_id else None,
        role=models.PersonRole(role),
        email=email or None,
        phone=phone or None,
        linkedin=linkedin or None,
        is_champion=1 if is_champion else 0,
        notes=notes or None,
    ))
    db.commit()
    return RedirectResponse(url="/people", status_code=303)


@router.get("/people/{person_id}/edit")
def edit_person_page(person_id: int, request: Request, db: Session = Depends(get_db)):
    person = _get_or_404(db, models.Person, person_id)
    return templates.TemplateResponse(request, "person_edit.html", {
        "active": "people",
        "person": person,
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "applications": db.query(models.JobApplication).all(),
        "person_roles": PERSON_ROLE_VALUES,
    })


@router.post("/ui/people/{person_id}/edit")
def update_person_ui(
    person_id: int,
    name: str = Form(...),
    company_id: int = Form(...),
    application_id: Optional[str] = Form(None),
    role: str = Form("Recruiter"),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin: str = Form(""),
    is_champion: Optional[str] = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    person = _get_or_404(db, models.Person, person_id)
    person.name = name
    person.company_id = company_id
    person.application_id = int(application_id) if application_id else None
    person.role = models.PersonRole(role)
    person.email = email or None
    person.phone = phone or None
    person.linkedin = linkedin or None
    person.is_champion = 1 if is_champion else 0
    person.notes = notes or None
    db.commit()
    return RedirectResponse(url="/people", status_code=303)


@router.post("/ui/people/{person_id}/delete")
def delete_person_ui(person_id: int, db: Session = Depends(get_db)):
    person = _get_or_404(db, models.Person, person_id)
    db.delete(person)  # unlinks (doesn't delete) any email threads they're on
    db.commit()
    return RedirectResponse(url="/people", status_code=303)


# --------------------------------------------------------------------------- #
# Email Threads: recruiter/HM email exchanges, pasted in manually for now.
# Related to People through a many-to-many join table, not a single required
# "owner" -- a thread can genuinely involve more than one person (an intro
# thread, a BCC'd hiring manager). Application is an optional lookup set once
# the thread is actually about a role. See ARCHITECTURE.md.
# --------------------------------------------------------------------------- #
@router.get("/email-threads")
def email_threads_page(
    request: Request,
    person_id: Optional[int] = None,
    application_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(request, "email_threads.html", {
        "active": "emails",
        "threads": db.query(models.EmailThread)
        .order_by(models.EmailThread.last_message_at.desc().nullslast())
        .all(),
        "people": db.query(models.Person).order_by(models.Person.name).all(),
        "applications": db.query(models.JobApplication).all(),
        "preselect_person_id": person_id,
        "preselect_application_id": application_id,
    })


def _resolve_thread_people(
    person_ids_form: List[str],
    parsed: dict,
    db: Session,
    application_id: Optional[int],
    required: bool = True,
) -> List["models.Person"]:
    """Resolve which People an email thread involves. An explicit selection
    in the form always wins outright (auto-detection is skipped entirely) --
    picking zero people on purpose is a valid, if unusual, choice, same as
    every other "manual overrides auto-fill" rule in this app. Left empty,
    every real sender found in a Gmail-shaped upload/paste gets
    found-or-created by email (dedup key: lowercased email address) and
    linked -- so cc'd/other repliers all end up attached to the thread, not
    just whichever one happened to be found first.

    ``required`` distinguishes create from edit: a brand-new thread about
    nobody doesn't make sense, so create fails loudly if nothing could be
    resolved either way. An existing thread ending up with zero people
    (e.g. you unchecked everyone to unlink a contact who's since left the
    company) is a legitimate state, not an error -- an orphaned thread can
    just be deleted later if you don't want it hanging around.
    """
    if person_ids_form:
        ids = [int(pid) for pid in person_ids_form if pid]
        return db.query(models.Person).filter(models.Person.id.in_(ids)).all() if ids else []
    other_senders = parsed.get("other_senders") or []
    if not other_senders:
        if required:
            raise HTTPException(
                400,
                "Couldn't detect who this thread is with, so at least one Person is "
                "required. Either pick one or more from the list, or upload/paste a "
                "Gmail-exported thread (Gmail's \"Print all\") so it can be detected "
                "automatically.",
            )
        return []
    return [
        _find_or_create_person_by_email(
            sender["email"], db, name=sender["name"], application_id=application_id
        )
        for sender in other_senders
    ]


@router.post("/ui/email-threads")
def create_email_thread_ui(
    person_ids: List[str] = Form([]),
    application_id: Optional[str] = Form(None),
    subject: str = Form(""),
    body: str = Form(""),
    participants: str = Form(""),
    started_at: str = Form(""),
    last_message_at: str = Form(""),
    notes: str = Form(""),
    score: str = Form(""),
    score_reason: str = Form(""),
    my_performance: str = Form(""),
    employer_engagement: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Create an email thread. Same dual-input pattern as Resume: paste the
    text directly, or upload a file (a PDF export/print of the thread works
    great) and it gets extracted automatically via the same extractor Resume
    uses. An uploaded file wins over pasted text when both are present.

    If the text looks like a Gmail thread export, subject/participants/dates
    are auto-filled from it -- but only into fields you left blank, so
    anything you typed by hand already wins. People work the same way: leave
    the list on "auto-detect" and everyone who actually sent a message is
    found-or-created by email address and linked; pick people explicitly and
    that always overrides detection.
    """
    body_text = _extract_upload_text(file) or (body or "").strip()
    parsed = parse_gmail_export(body_text)
    app_id = int(application_id) if application_id else None
    resolved_people = _resolve_thread_people(person_ids, parsed, db, app_id)
    thread = models.EmailThread(
        application_id=app_id,
        subject=(subject or "").strip() or parsed["subject"],
        body=body_text or None,
        participants=(participants or "").strip() or parsed["participants"],
        started_at=_parse_dt(started_at) or parsed["started_at"],
        last_message_at=_parse_dt(last_message_at) or parsed["last_message_at"],
        notes=notes or None,
    )
    _apply_score(thread, score, score_reason)
    _apply_activity_quality(thread, my_performance, employer_engagement)
    thread.people = resolved_people
    db.add(thread)
    db.commit()
    # Read it now, while the thread is new and you have not formed a view. Any
    # failure is swallowed rather than blocking the redirect: the thread itself
    # saved fine, and the edit page reports the read's state plainly with a
    # "Read it now" button beside it, so a silent failure shows up as "not read
    # yet" next to the thing that retries it.
    _read_thread_now(thread)
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)


@router.get("/email-threads/{thread_id}/edit")
def edit_email_thread_page(
    thread_id: int,
    request: Request,
    read_error: str = "",
    db: Session = Depends(get_db),
):
    thread = _get_or_404(db, models.EmailThread, thread_id)
    return templates.TemplateResponse(request, "email_thread_edit.html", {
        "active": "emails",
        "thread": thread,
        "people": db.query(models.Person).order_by(models.Person.name).all(),
        "applications": db.query(models.JobApplication).all(),
        "selected_person_ids": {p.id for p in thread.people},
        # The automatic read on save swallows its errors so a failed API call
        # cannot cost you a saved thread. The button below does not: when you
        # asked for it, you are owed the reason it did not happen.
        "read_error": read_error,
        "read_enabled": llm.enabled(),
        "has_human_rating": _has_human_rating(thread),
    })


@router.post("/ui/email-threads/{thread_id}/read")
def read_email_thread_ui(thread_id: int, db: Session = Depends(get_db)):
    """Read this thread on demand — the backfill path for everything that was
    already in the database before the automatic read existed, and the retry
    for anything whose read failed.

    It obeys exactly the same guard as the automatic path: a rating you typed
    is never overwritten, so pressing this on a thread you have already judged
    does nothing at all. Re-reading a model-written rating is fine and replaces
    it, since nothing of yours is at stake.
    """
    thread = _get_or_404(db, models.EmailThread, thread_id)
    error = _read_thread_now(thread)
    db.commit()
    url = "/email-threads/{}/edit".format(thread_id)
    if error:
        url += "?read_error=" + quote(error)
    return RedirectResponse(url=url, status_code=303)


@router.post("/ui/email-threads/{thread_id}/edit")
def update_email_thread_ui(
    thread_id: int,
    person_ids: List[str] = Form([]),
    application_id: Optional[str] = Form(None),
    subject: str = Form(""),
    body: str = Form(""),
    participants: str = Form(""),
    started_at: str = Form(""),
    last_message_at: str = Form(""),
    notes: str = Form(""),
    score: str = Form(""),
    score_reason: str = Form(""),
    my_performance: str = Form(""),
    employer_engagement: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Update an email thread. Uploading a file re-extracts and replaces the
    body; otherwise the pasted-text box (pre-filled with the current body) is
    the source of truth, so extraction glitches can be hand-fixed without
    re-uploading — same pattern as editing a Resume. As on create, a
    Gmail-shaped body only fills in fields left blank; it never overwrites
    something already typed in the form -- and People auto-detection only
    runs off a freshly uploaded file, never off the already-saved body, so
    just re-saving the form can't unexpectedly relink the thread to someone
    else or spawn a duplicate Person. Leaving the People list checked as-is
    (the normal case) simply keeps it unchanged, since the form always
    submits the currently-checked people back as an explicit selection.
    """
    thread = _get_or_404(db, models.EmailThread, thread_id)
    before_perf, before_eng = thread.my_performance, thread.employer_engagement
    body_before = thread.body
    extracted = _extract_upload_text(file)
    body_text = extracted or (body or "").strip()
    parsed = parse_gmail_export(body_text) if extracted else {
        "subject": None, "participants": None, "started_at": None, "last_message_at": None,
        "other_senders": [],
    }
    app_id = int(application_id) if application_id else None
    thread.people = _resolve_thread_people(person_ids, parsed, db, app_id, required=False)
    thread.application_id = app_id
    thread.subject = (subject or "").strip() or parsed["subject"]
    thread.body = body_text or None
    thread.participants = (participants or "").strip() or parsed["participants"]
    thread.started_at = _parse_dt(started_at) or parsed["started_at"]
    thread.last_message_at = _parse_dt(last_message_at) or parsed["last_message_at"]
    thread.notes = notes or None
    _apply_score(thread, score, score_reason)
    _apply_activity_quality(thread, my_performance, employer_engagement)
    claimed = _hand_edit_claims_the_rating(thread, before_perf, before_eng)
    # Re-read only when the messages themselves changed, and never on a save
    # where you touched the ratings yourself. Saving a subject fix or relinking
    # a person must not cost an API call, and the previous read is still a
    # correct read of a body nobody touched.
    if not claimed and (thread.body or "") != (body_before or ""):
        _read_thread_now(thread)
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)


@router.post("/ui/email-threads/{thread_id}/delete")
def delete_email_thread_ui(thread_id: int, db: Session = Depends(get_db)):
    thread = _get_or_404(db, models.EmailThread, thread_id)
    db.delete(thread)
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)


# --------------------------------------------------------------------------- #
# Chat: ask questions about the whole pipeline
# --------------------------------------------------------------------------- #
# The third and widest place data leaves the box. The Brief sends one
# application when you press a button; the thread read sends one thread when
# you save it; this sends everything, on every question. That was a deliberate
# choice made with the alternatives on the table -- see the module note in
# chat.py -- and this is the code that carries it out, so it is worth being
# able to read in one place exactly what gets assembled.
#
# Same division of labour as the Brief: all ORM walking happens here, and
# `chat.py` stays plain-data-in, text-out and testable with literals.

# How many turns of the stored conversation are replayed to the model. The
# history sits *after* the cached corpus block and so is billed fresh every
# time; letting it grow without bound would slowly eat the saving the cache
# exists to produce. Twelve is far more context than any real follow-up needs.
CHAT_HISTORY_TURNS = 12

# The chat's own ceiling, well above the Brief's. Answers here are often "list
# the four that have gone quiet and why", which is genuinely longer than a
# two-section brief, and truncating mid-list is a worse failure than a slightly
# larger bill.
CHAT_MAX_TOKENS = 2000


def _chat_corpus(db: Session, analytics: Optional[dict] = None) -> str:
    """Walk every application into the plain dicts `chat.build_corpus` wants.

    One eager load for the whole page rather than per-application lazy loads:
    this touches every relationship on every row, which is exactly the shape
    that turns into hundreds of queries if left to default loading.
    """
    apps = (
        db.query(models.JobApplication)
        .options(
            selectinload(models.JobApplication.meetings),
            selectinload(models.JobApplication.email_threads),
            selectinload(models.JobApplication.people),
            selectinload(models.JobApplication.stage_history),
            selectinload(models.JobApplication.company),
            selectinload(models.JobApplication.resume),
            selectinload(models.JobApplication.job_posting),
        )
        .all()
    )
    payload = []
    for a in apps:
        posting = None
        if a.job_posting:
            posting = {
                "title": a.job_posting.title,
                "url": a.job_posting.url,
                "location": a.job_posting.location,
                "jd_text": a.job_posting.jd_text,
            }
        payload.append({
            "company": a.company.name if a.company else None,
            "title": a.title,
            "stage": a.stage.value if a.stage else None,
            "source": a.source.value if a.source else None,
            # Every date goes through `_naive_utc` on the way out. Form-entered
            # dates are naive and stamped ones are aware, and chat.py sorts and
            # subtracts across the whole mix to work out what is most recent --
            # which raises TypeError the moment the two meet unflattened.
            "applied_date": _naive_utc(a.applied_date),
            "champion": a.champion,
            "manual_forecast": a.manual_forecast.value if a.manual_forecast else None,
            "seniority": a.seniority.value if a.seniority else None,
            "speciality": a.speciality.value if a.speciality else None,
            "funding_stage": (a.company.funding_stage.value
                              if a.company and a.company.funding_stage else None),
            "employee_band": (a.company.employee_band.value
                              if a.company and a.company.employee_band else None),
            "lost_reason": a.lost_reason,
            "lost_category": a.lost_category.value if a.lost_category else None,
            "context": a.context,
            "next_steps": a.next_steps,
            "pain": a.pain,
            "process": a.process,
            "risks": a.risks,
            "notes": a.notes,
            "resume_label": a.resume.label if a.resume else None,
            "posting": posting,
            "people": [
                {
                    "name": p.name,
                    "role": p.role.value if p.role else None,
                    "email": p.email,
                    "company": p.company.name if p.company else None,
                    "is_champion": p.is_champion,
                }
                for p in a.people
            ],
            "stage_history": [
                {
                    "changed_at": _naive_utc(h.changed_at),
                    "from_stage": h.from_stage.value if h.from_stage else None,
                    "to_stage": h.to_stage.value if h.to_stage else None,
                }
                for h in a.stage_history
            ],
            "meetings": [
                {
                    # `when` is the one key both activity kinds share, because
                    # recency is what the budget is spent on and a meeting date
                    # and a thread's last message are the same fact for that
                    # purpose. Falling back to `scored_at` keeps an undated
                    # meeting from sorting to the beginning of time and being
                    # dropped first, which is the opposite of what you want for
                    # something logged this week without a date typed in.
                    "when": _naive_utc(m.meeting_date or m.scored_at or m.created_at),
                    "title": m.title,
                    "kind": m.meeting_type.value if m.meeting_type else None,
                    "summary": m.summary,
                    "transcript": m.transcript,
                    "notes": m.notes,
                    "score": m.score,
                    "score_reason": m.score_reason,
                    "my_performance": m.my_performance,
                    "employer_engagement": m.employer_engagement,
                }
                for m in a.meetings
            ],
            "email_threads": [
                {
                    "when": _naive_utc(t.last_message_at or t.started_at
                                       or t.scored_at or t.created_at),
                    "subject": t.subject,
                    "participants": t.participants,
                    "body": t.body,
                    "notes": t.notes,
                    "score": t.score,
                    "score_reason": t.score_reason,
                    "my_performance": t.my_performance,
                    "employer_engagement": t.employer_engagement,
                    # So the packet can say a rating was written by an
                    # automatic read rather than by hand. Same labelling the
                    # Brief does, and for the same reason: a number the model
                    # wrote should not come back to it as corroboration.
                    "rating_source": t.rating_source,
                }
                for t in a.email_threads
            ],
        })
    return chat_model.build_corpus(payload, analytics=analytics)


def _chat_history(db: Session) -> List[models.ChatMessage]:
    return (
        db.query(models.ChatMessage)
        .order_by(models.ChatMessage.id)
        .all()
    )


@router.get("/chat")
def chat_redirect():
    """The chat and the analytics page merged into /insights.

    Kept as a redirect rather than deleted: this URL has been in the nav, in
    the README, and in Gabe's browser history. A 301 costs one line and means
    nothing that already points here breaks.
    """
    return RedirectResponse(url="/insights", status_code=301)


@router.post("/ui/chat/clear")
def chat_clear(db: Session = Depends(get_db)):
    """Delete the whole conversation.

    A real delete, not a hidden flag. This is the only place in the app that
    throws away data on purpose, and it earns that: the transcript accumulates
    quoted fragments of other people's emails and interviews, so being able to
    empty it in one click is part of what makes the feature defensible.
    """
    db.query(models.ChatMessage).delete()
    db.commit()
    return RedirectResponse(url="/insights", status_code=303)


def _analytics_apps(db: Session) -> List[dict]:
    """Every application flattened into the plain dicts `analytics.py` wants.

    Same division of labour as the other three adapters in this file: the ORM
    walking happens here and the arithmetic stays in a module that can be
    tested against literals.
    """
    apps = (
        db.query(models.JobApplication)
        .options(
            selectinload(models.JobApplication.stage_history),
            selectinload(models.JobApplication.company),
            selectinload(models.JobApplication.resume),
        )
        .all()
    )
    return [
        {
            "id": a.id,
            "company": a.company.name if a.company else None,
            "title": a.title,
            "stage": a.stage.value if a.stage else None,
            "source": a.source.value if a.source else None,
            # Flattened on the way out, like everywhere else -- analytics.py
            # subtracts these from `_utcnow`-stamped history rows constantly.
            "applied_date": _naive_utc(a.applied_date),
            "created_at": _naive_utc(a.created_at),
            "lost_category": a.lost_category.value if a.lost_category else None,
            "lost_reason": a.lost_reason,
            "seniority": a.seniority.value if a.seniority else None,
            "speciality": a.speciality.value if a.speciality else None,
            "funding_stage": (a.company.funding_stage.value
                              if a.company and a.company.funding_stage else None),
            "employee_band": (a.company.employee_band.value
                              if a.company and a.company.employee_band else None),
            # Carried so the view can be filtered or compared by resume --
            # "does v3 actually get further than v2" is one of the few
            # questions this dataset can answer that the board cannot.
            "resume_label": a.resume.label if a.resume else None,
            "stage_history": [
                {
                    "from_stage": h.from_stage.value if h.from_stage else None,
                    "to_stage": h.to_stage.value if h.to_stage else None,
                    "changed_at": _naive_utc(h.changed_at),
                }
                for h in a.stage_history
            ],
        }
        for a in apps
    ]


@router.get("/analytics")
def analytics_redirect():
    """Merged into /insights. Redirect rather than delete -- see chat_redirect."""
    return RedirectResponse(url="/insights", status_code=301)


def _vocabulary(apps: List[dict]) -> dict:
    """The values that actually exist, for validating a proposed filter.

    Built from the data rather than from the enums, and the difference matters.
    `LostCategory` has nine members but a pipeline might only have used two, and
    a filter on a category no record carries returns an empty cohort -- which
    renders as "not enough data", indistinguishable from a real finding.
    Validating against what is present turns that silent emptiness into a
    visible rejection.
    """
    def seen(key):
        return sorted({str(a.get(key)) for a in apps if a.get(key)})
    return {
        "source": seen("source"),
        "stage": seen("stage"),
        "lost_category": seen("lost_category"),
        "seniority": seen("seniority"),
        "speciality": seen("speciality"),
        "funding_stage": seen("funding_stage"),
        "employee_band": seen("employee_band"),
        "resume": seen("resume_label"),
        "company": seen("company"),
    }


def _insights_context(db: Session, params: dict) -> dict:
    """Everything the merged page renders, filtered by the query string.

    The filter is read from the URL on every request rather than held in a
    session, so a chat-driven view is a link: shareable, bookmarkable, and
    undoable with the back button. Undoing the chat never requires the chat.
    """
    apps = _analytics_apps(db)
    spec, rejected = viewspec.from_query(params, vocabulary=_vocabulary(apps))
    # Rejections raised while parsing the model's proposal travel here in the
    # query string, because they happened on the POST and have to survive the
    # redirect. Shown alongside any raised by the URL itself.
    carried = (params.get("rejected") or "").strip()
    if carried:
        rejected = [carried] + rejected
    visible = viewspec.apply(apps, spec)

    payload = analytics_model.overview(visible, STAGE_ORDER_VALUES)
    comparison = []
    for name, group in viewspec.cohorts(visible, spec.get("compare_by")):
        summary = analytics_model.overview(group, STAGE_ORDER_VALUES)
        comparison.append({
            "name": name,
            "total": summary["total"],
            "intervals": summary["intervals"],
            "funnel": summary["funnel"],
        })

    query = viewspec.to_query(spec)
    # Each chip carries the link that removes it, built here rather than in the
    # template: "the URL minus this one parameter" is a computation, and Jinja
    # is the wrong place to do computations you want to be able to test.
    chips = []
    for chip in viewspec.describe(spec):
        remaining = {k: v for k, v in query.items() if k != chip["param"]}
        chip["href"] = "/insights" + ("?" + urlencode(remaining) if remaining else "")
        chips.append(chip)

    return {
        "active": "insights",
        "stage_order": STAGE_ORDER_VALUES,
        "chips": chips,
        "query_string": urlencode(query),
        "rejected": rejected,
        "filtered": bool(spec),
        "compare_by": spec.get("compare_by"),
        "comparison": comparison,
        # The unfiltered count, so the chip row can say "6 of 11" rather than
        # leaving a suddenly-small pipeline looking like data loss.
        "unfiltered_total": len(apps),
        "query": query,
        "messages": _chat_history(db),
        "chat_enabled": llm.enabled(),
        "model_name": llm.model_name(),
        **payload,
    }


@router.get("/insights")
def insights_page(request: Request, error: str = "", db: Session = Depends(get_db)):
    context = _insights_context(db, dict(request.query_params))
    context["error"] = error
    return templates.TemplateResponse(request, "insights.html", context)


@router.post("/ui/insights/ask")
def insights_ask(request: Request, question: str = Form(""),
                 db: Session = Depends(get_db)):
    question = (question or "").strip()
    current = {k: v for k, v in request.query_params.items()}
    back = "/insights" + ("?" + urlencode(current) if current else "")
    if not question:
        return RedirectResponse(url=back, status_code=303)

    apps = _analytics_apps(db)
    vocabulary = _vocabulary(apps)
    active, _ = viewspec.from_query(current, vocabulary=vocabulary)
    chips = viewspec.describe(active)

    history = [{"role": m.role, "content": m.content} for m in _chat_history(db)]

    # Stored before the call, so a timeout does not also cost the question.
    db.add(models.ChatMessage(role="user", content=question))
    db.commit()

    usage: dict = {}
    try:
        text, model_used = llm.generate(
            chat_model.build_system_blocks(
                # The analytics handed to the model are the *unfiltered*
                # pipeline, matching the cached corpus. The filter currently on
                # screen travels on the question turn instead -- see
                # chat._analytics_block for why the two are separated.
                _chat_corpus(db, analytics_model.overview(apps, STAGE_ORDER_VALUES))),
            chat_model.build_messages(
                history, question, max_turns=CHAT_HISTORY_TURNS,
                view_note="; ".join(c["label"] for c in chips) or "everything"),
            max_tokens=CHAT_MAX_TOKENS,
            usage_out=usage,
        )
    except llm.LLMError as exc:
        sep = "&" if current else "?"
        return RedirectResponse(
            url="{}{}error={}".format(back, sep, quote(str(exc))), status_code=303)

    prose, block = viewspec.extract_block(text)
    proposed, rejected = viewspec.parse(block, vocabulary=vocabulary)

    db.add(models.ChatMessage(
        role="assistant",
        content=prose or text,
        model=model_used,
        usage=json.dumps(usage) if usage else None,
        # What the answer did to the view, kept beside the answer that did it.
        # Without this the transcript reads as a series of remarks with no
        # record of which one changed what you are looking at.
        view_spec=json.dumps(viewspec.to_query(proposed)) if proposed else None,
    ))
    db.commit()

    # A proposed view replaces the current one rather than merging into it.
    # Merging looks helpful and is not: two questions in a row would silently
    # intersect into a cohort nobody asked for, and the only way back would be
    # to notice and undo it by hand.
    destination = "/insights"
    query = viewspec.to_query(proposed) if proposed else {}
    if rejected:
        query["rejected"] = " ".join(rejected)
    if query:
        destination += "?" + urlencode(query)
    return RedirectResponse(url=destination, status_code=303)


@router.post("/ui/insights/reset")
def insights_reset():
    """Drop every filter. One click, no question asked of the model."""
    return RedirectResponse(url="/insights", status_code=303)


# --------------------------------------------------------------------------- #
# What I'm looking for: standing criteria, and fit against them
# --------------------------------------------------------------------------- #
def _looking_for(db: Session) -> models.LookingFor:
    """The singleton row, created on first read.

    Created lazily rather than seeded at startup, so a fresh database has no
    row until someone opens the page -- and an empty statement stays
    distinguishable from one that was never written.
    """
    row = db.get(models.LookingFor, 1)
    if row is None:
        row = models.LookingFor(id=1)
        db.add(row)
        # Seed Gabe's six axes on the very first visit, and only then. The
        # singleton's existence is the marker, so deleting a criterion later
        # never resurrects it -- a starter list that grows back is worse than
        # no starter list, because you cannot tell it to stop.
        for order, (name, blurb) in enumerate(fit.STARTER_CRITERIA):
            db.add(models.Criterion(name=name, description=blurb,
                                    sort_order=order))
        db.commit()
        db.refresh(row)
    return row


def _criteria(db: Session) -> List[models.Criterion]:
    return (
        db.query(models.Criterion)
        .order_by(models.Criterion.sort_order, models.Criterion.id)
        .all()
    )


def _fit_rows(app_obj, criteria) -> List[dict]:
    """One row per criterion for this application, rated or not.

    Every criterion appears even with no rating, because the blanks are the
    point: they are what tells you the score is over two axes out of seven
    rather than that the opportunity is thin.
    """
    by_id = {r.criterion_id: r for r in app_obj.criterion_ratings}
    rows = []
    for crit in criteria:
        rating = by_id.get(crit.id)
        rows.append({
            "criterion": crit,
            "name": crit.name,
            "score": rating.score if rating else None,
            "note": rating.note if rating else None,
        })
    return rows


def _fit_for(app_obj, criteria, threshold) -> dict:
    return fit.score(_fit_rows(app_obj, criteria), threshold=threshold)


@router.post("/ui/looking-for")
def update_looking_for_ui(statement: str = Form(""), dq_threshold: str = Form(""),
                          db: Session = Depends(get_db)):
    row = _looking_for(db)
    row.statement = statement.strip() or None
    parsed = fit.clamp_score(dq_threshold)
    # An out-of-scale threshold leaves the stored one alone rather than
    # snapping to a bound. Silently rewriting it would change which
    # applications are disqualified without anyone asking for that.
    if parsed is not None:
        row.dq_threshold = parsed
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)


@router.post("/ui/looking-for/criteria")
def create_criterion_ui(name: str = Form(...), description: str = Form(""),
                        db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/settings#looking-for", status_code=303)
    highest = db.query(models.Criterion).count()
    db.add(models.Criterion(name=name, description=description.strip() or None,
                            sort_order=highest))
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)


@router.post("/ui/looking-for/criteria/{criterion_id}/edit")
def update_criterion_ui(criterion_id: int, name: str = Form(...),
                        description: str = Form(""),
                        db: Session = Depends(get_db)):
    crit = _get_or_404(db, models.Criterion, criterion_id)
    if name.strip():
        crit.name = name.strip()
    crit.description = description.strip() or None
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)


@router.post("/ui/looking-for/criteria/{criterion_id}/delete")
def delete_criterion_ui(criterion_id: int, db: Session = Depends(get_db)):
    """Deleting a criterion takes its ratings with it.

    Cascade rather than orphan, because a rating means nothing without the
    axis it was made against -- and leaving them would let a deleted criterion
    go on affecting an average nobody can see the source of.
    """
    crit = _get_or_404(db, models.Criterion, criterion_id)
    db.delete(crit)
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)


@router.post("/ui/applications/{application_id}/fit")
async def update_fit_ui(application_id: int, request: Request,
                        db: Session = Depends(get_db)):
    """Save this application's ratings against every criterion.

    Async, and the only async route in this file, because the field names are
    per-criterion (`crit_7`) and cannot be declared as `Form(...)` parameters
    -- reading them needs the raw form, which is awaitable.

    Its own form rather than part of the main edit post: rating is a different
    activity from correcting a date, and folding it in would mean a stray
    submit while editing notes could clear ratings that were not on screen.
    """
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    form = await request.form()
    existing = {r.criterion_id: r for r in app_obj.criterion_ratings}

    for crit in _criteria(db):
        raw = form.get("crit_{}".format(crit.id))
        note = (form.get("note_{}".format(crit.id)) or "").strip() or None
        value = fit.clamp_score(raw)
        row = existing.get(crit.id)
        if value is None and note is None:
            # Nothing on either side: drop the row rather than storing an empty
            # one, so "never rated" and "rated then cleared" look the same in
            # the database as they do on the page.
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            row = models.CriterionRating(criterion_id=crit.id,
                                         application_id=app_obj.id)
            db.add(row)
        row.score = value
        row.note = note

    db.commit()
    return RedirectResponse(
        url="/applications/{}/edit".format(application_id), status_code=303)


# --------------------------------------------------------------------------- #
# Log: say what happened, review what it proposes, apply what you approve
# --------------------------------------------------------------------------- #
LOST_CATEGORY_VALUES = [c.value for c in models.LostCategory]

# How many recent notes the page lists. Enough to answer "did I already log
# this call?", which is the question the history exists for, and short enough
# that the page stays a place you dictate into rather than a place you read.
LOG_HISTORY = 12


def _log_applications(db: Session) -> List[dict]:
    """The applications a note is allowed to touch, as plain dicts.

    Open ones only. A note is about live work, and including forty closed
    records would make the model's job harder for no gain -- more candidates to
    confuse "Condor" with, and a longer packet to pay for. Reopening something
    closed is a hand edit, which is the right weight for that decision.
    """
    apps = (
        db.query(models.JobApplication)
        .options(selectinload(models.JobApplication.company),
                 selectinload(models.JobApplication.people))
        .filter(models.JobApplication.stage.notin_(
            [models.Stage.CLOSED_WON, models.Stage.CLOSED_LOST]))
        .order_by(models.JobApplication.last_activity_date.desc())
        .all()
    )
    rows = []
    for app_obj in apps:
        row = {
            "id": app_obj.id,
            "company": app_obj.company.name if app_obj.company else None,
            "title": app_obj.title,
            "stage": app_obj.stage.value if app_obj.stage else None,
            # People ride along because a note names humans far more often than
            # it names companies -- "Todd said" has to resolve to something,
            # and without this the model has only the company name to go on.
            "people": [p.name for p in app_obj.people if p.name],
        }
        for field in logspec.TEXT_FIELDS:
            row[field] = getattr(app_obj, field, None)
        for field in logspec.DATE_FIELDS:
            value = _naive_utc(getattr(app_obj, field, None))
            row[field] = value.date().isoformat() if value else None
        rows.append(row)
    return rows


# What a log entry's status means. Four states rather than two, because
# "pending" was doing the work of three and the difference is what you are
# supposed to do next:
#
#   pending   -- read fine, changes are waiting for you to tick boxes.
#   failed    -- never read at all. Needs a retry, not a review.
#   nothing   -- read fine, and there was nothing in it to record.
#   applied / discarded -- resolved.
#
# Collapsing the middle two into "pending" produced a loop: a note that could
# not be read appeared under "Waiting on you", whose only button took you to a
# review screen with nothing on it to act on. A status that cannot tell "you
# have work to do" from "the API was down" sends you to the wrong screen every
# time -- the same collapse-two-causes-into-one bug as the forecast panel's
# "no rated threads" message.
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
STATUS_NOTHING = "nothing"


def _read_into(db: Session, entry: models.LogEntry) -> models.LogEntry:
    """Read `entry.text` and record what it proposes. Writes nothing to the record.

    Separated from creation so a retry re-reads the note already stored rather
    than making a second entry. Re-dictating a note you already said is the one
    thing a capture tool must never ask for, and before this existed it was the
    only way to recover from a failed call.

    Every field the previous attempt wrote is cleared first, so a retry that
    succeeds does not leave last time's error sitting under this time's answer.
    """
    entry.prose = None
    entry.proposal = None
    entry.unmatched = None
    entry.rejected = None
    entry.model = None
    entry.usage = None
    entry.application_id = None

    def failed(message: str) -> models.LogEntry:
        entry.status = STATUS_FAILED
        entry.rejected = json.dumps([message])
        db.commit()
        return entry

    if not llm.enabled():
        return failed("Reading notes is off — no ANTHROPIC_API_KEY is set. "
                      "The note was kept.")

    apps = _log_applications(db)
    if not apps:
        return failed("There are no open applications for a note to update. "
                      "The note was kept.")

    # One `today` for the whole read, used for both the prompt and the sanity
    # check, so a note submitted across midnight cannot be told one date and
    # validated against another.
    today = datetime.now(timezone.utc).date()

    usage: dict = {}
    try:
        text, model_used = llm.generate(
            logspec.system_prompt(stages=STAGE_ORDER_VALUES,
                                  categories=LOST_CATEGORY_VALUES,
                                  today=today.isoformat(),
                                  definitions=_definition_overrides(db)),
            logspec.build_messages(logspec.build_packet(apps, entry.text)),
            max_tokens=logspec.MAX_TOKENS,
            timeout=90,
            usage_out=usage,
        )
    except llm.LLMError as exc:
        return failed(str(exc))
    except Exception as exc:  # noqa: BLE001 -- the note must survive anything
        return failed("Reading the note failed unexpectedly: {}".format(exc))

    prose, block = logspec.extract_block(text)
    changes, unmatched, rejected = logspec.parse(
        block, applications=apps, stages=STAGE_ORDER_VALUES,
        categories=LOST_CATEGORY_VALUES, today=today)

    entry.prose = prose or None
    entry.proposal = json.dumps(changes)
    entry.unmatched = json.dumps(unmatched) if unmatched else None
    entry.rejected = json.dumps(rejected) if rejected else None
    entry.model = model_used
    entry.usage = json.dumps(usage) if usage else None
    # A read that produced nothing to apply is finished, not waiting. Leaving
    # it pending parked chatter under "Waiting on you" forever.
    entry.status = STATUS_PENDING if changes else STATUS_NOTHING
    # The convenience link, set only when the whole note is about one record.
    # See LogEntry's docstring for why this is not the authoritative one.
    touched = {c["application_id"] for c in changes}
    entry.application_id = touched.pop() if len(touched) == 1 else None
    db.commit()
    return entry


def _propose_from_note(db: Session, note: str, *, origin: str
                       ) -> models.LogEntry:
    """Store a note, then read it. Always returns an entry, even on failure.

    The note is written before the call on purpose: a note that could not be
    read is still a note you said, and losing it because the API was down would
    be the worst possible failure for a capture tool.
    """
    entry = models.LogEntry(text=note, origin=origin, status=STATUS_FAILED)
    db.add(entry)
    db.flush()
    return _read_into(db, entry)


def _apply_changes(db: Session, entry: models.LogEntry,
                   approved_keys: set) -> List[dict]:
    """Write the approved changes. The only function in this feature that writes.

    Kept to one function on purpose. It is the seam that makes this portable:
    swapping the target from these ORM objects to a Salesforce or HubSpot API
    is a rewrite of this body and nothing else, because every other part of the
    feature deals in plain dicts.

    Re-reads the current value at write time rather than trusting the `current`
    captured when the proposal was made. A pending note can sit for a day while
    you edit the record by hand, and applying a stale append would silently
    drop whatever you typed in between.
    """
    proposed = json.loads(entry.proposal or "[]")
    written = []
    for change in proposed:
        if change["key"] not in approved_keys:
            continue
        app_obj = db.get(models.JobApplication, change["application_id"])
        if app_obj is None:
            continue          # deleted between proposing and approving

        field, value, mode = change["field"], change["value"], change["mode"]
        if field == "stage":
            # Assigning stage fires the StageHistory listener, so a stage moved
            # by voice lands in the funnel history identically to one dragged
            # on the board. Nothing here needs to know that; it is why stage is
            # set through the attribute rather than through an UPDATE.
            app_obj.stage = models.Stage(value)
        elif field == "lost_category":
            app_obj.lost_category = models.LostCategory(value)
        elif field in logspec.DATE_FIELDS:
            # Assigning this fires the history listener exactly as a hand edit
            # does, so a date moved by voice is logged like any other.
            setattr(app_obj, field, datetime.strptime(value, "%Y-%m-%d"))
        else:
            setattr(app_obj, field,
                    logspec.merged_value(getattr(app_obj, field), value, mode))

        # A voice update is activity. Without this a dictated note leaves the
        # card looking untouched for as long as the board's age counter says.
        app_obj.last_activity_date = datetime.now(timezone.utc)
        written.append(change)

    entry.applied = json.dumps(written)
    entry.status = "applied" if written else "discarded"
    entry.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return written


def _log_context(db: Session, entry: Optional[models.LogEntry] = None,
                 *, error: str = "", applied: str = "") -> dict:
    recent = (
        db.query(models.LogEntry)
        .order_by(models.LogEntry.created_at.desc(), models.LogEntry.id.desc())
        .limit(LOG_HISTORY + 1)
        .all()
    )
    # The note under review is quoted back above; listing it again three inches
    # lower reads as a duplicate rather than as history.
    recent = [r for r in recent if entry is None or r.id != entry.id][:LOG_HISTORY]
    pending = (
        db.query(models.LogEntry)
        .filter(models.LogEntry.status == STATUS_PENDING)
        .order_by(models.LogEntry.created_at.desc())
        .all()
    )
    unread = (
        db.query(models.LogEntry)
        .filter(models.LogEntry.status == STATUS_FAILED)
        .order_by(models.LogEntry.created_at.desc())
        .all()
    )
    return {
        "active": "log",
        "entry": entry,
        "changes": json.loads(entry.proposal or "[]") if entry else [],
        "unmatched": json.loads(entry.unmatched or "[]") if entry else [],
        "rejected": json.loads(entry.rejected or "[]") if entry else [],
        "recent": recent,
        # Anything queued by the API and not yet reviewed. Surfaced at the top
        # of the page rather than in the history, because a pending change is
        # work waiting on you and history is not.
        "pending": [p for p in pending if entry is None or p.id != entry.id],
        # Notes that were never read. Kept separate from `pending` because the
        # action is different: these need a retry, not a decision.
        "unread": [u for u in unread if entry is None or u.id != entry.id],
        "failed": bool(entry) and entry.status == STATUS_FAILED,
        "enabled": llm.enabled(),
        "error": error,
        "applied": applied,
    }


@router.get("/log")
def log_page(request: Request, error: str = "", applied: str = "",
             entry_id: int = 0, db: Session = Depends(get_db)):
    entry = db.get(models.LogEntry, entry_id) if entry_id else None
    return templates.TemplateResponse(
        request, "log.html", _log_context(db, entry, error=error, applied=applied))


@router.post("/ui/log")
def log_submit(note: str = Form(""), db: Session = Depends(get_db)):
    """Read a note and show what it proposes. Still writes nothing."""
    note = (note or "").strip()
    if not note:
        return RedirectResponse(url="/log", status_code=303)
    entry = _propose_from_note(db, note, origin="web")
    return RedirectResponse(
        url="/log?entry_id={}".format(entry.id), status_code=303)


@router.post("/ui/log/{entry_id}/apply")
async def log_apply(entry_id: int, request: Request,
                    db: Session = Depends(get_db)):
    """Apply exactly the changes whose boxes are ticked, and nothing else.

    `async` for the same reason as the fit form: the checkbox names are built
    from ids at render time, so they cannot be declared as parameters and the
    raw form has to be read.
    """
    entry = _get_or_404(db, models.LogEntry, entry_id)
    form = await request.form()
    approved = {v for k, v in form.multi_items() if k == "approve"}
    written = _apply_changes(db, entry, approved)
    return RedirectResponse(
        url="/log?applied={}".format(quote(logspec.summarise(written))),
        status_code=303)


@router.post("/ui/log/{entry_id}/discard")
def log_discard(entry_id: int, db: Session = Depends(get_db)):
    """Keep the note, apply none of it.

    Discarding writes `applied` as an empty list rather than leaving it NULL,
    so "reviewed and rejected everything" stays distinguishable from "never
    reviewed" -- the same distinction the champion field and the rating
    provenance both exist to preserve.
    """
    entry = _get_or_404(db, models.LogEntry, entry_id)
    _apply_changes(db, entry, set())
    return RedirectResponse(url="/log", status_code=303)


@router.post("/ui/log/{entry_id}/retry")
def log_retry(entry_id: int, db: Session = Depends(get_db)):
    """Read a stored note again, in place.

    Re-reads the same entry rather than creating a second one, so retrying a
    note does not litter the history with duplicates of something you said
    once — and so the double-entry check the history exists for keeps working.
    """
    entry = _get_or_404(db, models.LogEntry, entry_id)
    _read_into(db, entry)
    return RedirectResponse(
        url="/log?entry_id={}".format(entry.id), status_code=303)


@router.post("/ui/log/{entry_id}/delete")
def log_delete(entry_id: int, db: Session = Depends(get_db)):
    entry = _get_or_404(db, models.LogEntry, entry_id)
    db.delete(entry)
    db.commit()
    return RedirectResponse(url="/log", status_code=303)



# --------------------------------------------------------------------------- #
# Settings: what the fields mean, and what you are looking for
# --------------------------------------------------------------------------- #
def _definition_overrides(db: Session) -> dict:
    """Your edited definitions, keyed by field. Absent fields use the default."""
    return {
        row.field: row.definition
        for row in db.query(models.FieldDefinition).all()
        if (row.definition or "").strip()
    }


def _definition_rows(db: Session) -> List[dict]:
    return fields_model.rows(overrides=_definition_overrides(db),
                             writable=logspec.WRITABLE)


def _settings_context(db: Session, **extra) -> dict:
    """Everything both halves of the page need.

    The Looking For half is unchanged and still reads through `_looking_for`
    and `_criteria`; folding the page in was a template move, not a rewrite,
    which is why its routes keep their `/ui/looking-for/...` paths. Renaming
    working form endpoints to match a nav change would be churn with a
    regression attached.
    """
    row = _looking_for(db)
    criteria = _criteria(db)
    threshold = fit.threshold_of(row.dq_threshold)
    apps = (
        db.query(models.JobApplication)
        .options(selectinload(models.JobApplication.criterion_ratings),
                 selectinload(models.JobApplication.company))
        .all()
    )
    context = {
        "active": "settings",
        "definition_rows": _definition_rows(db),
        "definition_groups": fields_model.groups(),
        "writers": fields_model.WRITERS,
        "looking_for": row,
        "criteria": criteria,
        "threshold": threshold,
        "scale_min": fit.SCALE_MIN,
        "scale_max": fit.SCALE_MAX,
        "ranked": fit.rank([
            {"id": a.id,
             "company": a.company.name if a.company else None,
             "title": a.title,
             "stage": a.stage.value if a.stage else None,
             "ratings": _fit_rows(a, criteria)}
            for a in apps
        ], threshold=threshold),
        "saved": "",
    }
    context.update(extra)
    return context


@router.get("/settings")
def settings_page(request: Request, saved: str = "",
                  db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "settings.html", _settings_context(db, saved=saved))


@router.get("/looking-for")
def looking_for_redirect():
    """Folded into Settings. Redirect rather than delete: this URL was in the
    nav for weeks and is in his history."""
    return RedirectResponse(url="/settings#looking-for", status_code=301)


@router.post("/ui/settings/definitions/{field}")
def update_definition_ui(field: str, definition: str = Form(""),
                         db: Session = Depends(get_db)):
    """Save your wording for one field, or clear it back to the default.

    A blank submission deletes the override rather than storing an empty
    string. An empty definition is worse than the shipped one -- it would
    hand the model a bare field name and this page a blank row -- so the only
    thing "empty" can sensibly mean here is "use the default".
    """
    if field not in fields_model.BY_FIELD:
        raise HTTPException(404, "no such field")
    row = (db.query(models.FieldDefinition)
           .filter(models.FieldDefinition.field == field).first())
    text = (definition or "").strip()
    default = fields_model.default_definition(field)
    if not text or text == default:
        # Matching the default exactly is also a reset. Storing it would be a
        # row that pins today's wording and silently stops tracking a better
        # one shipped later.
        if row is not None:
            db.delete(row)
        db.commit()
        return RedirectResponse(
            url="/settings?saved={}#definitions".format(quote(field)),
            status_code=303)
    if row is None:
        row = models.FieldDefinition(field=field)
        db.add(row)
    row.definition = text
    db.commit()
    return RedirectResponse(
        url="/settings?saved={}#definitions".format(quote(field)),
        status_code=303)


@router.post("/ui/settings/definitions/{field}/reset")
def reset_definition_ui(field: str, db: Session = Depends(get_db)):
    if field not in fields_model.BY_FIELD:
        raise HTTPException(404, "no such field")
    row = (db.query(models.FieldDefinition)
           .filter(models.FieldDefinition.field == field).first())
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse(
        url="/settings?saved={}#definitions".format(quote(field)),
        status_code=303)
