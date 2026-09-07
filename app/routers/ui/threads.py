"""Email threads, and the automatic read that scores them.

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
