"""Settings: field definitions, and what I'm looking for.

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


@router.post("/ui/looking-for")
def update_looking_for_ui(statement: str = Form(""), dq_threshold: str = Form(""),
                          db: Session = Depends(get_db)):
    row = _looking_for(db)
    row.statement = statement.strip() or None
    parsed = fit.clamp_score(dq_threshold)
    # An out-of-scale threshold leaves the stored one alone rather than
    # snapping to a bound. Silently rewriting it would change which
    # applications are disqualified without anyone asking for that.
    if parsed is not None:
        row.dq_threshold = parsed
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)

@router.post("/ui/looking-for/criteria")
def create_criterion_ui(name: str = Form(...), description: str = Form(""),
                        db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/settings#looking-for", status_code=303)
    highest = db.query(models.Criterion).count()
    db.add(models.Criterion(name=name, description=description.strip() or None,
                            sort_order=highest))
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)

@router.post("/ui/looking-for/criteria/{criterion_id}/edit")
def update_criterion_ui(criterion_id: int, name: str = Form(...),
                        description: str = Form(""),
                        db: Session = Depends(get_db)):
    crit = _get_or_404(db, models.Criterion, criterion_id)
    if name.strip():
        crit.name = name.strip()
    crit.description = description.strip() or None
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)

@router.post("/ui/looking-for/criteria/{criterion_id}/delete")
def delete_criterion_ui(criterion_id: int, db: Session = Depends(get_db)):
    """Deleting a criterion takes its ratings with it.

    Cascade rather than orphan, because a rating means nothing without the
    axis it was made against -- and leaving them would let a deleted criterion
    go on affecting an average nobody can see the source of.
    """
    crit = _get_or_404(db, models.Criterion, criterion_id)
    db.delete(crit)
    db.commit()
    return RedirectResponse(url="/settings#looking-for", status_code=303)

@router.post("/ui/applications/{application_id}/fit")
async def update_fit_ui(application_id: int, request: Request,
                        db: Session = Depends(get_db)):
    """Save this application's ratings against every criterion.

    Async, and the only async route in this file, because the field names are
    per-criterion (`crit_7`) and cannot be declared as `Form(...)` parameters
    -- reading them needs the raw form, which is awaitable.

    Its own form rather than part of the main edit post: rating is a different
    activity from correcting a date, and folding it in would mean a stray
    submit while editing notes could clear ratings that were not on screen.
    """
    app_obj = _get_or_404(db, models.JobApplication, application_id)
    form = await request.form()
    existing = {r.criterion_id: r for r in app_obj.criterion_ratings}

    for crit in _criteria(db):
        raw = form.get("crit_{}".format(crit.id))
        note = (form.get("note_{}".format(crit.id)) or "").strip() or None
        value = fit.clamp_score(raw)
        row = existing.get(crit.id)
        if value is None and note is None:
            # Nothing on either side: drop the row rather than storing an empty
            # one, so "never rated" and "rated then cleared" look the same in
            # the database as they do on the page.
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            row = models.CriterionRating(criterion_id=crit.id,
                                         application_id=app_obj.id)
            db.add(row)
        row.score = value
        row.note = note

    db.commit()
    return RedirectResponse(
        url="/applications/{}/edit".format(application_id), status_code=303)

def _settings_context(db: Session, **extra) -> dict:
    """Everything both halves of the page need.

    The Looking For half is unchanged and still reads through `_looking_for`
    and `_criteria`; folding the page in was a template move, not a rewrite,
    which is why its routes keep their `/ui/looking-for/...` paths. Renaming
    working form endpoints to match a nav change would be churn with a
    regression attached.
    """
    row = _looking_for(db)
    criteria = _criteria(db)
    threshold = fit.threshold_of(row.dq_threshold)
    apps = (
        db.query(models.JobApplication)
        .options(selectinload(models.JobApplication.criterion_ratings),
                 selectinload(models.JobApplication.company))
        .all()
    )
    context = {
        "active": "settings",
        "definition_rows": _definition_rows(db),
        "definition_groups": fields_model.groups(),
        "writers": fields_model.WRITERS,
        "looking_for": row,
        "criteria": criteria,
        "threshold": threshold,
        "scale_min": fit.SCALE_MIN,
        "scale_max": fit.SCALE_MAX,
        "ranked": fit.rank([
            {"id": a.id,
             "company": a.company.name if a.company else None,
             "title": a.title,
             "stage": a.stage.value if a.stage else None,
             "ratings": _fit_rows(a, criteria)}
            for a in apps
        ], threshold=threshold),
        "saved": "",
    }
    context.update(extra)
    return context

@router.get("/settings")
def settings_page(request: Request, saved: str = "",
                  db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "settings.html", _settings_context(db, saved=saved))

@router.get("/looking-for")
def looking_for_redirect():
    """Folded into Settings. Redirect rather than delete: this URL was in the
    nav for weeks and is in his history."""
    return RedirectResponse(url="/settings#looking-for", status_code=301)

@router.post("/ui/settings/definitions/{field}")
def update_definition_ui(field: str, definition: str = Form(""),
                         db: Session = Depends(get_db)):
    """Save your wording for one field, or clear it back to the default.

    A blank submission deletes the override rather than storing an empty
    string. An empty definition is worse than the shipped one -- it would
    hand the model a bare field name and this page a blank row -- so the only
    thing "empty" can sensibly mean here is "use the default".
    """
    if field not in fields_model.BY_FIELD:
        raise HTTPException(404, "no such field")
    row = (db.query(models.FieldDefinition)
           .filter(models.FieldDefinition.field == field).first())
    text = (definition or "").strip()
    default = fields_model.default_definition(field)
    if not text or text == default:
        # Matching the default exactly is also a reset. Storing it would be a
        # row that pins today's wording and silently stops tracking a better
        # one shipped later.
        if row is not None:
            db.delete(row)
        db.commit()
        return RedirectResponse(
            url="/settings?saved={}#definitions".format(quote(field)),
            status_code=303)
    if row is None:
        row = models.FieldDefinition(field=field)
        db.add(row)
    row.definition = text
    db.commit()
    return RedirectResponse(
        url="/settings?saved={}#definitions".format(quote(field)),
        status_code=303)

@router.post("/ui/settings/definitions/{field}/reset")
def reset_definition_ui(field: str, db: Session = Depends(get_db)):
    if field not in fields_model.BY_FIELD:
        raise HTTPException(404, "no such field")
    row = (db.query(models.FieldDefinition)
           .filter(models.FieldDefinition.field == field).first())
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse(
        url="/settings?saved={}#definitions".format(quote(field)),
        status_code=303)
