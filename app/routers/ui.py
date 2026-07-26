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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import granola
from ..services.email_parse import parse_gmail_export
from ..services.resume_extract import extract_text

# templates/ lives next to app/, resolved relative to this file so it works
# regardless of the current working directory.
TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)

STAGE_VALUES = [s.value for s in models.Stage]          # ordered; Closed Lost last
COMPANY_TYPES = [t.value for t in models.CompanyType]
LOST_REASON_VALUES = [r.value for r in models.LostReason]
PERSON_ROLE_VALUES = [r.value for r in models.PersonRole]


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


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


def _activity_timeline(app_obj: models.JobApplication) -> list[dict]:
    """Merge Meetings and Email Threads into one chronologically-sorted list
    for the Application page. Purely a display-layer merge (no new table,
    no schema change) -- each side keeps its own shape, we just normalize
    both into a common {type, when, title, sub, url} dict and sort by the
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
        })
    for t in app_obj.email_threads:
        rows.append({
            "type": "Email",
            "when": t.last_message_at or t.started_at,
            "title": t.subject or "Untitled thread",
            "sub": t.person.name if t.person else None,
            "url": f"/email-threads/{t.id}/edit",
        })
    rows.sort(key=lambda r: r["when"] or datetime.min, reverse=True)
    return rows


@router.get("/applications/{application_id}/edit")
def edit_application_page(application_id: int, request: Request, db: Session = Depends(get_db)):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    return templates.TemplateResponse(request, "application_edit.html", {
        "active": "board",
        "app_obj": app_obj,
        "stages": STAGE_VALUES,
        "lost_reasons": LOST_REASON_VALUES,
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "resumes": db.query(models.Resume).order_by(models.Resume.label).all(),
        "postings": db.query(models.JobPosting)
        .order_by(models.JobPosting.last_seen_at.desc())
        .all(),
        "activity": _activity_timeline(app_obj),
    })


@router.post("/ui/applications/{application_id}/edit")
def update_application_ui(
    application_id: int,
    company_id: int = Form(...),
    title: str = Form(""),
    stage: str = Form(...),
    resume_id: Optional[str] = Form(None),
    job_posting_id: Optional[str] = Form(None),
    lost_reason: str = Form(""),
    applied_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    app_obj.company_id = company_id
    app_obj.title = title or None
    app_obj.resume_id = int(resume_id) if resume_id else None
    app_obj.job_posting_id = int(job_posting_id) if job_posting_id else None
    app_obj.applied_date = _parse_dt(applied_date)
    app_obj.notes = notes or None

    new_stage = models.Stage(stage)
    if new_stage != app_obj.stage:
        app_obj.stage = new_stage  # triggers the StageHistory event listener
        app_obj.last_activity_date = datetime.now(timezone.utc)
    app_obj.lost_reason = (
        models.LostReason(lost_reason) if new_stage == models.Stage.CLOSED_LOST and lost_reason else None
    )

    db.commit()
    return RedirectResponse(url="/board", status_code=303)


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
def edit_company_page(company_id: int, request: Request, db: Session = Depends(get_db)):
    company = _get_or_404(db, models.Company, company_id)
    return templates.TemplateResponse(request, "company_edit.html", {
        "active": "companies",
        "company": company,
        "company_types": COMPANY_TYPES,
    })


@router.post("/ui/companies/{company_id}/edit")
def update_company_ui(
    company_id: int,
    name: str = Form(...),
    company_type: str = Form("Employer"),
    website: str = Form(""),
    industry: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    company = _get_or_404(db, models.Company, company_id)
    company.name = name
    company.company_type = models.CompanyType(company_type)
    company.website = website or None
    company.industry = industry or None
    company.notes = notes or None
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
    db.delete(person)  # cascades to that person's email threads
    db.commit()
    return RedirectResponse(url="/people", status_code=303)


# --------------------------------------------------------------------------- #
# Email Threads: recruiter/HM email exchanges, pasted in manually for now.
# Master is Person (a thread is always a conversation with someone, and can
# predate any application, e.g. cold outreach); Application is an optional
# lookup set once the thread is actually about a role. See ARCHITECTURE.md.
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


def _resolve_thread_person(
    person_id_form: Optional[str],
    parsed: dict,
    db: Session,
    application_id: Optional[int],
) -> int:
    """Resolve which Person an email thread belongs to. An explicit choice in
    the form always wins. Otherwise, every real sender found in a Gmail-shaped
    upload/paste gets found-or-created by email (dedup key: lowercased email
    address) -- so cc'd/other repliers become known People too, not just the
    one the thread attaches to. The thread itself attaches to the first
    sender found, in message order (usually whoever is driving the thread).
    """
    if person_id_form:
        return int(person_id_form)
    other_senders = parsed.get("other_senders") or []
    if not other_senders:
        raise HTTPException(
            400,
            "Couldn't detect who this thread is with, so a Person is required. "
            "Either pick one from the dropdown, or upload/paste a Gmail-exported "
            "thread (Gmail's \"Print all\") so it can be detected automatically.",
        )
    primary = None
    for sender in other_senders:
        person = _find_or_create_person_by_email(
            sender["email"], db, name=sender["name"], application_id=application_id
        )
        if primary is None:
            primary = person
    return primary.id


@router.post("/ui/email-threads")
def create_email_thread_ui(
    person_id: Optional[str] = Form(None),
    application_id: Optional[str] = Form(None),
    subject: str = Form(""),
    body: str = Form(""),
    participants: str = Form(""),
    started_at: str = Form(""),
    last_message_at: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Create an email thread. Same dual-input pattern as Resume: paste the
    text directly, or upload a file (a PDF export/print of the thread works
    great) and it gets extracted automatically via the same extractor Resume
    uses. An uploaded file wins over pasted text when both are present.

    If the text looks like a Gmail thread export, subject/participants/dates
    are auto-filled from it -- but only into fields you left blank, so
    anything you typed by hand already wins. Person works the same way: leave
    it on "auto-detect" and it's found-or-created by email address; pick one
    explicitly and that always overrides detection.
    """
    body_text = _extract_upload_text(file) or (body or "").strip()
    parsed = parse_gmail_export(body_text)
    app_id = int(application_id) if application_id else None
    resolved_person_id = _resolve_thread_person(person_id, parsed, db, app_id)
    db.add(models.EmailThread(
        person_id=resolved_person_id,
        application_id=app_id,
        subject=(subject or "").strip() or parsed["subject"],
        body=body_text or None,
        participants=(participants or "").strip() or parsed["participants"],
        started_at=_parse_dt(started_at) or parsed["started_at"],
        last_message_at=_parse_dt(last_message_at) or parsed["last_message_at"],
        notes=notes or None,
    ))
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)


