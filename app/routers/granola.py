"""JSON endpoints backing the Granola import UI on the Meetings page."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..services import granola

router = APIRouter(prefix="/api/granola", tags=["granola"])


@router.get("/status")
def status():
    return {"enabled": granola.enabled()}


@router.get("/notes")
def notes(limit: int = 25):
    if not granola.enabled():
        raise HTTPException(400, "Granola API key not set (GRANOLA_API_KEY).")
    try:
        return {"notes": granola.list_notes(limit)}
    except Exception as exc:
        raise HTTPException(502, f"Granola request failed: {exc}")


@router.get("/notes/{note_id}")
def note(note_id: str):
    if not granola.enabled():
        raise HTTPException(400, "Granola API key not set (GRANOLA_API_KEY).")
    try:
        return granola.get_note(note_id)
    except Exception as exc:
        raise HTTPException(502, f"Granola request failed: {exc}")
