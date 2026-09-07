"""People: recruiters, hiring managers, interviewers, referrals.

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
