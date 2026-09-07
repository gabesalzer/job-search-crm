"""Resume versions.

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
