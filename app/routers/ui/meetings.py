"""Meetings, including Granola import.

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
