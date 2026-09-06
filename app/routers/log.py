"""The log's JSON door: post a note from outside the browser.

This exists so the capture layer is replaceable. The browser form on ``/log``
and this endpoint are two clients of the same three steps -- read the note,
propose changes, wait for a person -- and neither is privileged. A phone
shortcut, a webhook, an n8n flow, a shell script piping a transcription: any of
them can queue a note here without a line of this app changing.

That is the whole argument for building the endpoint before building the
integration. An orchestration layer wired to a system with no write API ends up
defining the write API by accident, in whatever shape the orchestrator found
convenient. With the endpoint first, the orchestrator becomes a front door that
can be swapped or removed.

**Automation does not get an exception to the review gate.** A note posted here
is read and its changes are *proposed*; nothing is written to any application
until a person ticks boxes on ``/log``. An endpoint that applied its own
changes would be the same feature with the one part that makes it trustworthy
removed -- and it would be strictly worse than the browser path, because the
mistakes would land while nobody was looking.

Authentication is the app's existing Basic Auth (see ``main.BasicAuthMiddleware``),
which already gates every path but ``/health``. A second credential was
considered and rejected: a bearer token here would be another secret to set,
rotate and leak, protecting a surface the existing password already covers.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db

router = APIRouter(prefix="/api/log", tags=["log"])


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1,
                      description="What was said, as text. Transcribe first.")
    origin: Optional[str] = Field(
        None, max_length=16,
        description="Where it came from, for diagnostics: 'api' by default.")


class NoteOut(BaseModel):
    id: int
    status: str
    proposed: int
    unmatched: list[str]
    rejected: list[str]
    review_url: str


def _out(entry: models.LogEntry) -> NoteOut:
    return NoteOut(
        id=entry.id,
        status=entry.status,
        proposed=len(json.loads(entry.proposal or "[]")),
        unmatched=json.loads(entry.unmatched or "[]"),
        rejected=json.loads(entry.rejected or "[]"),
        # Returned so whatever posted the note can hand a person somewhere to
        # go. A queued change nobody knows about is a change that never
        # happens.
        review_url="/log?entry_id={}".format(entry.id),
    )


@router.post("", response_model=NoteOut, status_code=201)
def submit_note(payload: NoteIn, db: Session = Depends(get_db)):
    """Queue a note and return what it proposes. Writes nothing to the record.

    201 even when the read failed: the note was stored, which is the promise
    this endpoint makes. A caller that cares can look at ``rejected``, and the
    note is retryable from the page either way. Returning an error would invite
    the caller to retry the *capture*, which is how you get the same note
    logged three times.
    """
    from .ui import _propose_from_note   # local: ui imports models, not routers

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    entry = _propose_from_note(db, text,
                               origin=(payload.origin or "api")[:16])
    return _out(entry)


@router.get("/pending", response_model=list[NoteOut])
def pending_notes(db: Session = Depends(get_db)):
    """Everything waiting on a person. Useful for a nudge on a schedule."""
    entries = (
        db.query(models.LogEntry)
        .filter(models.LogEntry.status == "pending")
        .order_by(models.LogEntry.created_at.desc())
        .all()
    )
    return [_out(e) for e in entries]
