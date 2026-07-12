"""Server-rendered UI (Jinja2).

A thin presentation layer over the exact same models and database the JSON API
uses. Form posts here just create/update rows and redirect back to the page;
the drag-to-change-stage on the board calls the JSON API directly.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db

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


def _find_or_create_company(name: str, db: Session) -> models.Company:
    """Look up a company by name (case-insensitive), creating it if missing.

    This is what makes the flow posting-first: you add a posting and the
    company (Account) is created automatically if it doesn't exist yet — no
    need to set up the company beforehand.
    """
    name = (name or "").strip() or "Unknown company"
    existing = (
        db.query(models.Company)
        .filter(models.Company.name.ilike(name))
        .first()
    )
    if existing:
        return existing
    company = models.Company(name=name, company_type=models.CompanyType.EMPLOYER)
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
    company = _find_or_create_company(company_name, db)
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
