"""Server-rendered UI (Jinja2).

A thin presentation layer over the exact same models and database the JSON API
uses. Form posts here just create/update rows and redirect back to the page;
the drag-to-change-stage on the board calls the JSON API directly.
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from .. import forecast as forecast_model
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
APPLICATION_SOURCE_VALUES = [s.value for s in models.ApplicationSource]
FORECAST_VALUES = [f.value for f in models.ForecastCategory]


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


def _apply_meeting_quality(meeting, my_performance: str, employer_engagement: str) -> None:
    """Write the two halves of meeting quality: how well you did, and how
    interested they were.

    Reuses _parse_score because these share its scale and its rules exactly --
    0-100, clamped rather than rejected, blank meaning "no judgment formed"
    rather than zero. That last distinction is the whole reason these aren't
    defaulted: a call where you didn't rate your own performance and a call you
    rated 0 are opposite claims, and the forecast reads them as such.

    Unlike `score` these carry no `*_at` stamp. `scored_at` exists because the
    rollup has to order readings against each other over time; these two are
    read only off the most recent meeting, which is already ordered by
    `meeting_date`. A timestamp nobody reads is a column that can only rot.
    """
    meeting.my_performance = _parse_score(my_performance)
    meeting.employer_engagement = _parse_score(employer_engagement)


def _score_rollup(app_obj: models.JobApplication) -> Optional[dict]:
    """Derive "where does this application stand" from its scored activities.

    Computed at display time from the Meeting/EmailThread rows rather than
    stored on the Application. Nothing to keep in sync, and the rollup can
    never disagree with the rows it summarizes -- rescore a meeting and the
    application's number moves on the next page load, with no backfill.

    Ordering uses the date the activity *happened* -- `meeting_date` for a
    meeting, `last_message_at` (then `started_at`) for a thread -- and falls
    back to `scored_at` only when the activity carries no date of its own.

    This used to sort on `scored_at` first, and that was wrong. `scored_at` is
    stamped by _set_score() the instant a number is saved, and since _set_score
    is the only writer of `score` anywhere in the app, every scored activity
    has one; the `or meeting_date` fallback could never fire. The rollup was
    therefore ordered entirely by data-entry time. Log two meetings in one
    sitting, oldest last, and the older conversation became "latest" -- while
    the activity list rendered directly beneath it on the same page sorted by
    the real dates and visibly disagreed. A score is a judgment *about* an
    event, and it belongs in the sequence where the event sits; when you formed
    it is a fact about your evening, not about the pursuit.

    `stale_days` is how old that latest reading is, and it's there because a
    number with no age on it lies by omission: an 80 from six weeks ago and an
    80 from yesterday are the same digits describing completely different
    situations. That matters most on the board, where a column of them gets
    scanned at once and the confident-looking old one is exactly the card that
    misleads. Following the sort key, it now measures from the event rather
    than from the keystroke -- "nothing has happened here in 21 days" is the
    thing worth acting on. It's None when the latest reading has no usable date
    at all, which is a different claim from "today".

    Returns None when nothing has been scored, so the template can stay quiet
    instead of rendering an empty widget.
    """
    def _sortable(dt: Optional[datetime]) -> datetime:
        # Form-entered dates come back naive; `scored_at` is stamped in UTC and
        # comes back aware. Comparing the two raises, so flatten to naive UTC
        # before sorting rather than letting the mix reach the comparison.
        if dt is None:
            return datetime.min
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    scored: list[tuple[datetime, int]] = []
    for m in app_obj.meetings:
        if m.score is not None:
            scored.append((_sortable(m.meeting_date or m.scored_at), m.score))
    for t in app_obj.email_threads:
        if t.score is not None:
            scored.append((
                _sortable(t.last_message_at or t.started_at or t.scored_at),
                t.score,
            ))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0])
    latest_at, latest = scored[-1]
    previous = scored[-2][1] if len(scored) > 1 else None
    # datetime.min is the sentinel _sortable() returns for an activity with no
    # date on any of its fields. Subtracting from it would report an age in the
    # hundreds of thousands of days, so an undated reading reports no age at all.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_days = (
        None if latest_at == datetime.min else max((now - latest_at).days, 0)
    )
    return {
        "latest": latest,
        "previous": previous,
        "delta": None if previous is None else latest - previous,
        "count": len(scored),
        "stale_days": stale_days,
    }


def _forecast_for(app_obj: models.JobApplication) -> dict:
    """Gather the four Automated Forecast inputs off an Application.

    This is the only function in the app that knows both the ORM and the
    forecast model, and it deliberately does nothing but read fields. All the
    judgment lives in app/forecast.py, which imports no SQLAlchemy and is
    therefore directly exercised by tests rather than mirrored in them.

    Like the score rollup, this runs at display time and stores nothing. A
    forecast column would be a snapshot that quietly rots the moment you rescore
    a meeting or swap the resume, and the failure mode of a stale forecast is
    the worst kind: it looks exactly like a fresh one.
    """
    return forecast_model.automated_forecast(
        stage=app_obj.stage.value if app_obj.stage else None,
        source=app_obj.source.value if app_obj.source else None,
        meetings=[
            {
                "when": m.meeting_date,
                "my_performance": m.my_performance,
                "employer_engagement": m.employer_engagement,
            }
            for m in app_obj.meetings
        ],
        resume_text=app_obj.resume.content if app_obj.resume else None,
        jd_text=app_obj.job_posting.jd_text if app_obj.job_posting else None,
    )


@router.get("/")
def root():
    return RedirectResponse(url="/board")


# --------------------------------------------------------------------------- #
# Pipeline (kanban board)
# --------------------------------------------------------------------------- #
@router.get("/board")
def board(request: Request, db: Session = Depends(get_db)):
    # The score rollup reads every scored meeting and thread on each card, and
    # the forecast additionally reads the resume and the posting. Load all four
    # up front: without this the board issues four extra queries per application
    # just to render the numbers, and that cost grows with the pipeline.
    #
    # Pulling full resume and JD text for a board view looks expensive. It
    # mostly isn't -- selectinload issues one query per relationship for the
    # whole page, not per card, and resumes are shared across applications so
    # the distinct set is small. The fit index is recomputed per card on every
    # load rather than cached, which is the same trade the score rollup makes:
    # a derived number that can never be stale beats one that's cheap to read
    # and quietly wrong.
    apps = (
        db.query(models.JobApplication)
        .options(
            selectinload(models.JobApplication.meetings),
            selectinload(models.JobApplication.email_threads),
            selectinload(models.JobApplication.resume),
            selectinload(models.JobApplication.job_posting),
        )
        .all()
    )
    grouped: dict[str, list] = {s: [] for s in STAGE_VALUES}
    for app_obj in apps:
        grouped.setdefault(app_obj.stage.value, []).append(app_obj)
    return templates.TemplateResponse(request, "board.html", {
        "active": "board",
        "stages": STAGE_VALUES,
        "grouped": grouped,
        # Same derivation the Application page uses, keyed by id so a card can
        # look up its own without the template calling into Python.
        "rollups": {a.id: _score_rollup(a) for a in apps},
        # Automated Forecast, same shape and same keying. The card shows only
        # the category; the reasoning behind it lives on the edit page, because
        # a board is for scanning and a four-part breakdown on every card would
        # bury the one thing you came here to see.
        "forecasts": {a.id: _forecast_for(a) for a in apps},
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
def edit_application_page(application_id: int, request: Request, db: Session = Depends(get_db)):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    return templates.TemplateResponse(request, "application_edit.html", {
        "active": "board",
        "app_obj": app_obj,
        "stages": STAGE_VALUES,
        "lost_reasons": LOST_REASON_VALUES,
        "sources": APPLICATION_SOURCE_VALUES,
        "companies": db.query(models.Company).order_by(models.Company.name).all(),
        "resumes": db.query(models.Resume).order_by(models.Resume.label).all(),
        "postings": db.query(models.JobPosting)
        .order_by(models.JobPosting.last_seen_at.desc())
        .all(),
        "activity": _activity_timeline(app_obj),
        "score_rollup": _score_rollup(app_obj),
        # The two forecasts, side by side and deliberately independent. The
        # automated one is derived here and stored nowhere; the manual one is a
        # column only you write. Where they disagree is the interesting part, so
        # neither is allowed to overwrite or defer to the other.
        "forecast": _forecast_for(app_obj),
        "forecast_values": FORECAST_VALUES,
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
    created_at: str = Form(""),
    last_activity_date: str = Form(""),
    updated_at: str = Form(""),
    notes: str = Form(""),
    context: str = Form(""),
    source: str = Form(""),
    manual_forecast: str = Form(""),
    db: Session = Depends(get_db),
):
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    app_obj.company_id = company_id
    app_obj.title = title or None
    app_obj.resume_id = int(resume_id) if resume_id else None
    app_obj.job_posting_id = int(job_posting_id) if job_posting_id else None
    app_obj.applied_date = _parse_dt(applied_date)
    app_obj.notes = notes or None
    app_obj.context = context or None
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

    new_stage = models.Stage(stage)
    if new_stage != app_obj.stage:
        app_obj.stage = new_stage  # triggers the StageHistory event listener
        app_obj.last_activity_date = datetime.now(timezone.utc)
    app_obj.lost_reason = (
        models.LostReason(lost_reason) if new_stage == models.Stage.CLOSED_LOST and lost_reason else None
    )

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
    _apply_meeting_quality(meeting, my_performance, employer_engagement)
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
    _apply_meeting_quality(meeting, my_performance, employer_engagement)
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
    thread.people = resolved_people
    db.add(thread)
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
        "selected_person_ids": {p.id for p in thread.people},
    })


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
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)


@router.post("/ui/email-threads/{thread_id}/delete")
def delete_email_thread_ui(thread_id: int, db: Session = Depends(get_db)):
    thread = _get_or_404(db, models.EmailThread, thread_id)
    db.delete(thread)
    db.commit()
    return RedirectResponse(url="/email-threads", status_code=303)
