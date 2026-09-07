"""The Log: say what happened, review what it proposes, apply what you approve.

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
    entry.questions = None
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
            logspec.build_messages(
                logspec.build_packet(apps, entry.text, entry.answers)),
            max_tokens=logspec.MAX_TOKENS,
            timeout=90,
            usage_out=usage,
        )
    except llm.LLMError as exc:
        return failed(str(exc))
    except Exception as exc:  # noqa: BLE001 -- the note must survive anything
        return failed("Reading the note failed unexpectedly: {}".format(exc))

    prose, block = logspec.extract_block(text)
    changes, unmatched, questions, rejected = logspec.parse(
        block, applications=apps, stages=STAGE_ORDER_VALUES,
        categories=LOST_CATEGORY_VALUES, today=today)

    entry.prose = prose or None
    entry.proposal = json.dumps(changes)
    entry.unmatched = json.dumps(unmatched) if unmatched else None
    entry.questions = json.dumps(questions) if questions else None
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

def _apply_changes(db: Session, entry: models.LogEntry, approved_keys: set,
                   edits: Optional[dict] = None) -> List[dict]:
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
    edits = edits or {}
    today = datetime.now(timezone.utc).date()
    written = []
    refused = []
    for change in proposed:
        if change["key"] not in approved_keys:
            continue
        app_obj = db.get(models.JobApplication, change["application_id"])
        if app_obj is None:
            continue          # deleted between proposing and approving

        field, mode = change["field"], change["mode"]
        value = change["value"]

        # A value you corrected goes through exactly the checks the model's had
        # to pass. The edit box is the screen a person trusts most, which is
        # precisely why it must not be the one place a bad value gets in.
        edited = False
        raw_edit = edits.get(change["key"])
        if raw_edit is not None and str(raw_edit).strip() != str(value).strip():
            coerced, reason = logspec.coerce_value(
                field, raw_edit, stages=STAGE_ORDER_VALUES,
                categories=LOST_CATEGORY_VALUES, today=today)
            if reason:
                refused.append("{}: {}".format(
                    change.get("company") or change["application_id"], reason))
                continue
            value, edited = coerced, True
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
        # Record what was actually written *and* whether you rewrote it.
        # Accepted-verbatim, edited-then-accepted and rejected are three
        # different verdicts on the model, and collapsing them into a binary
        # throws away the most useful half of the signal -- a proposal you
        # keep having to correct is a different problem from one you keep
        # throwing away, and they have different fixes.
        record = dict(change)
        record["value"] = value
        record["edited"] = edited
        if edited:
            record["proposed_value"] = change["value"]
        written.append(record)

    entry.applied = json.dumps(written)
    entry.status = "applied" if written else "discarded"
    entry.resolved_at = datetime.now(timezone.utc)
    if refused:
        entry.rejected = json.dumps(
            json.loads(entry.rejected or "[]") + refused)
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
        "questions": json.loads(entry.questions or "[]") if entry else [],
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
        "stages": STAGE_ORDER_VALUES,
        "lost_categories": LOST_CATEGORY_VALUES,
        "date_fields": logspec.DATE_FIELDS,
        "picklist_fields": logspec.PICKLIST_FIELDS,
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
    # `value_<key>` carries whatever is in the box, edited or not. Comparing
    # against the proposal is what decides whether it counts as an edit, so an
    # untouched box costs nothing.
    edits = {k[len("value_"):]: v for k, v in form.multi_items()
             if k.startswith("value_")}
    written = _apply_changes(db, entry, approved, edits)
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

@router.post("/ui/log/{entry_id}/answer")
async def log_answer(entry_id: int, request: Request,
                     db: Session = Depends(get_db)):
    """Answer the questions it asked, then read the note again with them.

    The answers are stored beside the note rather than appended to it, so what
    you said stays verbatim, and the re-read sees both. This is the only action
    a question offers: there is no "create the person it asked about" button,
    because the Log's write surface is deliberately existing applications only
    and a question is not a reason to widen it.
    """
    entry = _get_or_404(db, models.LogEntry, entry_id)
    form = await request.form()
    asked = json.loads(entry.questions or "[]")
    pairs = []
    for i, question in enumerate(asked):
        reply = (form.get("answer_{}".format(i)) or "").strip()
        if reply:
            pairs.append("Q: {}\nA: {}".format(question, reply))
    if not pairs:
        return RedirectResponse(
            url="/log?entry_id={}".format(entry.id), status_code=303)
    entry.answers = "\n\n".join(
        ([entry.answers] if entry.answers else []) + pairs)
    db.commit()
    _read_into(db, entry)
    return RedirectResponse(
        url="/log?entry_id={}".format(entry.id), status_code=303)

@router.post("/ui/log/{entry_id}/delete")
def log_delete(entry_id: int, db: Session = Depends(get_db)):
    entry = _get_or_404(db, models.LogEntry, entry_id)
    db.delete(entry)
    db.commit()
    return RedirectResponse(url="/log", status_code=303)
