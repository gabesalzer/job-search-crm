"""Server-rendered UI (Jinja2).

A thin presentation layer over the exact same models and database the JSON API
uses. Form posts here just create/update rows and redirect back to the page;
the drag-to-change-stage on the board calls the JSON API directly.
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import granola
from ..services.resume_extract import extract_text

# templates/ lives next to app/, resolved relative to this file so it works
# regardless of the current working directory.
TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)

STAGE_VALUES = [s.value for s in models.Stage]          # ordered; Closed Lost last
COMPANY_TYPES = [t.value for t in models.CompanyType]


@router.get("/")
def root():
    return RedirectResponse(url="/board")


# --------------------------------------------------------------------------- #
# Pipeline (kanban board)
# --------------------------------------------------------------------------- #
@router.get("/board")
def board(request: Request, db: Session = Depends(get_db)):
    grouped: dict[str, list] = {s: [] for s in STAGE_VALUES}
    for app_obj in db.query(models.JobApplication).all():
        grouped.setdefault(app_obj.stage.value, []).append(app_obj)
    return templates.TemplateResponse(request, "board.html", {
        "active": "board",
        "stages": STAGE_VALUES,
        "grouped": grouped,
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
    stage: str = Form("Saved"),
    resume_id: Optional[str] = Form(None),
    job_posting_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    app_obj = models.JobApplication(
        company_id=company_id,
        title=title or None,
        stage=models.Stage(stage),
        resume_id=int(resume_id) if resume_id else None,
        job_posting_id=int(job_posting_id) if job_posting_id else None,
    )
    db.add(app_obj)
    db.commit()
    return RedirectResponse(url="/board", status_code=303)


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


@router.get("/meetings")
def meetings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "meetings.html", {
        "active": "meetings",
        "meetings": db.query(models.Meeting).order_by(models.Meeting.created_at.desc()).all(),
        "applications": db.query(models.JobApplication).all(),
        "meeting_types": [t.value for t in models.MeetingType],
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
    db: Session = Depends(get_db),
):
    db.add(models.Meeting(
        application_id=application_id,
        title=title or None,
        meeting_type=models.MeetingType(meeting_type) if meeting_type else None,
        meeting_date=_parse_dt(meeting_date),
        summary=summary or None,
        transcript=transcript or None,
        notes=notes or None,
        granola_note_id=granola_note_id or None,
        granola_link=granola_link or None,
    ))
    db.commit()
    return RedirectResponse(url="/meetings", status_code=303)
