"""Everything more than one UI module needs.

`app/routers/ui.py` had reached 3,278 lines, which meant every task -- however
small -- began by reading all of it. That is a cost in a model's context window
and in yours, and it is the reason this split happened before switching to a
cheaper model rather than after.

What lives here was decided mechanically, not by taste: each top-level block
was placed with the domain that uses it, and only the ones crossing a boundary
stayed behind. That is why `_forecast_for` sits in `applications.py` while
`_get_or_404` is here.

Domain modules do ``from .shared import *``. That reaches the underscore names
because ``__all__`` lists them explicitly, which was the point -- renaming on
top of moving would have produced a diff nobody could review, and a refactor
this size has to be provably behaviour-preserving. The gate is what stands
behind it: same 29 test files, same render cases, same route count.
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

# templates/ lives next to app/, resolved relative to this file so it works
# regardless of the current working directory.
TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STAGE_VALUES = [s.value for s in models.Stage]          # ordered; Closed Lost last

# The funnel rungs only -- Staging and Closed Lost are deliberately absent
# (see the note above models.STAGE_ORDER).
STAGE_ORDER_VALUES = [s.value for s in models.STAGE_ORDER]

COMPANY_TYPES = [t.value for t in models.CompanyType]

LOST_CATEGORY_VALUES = [c.value for c in models.LostCategory]

PERSON_ROLE_VALUES = [r.value for r in models.PersonRole]

APPLICATION_SOURCE_VALUES = [s.value for s in models.ApplicationSource]

FORECAST_VALUES = [f.value for f in models.ForecastCategory]

SENIORITY_VALUES = [v.value for v in models.Seniority]

SPECIALITY_VALUES = [v.value for v in models.Speciality]

FUNDING_STAGE_VALUES = [v.value for v in models.FundingStage]

EMPLOYEE_BAND_VALUES = [v.value for v in models.EmployeeBand]

# The forecast's component budgets, read straight off the model so the
# breakdown panel can print "12.0/25" without the denominators being retyped
# into a template. They were hardcoded there once and went stale the moment the
# weights were rebalanced, which turned an explanation of the number into a
# contradiction of it.
FORECAST_WEIGHTS = {
    "stage": forecast_model.W_STAGE,
    "meetings": forecast_model.W_MEETINGS,
    "email": forecast_model.W_EMAIL,
    "fit": forecast_model.W_FIT,
    "source": forecast_model.W_SOURCE,
    "champion": forecast_model.W_CHAMPION,
}

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

def _apply_activity_quality(obj, my_performance: str, employer_engagement: str) -> None:
    """Write the two halves of an activity's quality: how well you did, and how
    interested they were.

    Takes a Meeting or an EmailThread. They carry the same pair of columns with
    the same meaning, and the forecast reads one shape over both, so the writer
    is shared too -- a second near-identical function is exactly how the two
    would drift apart.

    Reuses _parse_score because these share its scale and its rules exactly --
    0-100, clamped rather than rejected, blank meaning "no judgment formed"
    rather than zero. That last distinction is the whole reason these aren't
    defaulted: a call where you didn't rate your own performance and a call you
    rated 0 are opposite claims, and the forecast reads them as such.

    Unlike `score` these carry no `*_at` stamp. `scored_at` exists because
    score calibration later needs to know when a judgment was formed; these two
    are attributes of the activity, read at the activity's own date. A
    timestamp nobody reads is a column that can only rot.
    """
    obj.my_performance = _parse_score(my_performance)
    obj.employer_engagement = _parse_score(employer_engagement)

def _has_human_rating(thread) -> bool:
    """Has a person put a number in this thread's quality pair.

    `rating_source` is NULL for a human value and "model" for a read one, so
    "there is a value and nobody has claimed it for the model" is the test. It
    also answers correctly for every row that predates the column: before the
    automatic read existed, every value in these fields was typed by hand, so
    NULL-means-human is a fact about history rather than an assumption.

    One case it deliberately cannot distinguish: clearing both halves of a
    model-written pair leaves a thread that looks exactly like one nobody ever
    rated, so a later body edit will read it again. Storing "the human said
    blank" would need a fourth state on top of a tri-state, and the cheaper
    answer is that re-reads only fire on a body change or a button press, so a
    thread you cleared stays cleared unless you change the messages in it.
    """
    if thread.rating_source == "model":
        return False
    return thread.my_performance is not None or thread.employer_engagement is not None

def _read_thread_now(thread) -> Optional[str]:
    """Ask the model to rate one thread, and write the result. Returns an error.

    Returns None on success (including the success case where the model
    declined to score and only a note was stored), or a human-readable string
    when the call or the parse failed. Nothing is written on failure.

    This is the only place in the app that sends data to a third party without
    a button press behind it -- it fires when you save a thread. Three guards
    keep that narrow: it does nothing without a key, nothing without a body,
    and nothing at all if you have already formed your own view. A human
    rating is never overwritten, by this or by anything else.
    """
    if not llm.enabled():
        return "Automatic reading is off — no ANTHROPIC_API_KEY is set."
    if not (thread.body or "").strip():
        return "There are no messages in this thread to read."
    if _has_human_rating(thread):
        return "You have already rated this thread; a read would not overwrite it."

    payload = thread_read.build_read_payload(
        subject=thread.subject,
        body=thread.body,
        participants=thread.participants,
        started_at=thread.started_at,
        last_message_at=thread.last_message_at,
        company=(thread.application.company.name
                 if thread.application and thread.application.company else None),
        role_title=thread.application.title if thread.application else None,
        stage=(thread.application.stage.value
               if thread.application and thread.application.stage else None),
        context=thread.application.context if thread.application else None,
    )
    try:
        text, model_used = llm.generate(
            thread_read.SYSTEM_PROMPT,
            thread_read.build_messages(payload),
            # Three lines back, so the token ceiling is a runaway guard rather
            # than a shape constraint. The timeout is short because you are
            # sitting on a save, not watching a Brief render: a form that
            # appears to hang for three minutes reads as a crashed app, and
            # losing the reading is much cheaper than that.
            max_tokens=300,
            timeout=60,
        )
    except llm.LLMError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- see below
        # Deliberately broad. `llm.generate` promises LLMError but cannot fully
        # deliver: a 200 response whose body is not JSON raises ValueError out
        # of `resp.json()`, and anything escaping here would propagate out of
        # the POST handler *before* its `db.commit()`, rolling back the user's
        # entire edit. Losing a reading is a small failure; losing the subject
        # line they just fixed because a proxy returned an HTML error page is
        # not, and it is not a failure they could connect to a cause.
        return "The read failed unexpectedly: {}".format(exc)

    perf, eng, note, understood = thread_read.parse_read(text)
    if not understood:
        # Nothing recognisable came back, or only half of it did. Write nothing
        # rather than a guess -- a wrong number here propagates into the
        # forecast, the board colour and every comparison across applications,
        # and it does not look wrong. A half-parsed reply is the worse case:
        # `_rating` promotes a lone surviving number to the whole reading.
        return "The model's reply could not be read as a rating."

    thread.my_performance = perf
    thread.employer_engagement = eng
    thread.rating_note = note
    thread.rating_source = "model"
    # Aware, matching every other `_utcnow`-stamped column. `_naive_utc()`
    # flattens it before anything compares it against a form-entered date.
    thread.rated_at = datetime.now(timezone.utc)
    thread.rating_model = model_used
    return None

def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Flatten a datetime to naive UTC so mixed rows can be compared.

    Form-entered dates come back naive; columns stamped by `_utcnow` come back
    aware. Comparing the two raises TypeError, and the mix is unavoidable
    because both kinds live in the same tables.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value

CLOSED_STAGES = (models.Stage.CLOSED_WON, models.Stage.CLOSED_LOST)

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

# --------------------------------------------------------------------------- #
# What I'm looking for: standing criteria, and fit against them
# --------------------------------------------------------------------------- #
def _looking_for(db: Session) -> models.LookingFor:
    """The singleton row, created on first read.

    Created lazily rather than seeded at startup, so a fresh database has no
    row until someone opens the page -- and an empty statement stays
    distinguishable from one that was never written.
    """
    row = db.get(models.LookingFor, 1)
    if row is None:
        row = models.LookingFor(id=1)
        db.add(row)
        # Seed Gabe's six axes on the very first visit, and only then. The
        # singleton's existence is the marker, so deleting a criterion later
        # never resurrects it -- a starter list that grows back is worse than
        # no starter list, because you cannot tell it to stop.
        for order, (name, blurb) in enumerate(fit.STARTER_CRITERIA):
            db.add(models.Criterion(name=name, description=blurb,
                                    sort_order=order))
        db.commit()
        db.refresh(row)
    return row

def _criteria(db: Session) -> List[models.Criterion]:
    return (
        db.query(models.Criterion)
        .order_by(models.Criterion.sort_order, models.Criterion.id)
        .all()
    )

def _fit_rows(app_obj, criteria) -> List[dict]:
    """One row per criterion for this application, rated or not.

    Every criterion appears even with no rating, because the blanks are the
    point: they are what tells you the score is over two axes out of seven
    rather than that the opportunity is thin.
    """
    by_id = {r.criterion_id: r for r in app_obj.criterion_ratings}
    rows = []
    for crit in criteria:
        rating = by_id.get(crit.id)
        rows.append({
            "criterion": crit,
            "name": crit.name,
            "score": rating.score if rating else None,
            "note": rating.note if rating else None,
        })
    return rows

def _fit_for(app_obj, criteria, threshold) -> dict:
    return fit.score(_fit_rows(app_obj, criteria), threshold=threshold)

# --------------------------------------------------------------------------- #
# Log: say what happened, review what it proposes, apply what you approve
# --------------------------------------------------------------------------- #
LOST_CATEGORY_VALUES = [c.value for c in models.LostCategory]

# --------------------------------------------------------------------------- #
# Settings: what the fields mean, and what you are looking for
# --------------------------------------------------------------------------- #
def _definition_overrides(db: Session) -> dict:
    """Your edited definitions, keyed by field. Absent fields use the default."""
    return {
        row.field: row.definition
        for row in db.query(models.FieldDefinition).all()
        if (row.definition or "").strip()
    }

def _definition_rows(db: Session) -> List[dict]:
    return fields_model.rows(overrides=_definition_overrides(db),
                             writable=logspec.WRITABLE)


__all__ = [
    "APPLICATION_SOURCE_VALUES",
    "CLOSED_STAGES",
    "COMPANY_TYPES",
    "EMPLOYEE_BAND_VALUES",
    "FORECAST_VALUES",
    "FORECAST_WEIGHTS",
    "FUNDING_STAGE_VALUES",
    "LOST_CATEGORY_VALUES",
    "PERSON_ROLE_VALUES",
    "SENIORITY_VALUES",
    "SPECIALITY_VALUES",
    "STAGE_ORDER_VALUES",
    "STAGE_VALUES",
    "TEMPLATES_DIR",
    "_apply_activity_quality",
    "_apply_score",
    "_company_name_from_domain",
    "_company_website",
    "_criteria",
    "_definition_overrides",
    "_definition_rows",
    "_domain_of",
    "_extract_upload_text",
    "_find_or_create_company",
    "_find_or_create_company_by_domain",
    "_find_or_create_person_by_email",
    "_fit_for",
    "_fit_rows",
    "_get_or_404",
    "_has_human_rating",
    "_looking_for",
    "_naive_utc",
    "_parse_dt",
    "_parse_score",
    "_read_thread_now",
    "_same_to_the_minute",
    "_to_float",
    "templates",
]
