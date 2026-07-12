"""Job Posting (Product) endpoints, including the sourcing rating loop."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services.scrape import scrape_job

router = APIRouter(prefix="/api/postings", tags=["postings"])


class ScrapeRequest(BaseModel):
    url: str


@router.post("/scrape")
def scrape_posting(payload: ScrapeRequest, db: Session = Depends(get_db)):
    """Fetch a job-posting URL and return extracted fields to pre-fill the form.

    Does NOT create a posting — it only reads the page and hands the parsed
    fields back to the UI. If the detected company already exists, we return its
    id so the form can auto-select it.
    """
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(400, "No URL provided")
    try:
        fields = scrape_job(url)
    except Exception as exc:  # network error, 4xx/5xx, etc.
        raise HTTPException(502, f"Couldn't fetch that page: {exc}")

    matched_id = None
    name = fields.get("company_name")
    if name:
        existing = (
            db.query(models.Company)
            .filter(models.Company.name.ilike(name))
            .first()
        )
        if existing:
            matched_id = existing.id
    fields["matched_company_id"] = matched_id
    return fields


@router.post("", response_model=schemas.JobPostingRead, status_code=201)
def create_posting(payload: schemas.JobPostingCreate, db: Session = Depends(get_db)):
    if not db.get(models.Company, payload.company_id):
        raise HTTPException(400, "company_id does not exist")
    posting = models.JobPosting(**payload.model_dump())
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


@router.get("", response_model=list[schemas.JobPostingRead])
def list_postings(
    rating: Optional[models.Rating] = None,
    unrated: bool = False,
    db: Session = Depends(get_db),
):
    q = db.query(models.JobPosting)
    if unrated:
        q = q.filter(models.JobPosting.my_rating.is_(None))
    elif rating is not None:
        q = q.filter(models.JobPosting.my_rating == rating)
    return q.order_by(models.JobPosting.last_seen_at.desc()).all()


@router.get("/{posting_id}", response_model=schemas.JobPostingRead)
def get_posting(posting_id: int, db: Session = Depends(get_db)):
    posting = db.get(models.JobPosting, posting_id)
    if not posting:
        raise HTTPException(404, "Posting not found")
    return posting


@router.post("/{posting_id}/rate", response_model=schemas.JobPostingRead)
def rate_posting(
    posting_id: int, payload: schemas.RatePosting, db: Session = Depends(get_db)
):
    """Top-level sourcing feedback loop: thumbs up/down/neutral + reason."""
    posting = db.get(models.JobPosting, posting_id)
    if not posting:
        raise HTTPException(404, "Posting not found")
    posting.my_rating = payload.rating
    posting.rating_reason = payload.reason
    posting.rated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(posting)
    return posting


@router.delete("/{posting_id}", status_code=204)
def delete_posting(posting_id: int, db: Session = Depends(get_db)):
    posting = db.get(models.JobPosting, posting_id)
    if not posting:
        raise HTTPException(404, "Posting not found")
    db.delete(posting)
    db.commit()
