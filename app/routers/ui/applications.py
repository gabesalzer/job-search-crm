"""The application record: the board, the edit page, and the numbers on it.

Split out of the former monolithic ui.py; see shared.py for why.
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

from ... import analytics as analytics_model
from ... import brief as brief_model
from ... import chat as chat_model
from ... import classify
from ... import fields as fields_model
from ... import fit
from ... import forecast as forecast_model
from ... import logspec
from ... import models
from ... import thread_read
from ... import viewspec
from ...database import get_db
from ...services import granola, llm, scrape
from ...services.email_parse import parse_gmail_export
from ...services.resume_extract import extract_text
from .shared import *  # noqa: F401,F403 -- shared.__all__ lists the names

router = APIRouter(tags=["ui"], include_in_schema=False)


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
