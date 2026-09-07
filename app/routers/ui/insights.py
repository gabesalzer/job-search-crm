"""Insights: the analytics page, and the chat that can refilter it.

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
# Chat: ask questions about the whole pipeline
# --------------------------------------------------------------------------- #
# The third and widest place data leaves the box. The Brief sends one
# application when you press a button; the thread read sends one thread when
# you save it; this sends everything, on every question. That was a deliberate
# choice made with the alternatives on the table -- see the module note in
# chat.py -- and this is the code that carries it out, so it is worth being
# able to read in one place exactly what gets assembled.
#
# Same division of labour as the Brief: all ORM walking happens here, and
# `chat.py` stays plain-data-in, text-out and testable with literals.

# How many turns of the stored conversation are replayed to the model. The
# history sits *after* the cached corpus block and so is billed fresh every
# time; letting it grow without bound would slowly eat the saving the cache
# exists to produce. Twelve is far more context than any real follow-up needs.
CHAT_HISTORY_TURNS = 12

# The chat's own ceiling, well above the Brief's. Answers here are often "list
# the four that have gone quiet and why", which is genuinely longer than a
# two-section brief, and truncating mid-list is a worse failure than a slightly
# larger bill.
CHAT_MAX_TOKENS = 2000

def _chat_corpus(db: Session, analytics: Optional[dict] = None) -> str:
    """Walk every application into the plain dicts `chat.build_corpus` wants.

    One eager load for the whole page rather than per-application lazy loads:
    this touches every relationship on every row, which is exactly the shape
    that turns into hundreds of queries if left to default loading.
    """
    apps = (
        db.query(models.JobApplication)
        .options(
            selectinload(models.JobApplication.meetings),
            selectinload(models.JobApplication.email_threads),
            selectinload(models.JobApplication.people),
            selectinload(models.JobApplication.stage_history),
            selectinload(models.JobApplication.company),
            selectinload(models.JobApplication.resume),
            selectinload(models.JobApplication.job_posting),
        )
        .all()
    )
    payload = []
    for a in apps:
        posting = None
        if a.job_posting:
            posting = {
                "title": a.job_posting.title,
                "url": a.job_posting.url,
                "location": a.job_posting.location,
                "jd_text": a.job_posting.jd_text,
            }
        payload.append({
            "company": a.company.name if a.company else None,
            "title": a.title,
            "stage": a.stage.value if a.stage else None,
            "source": a.source.value if a.source else None,
            # Every date goes through `_naive_utc` on the way out. Form-entered
            # dates are naive and stamped ones are aware, and chat.py sorts and
            # subtracts across the whole mix to work out what is most recent --
            # which raises TypeError the moment the two meet unflattened.
            "applied_date": _naive_utc(a.applied_date),
            "champion": a.champion,
            "manual_forecast": a.manual_forecast.value if a.manual_forecast else None,
            "seniority": a.seniority.value if a.seniority else None,
            "speciality": a.speciality.value if a.speciality else None,
            "funding_stage": (a.company.funding_stage.value
                              if a.company and a.company.funding_stage else None),
            "employee_band": (a.company.employee_band.value
                              if a.company and a.company.employee_band else None),
            "lost_reason": a.lost_reason,
            "lost_category": a.lost_category.value if a.lost_category else None,
            "context": a.context,
            "next_steps": a.next_steps,
            "pain": a.pain,
            "process": a.process,
            "risks": a.risks,
            "notes": a.notes,
            "resume_label": a.resume.label if a.resume else None,
            "posting": posting,
            "people": [
                {
                    "name": p.name,
                    "role": p.role.value if p.role else None,
                    "email": p.email,
                    "company": p.company.name if p.company else None,
                    "is_champion": p.is_champion,
                }
                for p in a.people
            ],
            "stage_history": [
                {
                    "changed_at": _naive_utc(h.changed_at),
                    "from_stage": h.from_stage.value if h.from_stage else None,
                    "to_stage": h.to_stage.value if h.to_stage else None,
                }
                for h in a.stage_history
            ],
            "meetings": [
                {
                    # `when` is the one key both activity kinds share, because
                    # recency is what the budget is spent on and a meeting date
                    # and a thread's last message are the same fact for that
                    # purpose. Falling back to `scored_at` keeps an undated
                    # meeting from sorting to the beginning of time and being
                    # dropped first, which is the opposite of what you want for
                    # something logged this week without a date typed in.
                    "when": _naive_utc(m.meeting_date or m.scored_at or m.created_at),
                    "title": m.title,
                    "kind": m.meeting_type.value if m.meeting_type else None,
                    "summary": m.summary,
                    "transcript": m.transcript,
                    "notes": m.notes,
                    "score": m.score,
                    "score_reason": m.score_reason,
                    "my_performance": m.my_performance,
                    "employer_engagement": m.employer_engagement,
                }
                for m in a.meetings
            ],
            "email_threads": [
                {
                    "when": _naive_utc(t.last_message_at or t.started_at
                                       or t.scored_at or t.created_at),
                    "subject": t.subject,
                    "participants": t.participants,
                    "body": t.body,
                    "notes": t.notes,
                    "score": t.score,
                    "score_reason": t.score_reason,
                    "my_performance": t.my_performance,
                    "employer_engagement": t.employer_engagement,
                    # So the packet can say a rating was written by an
                    # automatic read rather than by hand. Same labelling the
                    # Brief does, and for the same reason: a number the model
                    # wrote should not come back to it as corroboration.
                    "rating_source": t.rating_source,
                }
                for t in a.email_threads
            ],
        })
    return chat_model.build_corpus(payload, analytics=analytics)

def _chat_history(db: Session) -> List[models.ChatMessage]:
    return (
        db.query(models.ChatMessage)
        .order_by(models.ChatMessage.id)
        .all()
    )

@router.get("/chat")
def chat_redirect():
    """The chat and the analytics page merged into /insights.

    Kept as a redirect rather than deleted: this URL has been in the nav, in
    the README, and in Gabe's browser history. A 301 costs one line and means
    nothing that already points here breaks.
    """
    return RedirectResponse(url="/insights", status_code=301)

@router.post("/ui/chat/clear")
def chat_clear(db: Session = Depends(get_db)):
    """Delete the whole conversation.

    A real delete, not a hidden flag. This is the only place in the app that
    throws away data on purpose, and it earns that: the transcript accumulates
    quoted fragments of other people's emails and interviews, so being able to
    empty it in one click is part of what makes the feature defensible.
    """
    db.query(models.ChatMessage).delete()
    db.commit()
    return RedirectResponse(url="/insights", status_code=303)

def _analytics_apps(db: Session) -> List[dict]:
    """Every application flattened into the plain dicts `analytics.py` wants.

    Same division of labour as the other three adapters in this file: the ORM
    walking happens here and the arithmetic stays in a module that can be
    tested against literals.
    """
    apps = (
        db.query(models.JobApplication)
        .options(
            selectinload(models.JobApplication.stage_history),
            selectinload(models.JobApplication.company),
            selectinload(models.JobApplication.resume),
        )
        .all()
    )
    return [
        {
            "id": a.id,
            "company": a.company.name if a.company else None,
            "title": a.title,
            "stage": a.stage.value if a.stage else None,
            "source": a.source.value if a.source else None,
            # Flattened on the way out, like everywhere else -- analytics.py
            # subtracts these from `_utcnow`-stamped history rows constantly.
            "applied_date": _naive_utc(a.applied_date),
            "created_at": _naive_utc(a.created_at),
            "lost_category": a.lost_category.value if a.lost_category else None,
            "lost_reason": a.lost_reason,
            "seniority": a.seniority.value if a.seniority else None,
            "speciality": a.speciality.value if a.speciality else None,
            "funding_stage": (a.company.funding_stage.value
                              if a.company and a.company.funding_stage else None),
            "employee_band": (a.company.employee_band.value
                              if a.company and a.company.employee_band else None),
            # Carried so the view can be filtered or compared by resume --
            # "does v3 actually get further than v2" is one of the few
            # questions this dataset can answer that the board cannot.
            "resume_label": a.resume.label if a.resume else None,
            "stage_history": [
                {
                    "from_stage": h.from_stage.value if h.from_stage else None,
                    "to_stage": h.to_stage.value if h.to_stage else None,
                    "changed_at": _naive_utc(h.changed_at),
                }
                for h in a.stage_history
            ],
        }
        for a in apps
    ]

@router.get("/analytics")
def analytics_redirect():
    """Merged into /insights. Redirect rather than delete -- see chat_redirect."""
    return RedirectResponse(url="/insights", status_code=301)

def _vocabulary(apps: List[dict]) -> dict:
    """The values that actually exist, for validating a proposed filter.

    Built from the data rather than from the enums, and the difference matters.
    `LostCategory` has nine members but a pipeline might only have used two, and
    a filter on a category no record carries returns an empty cohort -- which
    renders as "not enough data", indistinguishable from a real finding.
    Validating against what is present turns that silent emptiness into a
    visible rejection.
    """
    def seen(key):
        return sorted({str(a.get(key)) for a in apps if a.get(key)})
    return {
        "source": seen("source"),
        "stage": seen("stage"),
        "lost_category": seen("lost_category"),
        "seniority": seen("seniority"),
        "speciality": seen("speciality"),
        "funding_stage": seen("funding_stage"),
        "employee_band": seen("employee_band"),
        "resume": seen("resume_label"),
        "company": seen("company"),
    }

def _insights_context(db: Session, params: dict) -> dict:
    """Everything the merged page renders, filtered by the query string.

    The filter is read from the URL on every request rather than held in a
    session, so a chat-driven view is a link: shareable, bookmarkable, and
    undoable with the back button. Undoing the chat never requires the chat.
    """
    apps = _analytics_apps(db)
    spec, rejected = viewspec.from_query(params, vocabulary=_vocabulary(apps))
    # Rejections raised while parsing the model's proposal travel here in the
    # query string, because they happened on the POST and have to survive the
    # redirect. Shown alongside any raised by the URL itself.
    carried = (params.get("rejected") or "").strip()
    if carried:
        rejected = [carried] + rejected
    visible = viewspec.apply(apps, spec)

    payload = analytics_model.overview(visible, STAGE_ORDER_VALUES)
    comparison = []
    for name, group in viewspec.cohorts(visible, spec.get("compare_by")):
        summary = analytics_model.overview(group, STAGE_ORDER_VALUES)
        comparison.append({
            "name": name,
            "total": summary["total"],
            "intervals": summary["intervals"],
            "funnel": summary["funnel"],
        })

    query = viewspec.to_query(spec)
    # Each chip carries the link that removes it, built here rather than in the
    # template: "the URL minus this one parameter" is a computation, and Jinja
    # is the wrong place to do computations you want to be able to test.
    chips = []
    for chip in viewspec.describe(spec):
        remaining = {k: v for k, v in query.items() if k != chip["param"]}
        chip["href"] = "/insights" + ("?" + urlencode(remaining) if remaining else "")
        chips.append(chip)

    return {
        "active": "insights",
        "stage_order": STAGE_ORDER_VALUES,
        "chips": chips,
        "query_string": urlencode(query),
        "rejected": rejected,
        "filtered": bool(spec),
        "compare_by": spec.get("compare_by"),
        "comparison": comparison,
        # The unfiltered count, so the chip row can say "6 of 11" rather than
        # leaving a suddenly-small pipeline looking like data loss.
        "unfiltered_total": len(apps),
        "query": query,
        "messages": _chat_history(db),
        "chat_enabled": llm.enabled(),
        "model_name": llm.model_name(),
        **payload,
    }

@router.get("/insights")
def insights_page(request: Request, error: str = "", db: Session = Depends(get_db)):
    context = _insights_context(db, dict(request.query_params))
    context["error"] = error
    return templates.TemplateResponse(request, "insights.html", context)

@router.post("/ui/insights/ask")
def insights_ask(request: Request, question: str = Form(""),
                 db: Session = Depends(get_db)):
    question = (question or "").strip()
    current = {k: v for k, v in request.query_params.items()}
    back = "/insights" + ("?" + urlencode(current) if current else "")
    if not question:
        return RedirectResponse(url=back, status_code=303)

    apps = _analytics_apps(db)
    vocabulary = _vocabulary(apps)
    active, _ = viewspec.from_query(current, vocabulary=vocabulary)
    chips = viewspec.describe(active)

    history = [{"role": m.role, "content": m.content} for m in _chat_history(db)]

    # Stored before the call, so a timeout does not also cost the question.
    db.add(models.ChatMessage(role="user", content=question))
    db.commit()

    usage: dict = {}
    try:
        text, model_used = llm.generate(
            chat_model.build_system_blocks(
                # The analytics handed to the model are the *unfiltered*
                # pipeline, matching the cached corpus. The filter currently on
                # screen travels on the question turn instead -- see
                # chat._analytics_block for why the two are separated.
                _chat_corpus(db, analytics_model.overview(apps, STAGE_ORDER_VALUES))),
            chat_model.build_messages(
                history, question, max_turns=CHAT_HISTORY_TURNS,
                view_note="; ".join(c["label"] for c in chips) or "everything"),
            max_tokens=CHAT_MAX_TOKENS,
            usage_out=usage,
        )
    except llm.LLMError as exc:
        sep = "&" if current else "?"
        return RedirectResponse(
            url="{}{}error={}".format(back, sep, quote(str(exc))), status_code=303)

    prose, block = viewspec.extract_block(text)
    proposed, rejected = viewspec.parse(block, vocabulary=vocabulary)

    db.add(models.ChatMessage(
        role="assistant",
        content=prose or text,
        model=model_used,
        usage=json.dumps(usage) if usage else None,
        # What the answer did to the view, kept beside the answer that did it.
        # Without this the transcript reads as a series of remarks with no
        # record of which one changed what you are looking at.
        view_spec=json.dumps(viewspec.to_query(proposed)) if proposed else None,
    ))
    db.commit()

    # A proposed view replaces the current one rather than merging into it.
    # Merging looks helpful and is not: two questions in a row would silently
    # intersect into a cohort nobody asked for, and the only way back would be
    # to notice and undo it by hand.
    destination = "/insights"
    query = viewspec.to_query(proposed) if proposed else {}
    if rejected:
        query["rejected"] = " ".join(rejected)
    if query:
        destination += "?" + urlencode(query)
    return RedirectResponse(url=destination, status_code=303)

@router.post("/ui/insights/reset")
def insights_reset():
    """Drop every filter. One click, no question asked of the model."""
    return RedirectResponse(url="/insights", status_code=303)
