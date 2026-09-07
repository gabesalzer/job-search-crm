"""Companies, and the web-enrichment button.

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


def _has_human_enrichment(company) -> bool:
    return (company.enrichment_source is None
            and (company.funding_stage is not None
                 or company.employee_band is not None))

def _enrich_company_now(company) -> Optional[str]:
    """Read funding stage and headcount off the company's own website.

    Never fires on its own. It costs a Firecrawl fetch and an API call per
    press, and — more to the point — it is the only feature in this app whose
    output cannot be checked against anything already on the record. A step
    that needs that much judgment gets a button.

    Only text actually fetched from the site can produce a value here. The
    prompt forbids answering from what the model remembers, because a recalled
    funding round is frequently stale, cannot be cited, and would be stored
    beside a URL it did not come from.
    """
    if not llm.enabled():
        return "Lookups are off — no ANTHROPIC_API_KEY is set."
    if not (company.website or "").strip():
        return ("No website is recorded for this company, so there is nothing "
                "to read. Add one above and save first.")
    if _has_human_enrichment(company):
        return ("You have already filled these in; a lookup would not "
                "overwrite your values.")

    try:
        page = scrape.scrape_page_text(company.website)
    except Exception as exc:  # noqa: BLE001 -- any fetch failure, incl. httpx
        return "Couldn't fetch the site: {}".format(exc)

    packet = classify.build_company_packet(
        name=company.name, url=page["url"], page_text=page["text"])
    try:
        text, model_used = llm.generate(
            classify.company_system_prompt(),
            classify.build_company_messages(packet),
            max_tokens=classify.MAX_TOKENS,
            timeout=60,
        )
    except llm.LLMError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return "The lookup failed unexpectedly: {}".format(exc)

    stage, band, note, understood = classify.parse_company_reply(text)
    if not understood:
        return "The model's reply could not be read as a lookup result."

    company.funding_stage = models.FundingStage(stage) if stage else None
    company.employee_band = models.EmployeeBand(band) if band else None
    company.enrichment_note = note
    company.enrichment_source = "model"
    # The page it actually read, which may be /about rather than the homepage.
    # A value is only as checkable as the page behind it.
    company.enrichment_url = page["url"]
    company.enriched_at = datetime.now(timezone.utc)
    company.enrichment_model = model_used
    return None

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
def edit_company_page(company_id: int, request: Request,
                      lookup_result: str = "", db: Session = Depends(get_db)):
    company = _get_or_404(db, models.Company, company_id)
    return templates.TemplateResponse(request, "company_edit.html", {
        "active": "companies",
        "company": company,
        "company_types": COMPANY_TYPES,
        "funding_stages": FUNDING_STAGE_VALUES,
        "employee_bands": EMPLOYEE_BAND_VALUES,
        # Reports what a press did even on success, because "the site does not
        # say" is a legitimate outcome that leaves both fields blank -- which
        # looks exactly like the button not working.
        "lookup_result": lookup_result,
        "lookup_enabled": llm.enabled(),
    })

@router.post("/ui/companies/{company_id}/enrich")
def enrich_company_ui(company_id: int, db: Session = Depends(get_db)):
    """Read funding stage and headcount off the company's own site.

    A button, never automatic: it costs a fetch and an API call, and it is the
    one derived field in this app that cannot be checked against anything
    already on the record.
    """
    company = _get_or_404(db, models.Company, company_id)
    error = _enrich_company_now(company)
    if error:
        db.rollback()
        return RedirectResponse(
            url="/companies/{}/edit?lookup_result={}".format(
                company_id, quote(error)),
            status_code=303)
    db.commit()

    found = []
    if company.funding_stage:
        found.append(company.funding_stage.value)
    if company.employee_band:
        found.append("{} employees".format(company.employee_band.value))
    message = ("read {} from {}".format(" and ".join(found), company.enrichment_url)
               if found else
               "read {} and found nothing it states about funding or headcount "
               "(left blank rather than guessed)".format(company.enrichment_url))
    return RedirectResponse(
        url="/companies/{}/edit?lookup_result={}".format(company_id, quote(message)),
        status_code=303)

@router.post("/ui/companies/{company_id}/edit")
def update_company_ui(
    company_id: int,
    name: str = Form(...),
    company_type: str = Form("Employer"),
    website: str = Form(""),
    industry: str = Form(""),
    notes: str = Form(""),
    funding_stage: str = Form(""),
    employee_band: str = Form(""),
    db: Session = Depends(get_db),
):
    company = _get_or_404(db, models.Company, company_id)
    before = (company.funding_stage, company.employee_band)

    company.name = name
    company.company_type = models.CompanyType(company_type)
    company.website = website or None
    company.industry = industry or None
    company.notes = notes or None
    company.funding_stage = (
        models.FundingStage(funding_stage) if funding_stage else None)
    company.employee_band = (
        models.EmployeeBand(employee_band) if employee_band else None)

    # Typing in either field takes ownership of both, and drops the machine's
    # note, model and source URL along with it. Same rule as every other
    # provenance pair in this app: a value you set is never overwritten, and it
    # must not go on carrying a citation that no longer describes it.
    if (company.funding_stage, company.employee_band) != before:
        company.enrichment_source = None
        company.enrichment_note = None
        company.enrichment_url = None
        company.enrichment_model = None
        company.enriched_at = None

    db.commit()
    return RedirectResponse(url="/companies", status_code=303)

@router.post("/ui/companies/{company_id}/delete")
def delete_company_ui(company_id: int, db: Session = Depends(get_db)):
    company = _get_or_404(db, models.Company, company_id)
    db.delete(company)  # cascades to its postings, applications, and people
    db.commit()
    return RedirectResponse(url="/companies", status_code=303)
