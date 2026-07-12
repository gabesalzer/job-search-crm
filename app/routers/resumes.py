"""Resume endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


@router.post("", response_model=schemas.ResumeRead, status_code=201)
def create_resume(payload: schemas.ResumeCreate, db: Session = Depends(get_db)):
    resume = models.Resume(**payload.model_dump())
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


@router.get("", response_model=list[schemas.ResumeRead])
def list_resumes(db: Session = Depends(get_db)):
    return db.query(models.Resume).order_by(models.Resume.created_at.desc()).all()


@router.get("/{resume_id}", response_model=schemas.ResumeRead)
def get_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.get(models.Resume, resume_id)
    if not resume:
        raise HTTPException(404, "Resume not found")
    return resume