@router.get("/email-threads/{thread_id}/edit")
def edit_email_thread_page(thread_id: int, request: Request, db: Session = Depends(get_db)):
    thread = _get_or_404(db, models.EmailThread, thread_id)
    return templates.TemplateResponse(request, "email_thread_edit.html", {
        "active": "emails",
        "thread": thread,
        "people": db.query(models.Person).order_by(models.Person.name).all(),
        "applications": db.query(models.JobApplication).all(),
    })


@router.post("/ui/email-threads/{thread_id}/edit")
def update_email_thread_ui(
    thread_id: int,
    person_id: Optional[str] = Form(None),
    application_id: Optional[str] = Form(None),
    subject: str = Form(""),
    body: str = Form(""),
    participants: str = Form(""),
    started_at: str = Form(""),
    last_message_at: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """Update an email thread. Uploading a file re-extracts and replaces the
    body; otherwise the pasted-text box (pre-filled with the current body) is
    the source of truth, so extraction glitches can be hand-fixed without
    re-uploading — same pattern as editing a Resume. As on create, a
    Gmail-shaped body only fills in fields left blank; it never overwrites
    something already typed in the form -- and Person auto-detection only
    runs off a freshly uploaded file, never off the already-saved body, so
    just re-saving the form can't unexpectedly reassign the thread to someone
    else or spawn a duplicate Person.
    """
    thread = _get_or_404(db, models.EmailThread, thread_id)
    extracted = _extract_upload_text(file)
    body_text = extracted or (body or "").strip()
    parsed = parse_gmail_export(body_text) if extracted else {
        "subject": None, "participants": None, "started_at": None, "last_message_at": None,
        "other_senders": [],
    }
    app_id = int(application_id) if application_id else None
    thread.person_id = _resolve_thread_person(person_id, parsed, db, app_id)
    thread.application_id = app_id
    thread.subject = (subject or "").strip() or parsed["subject"]
    thread.body = body_text or None
    thread.participants = (participants or "").strip() or parsed["participants"]
    thread.started_at = _parse_dt(started_at) or parsed["started_at"]
    thread.last_message_at = _parse_dt(last_message_at) or parsed["last_message_at"]
    thread.notes = notes or None
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)


@router.post("/ui/email-threads/{thread_id}/delete")
def delete_email_thread_ui(thread_id: int, db: Session = Depends(get_db)):
    thread = _get_or_404(db, models.EmailThread, thread_id)
    db.delete(thread)
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)
