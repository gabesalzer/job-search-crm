"""Granola API client (optional).

Pulls meeting notes from Granola's API (list + fetch-with-transcript). Activates
only when GRANOLA_API_KEY is set — otherwise the app works fine with manual
entry, exactly like the Firecrawl scraper.

Auth is a bearer key (grn_...) generated in Granola's desktop settings (Business
plan). Endpoints: GET /v1/notes and GET /v1/notes/{id}. There are no webhooks,
so this is pull-based. The JSON field names are parsed defensively because we
validate the exact shape against a real response once a key is connected.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

# Base URL overridable via env in case Granola changes it.
API_BASE = os.getenv("GRANOLA_API_BASE", "https://api.granola.ai/v1").rstrip("/")


def _key() -> str:
    return os.getenv("GRANOLA_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def _headers() -> dict:
    return {"Authorization": f"Bearer {_key()}", "Accept": "application/json"}


def list_notes(limit: int = 25) -> list[dict]:
    """Return a lightweight list of recent notes: {id, title, created_at}."""
    resp = httpx.get(
        f"{API_BASE}/notes", headers=_headers(), params={"limit": limit}, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    items = (
        data.get("notes")
        or data.get("data")
        or data.get("results")
        or (data if isinstance(data, list) else [])
    )
    out = []
    for n in items:
        if isinstance(n, dict):
            out.append(
                {
                    "id": n.get("id") or n.get("note_id") or n.get("document_id"),
                    "title": _first(n, "title", "name", "subject") or "Untitled note",
                    "created_at": _first(n, "created_at", "date", "started_at", "createdAt"),
                }
            )
    return [n for n in out if n["id"]]


def get_note(note_id: str) -> dict:
    """Fetch one note with summary + transcript, mapped to our Meeting fields."""
    resp = httpx.get(
        f"{API_BASE}/notes/{note_id}",
        headers=_headers(),
        params={"include_transcript": "true"},
        timeout=45,
    )
    resp.raise_for_status()
    n = resp.json()
    if isinstance(n, dict) and isinstance(n.get("note"), dict):
        n = n["note"]  # some APIs wrap the object
    return {
        "id": n.get("id") or note_id,
        "title": _first(n, "title", "name", "subject"),
        "created_at": _first(n, "created_at", "date", "started_at", "createdAt"),
        "summary": _as_text(_first(n, "summary", "ai_summary", "notes")),
        "transcript": _as_text(_first(n, "transcript", "transcript_text", "content")),
        "link": _first(n, "url", "share_url", "link", "web_url"),
    }


# --------------------------------------------------------------------------- #
def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _as_text(value) -> Optional[str]:
    """Summaries/transcripts may come back as a string, a list of segments, or a
    dict — flatten to plain text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                speaker = seg.get("speaker") or seg.get("name")
                text = _first(seg, "text", "content", "value") or ""
                parts.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(p for p in parts if p).strip() or None
    if isinstance(value, dict):
        return _as_text(_first(value, "text", "content", "value", "markdown"))
    return str(value)
