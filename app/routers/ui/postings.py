"""Job postings: the triage and rating loop.

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
