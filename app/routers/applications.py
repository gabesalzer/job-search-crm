"""Job Application (Opportunity) endpoints, including stage transitions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/applications", tags=["applications"])


@router.post("", response_model=schemas.JobApplicationRead, status_code=201)
def create_application(
    payload: schemas.JobApplicationCreate, db: Session = Depends(get_db)
):
    if not db.get(models.Company, payload.company_id):
        raise HTTPException(400, "company_id does not exist")
    if payload.job_posting_id and not db.get(models.JobPosting, payload.job_posting_id):
        raise HTTPException(400, "job_posting_id does not exist")
    if payload.resume_id and not db.get(models.Resume, payload.resume_id):
        raise HTTPException(400, "resume_id does not exist")

    app_obj = models.JobApplication(**payload.model_dump())
    db.add(app_obj)
    # The stage 'set' event records the opening StageHistory row on flush.
    db.commit()
    db.refresh(app_obj)
    return app_obj


@router.get("", response_model=list[schemas.JobApplicationRead])
def list_applications(
    stage: Optional[models.Stage] = None, db: Session = Depends(get_db)
):
    q = db.query(models.JobApplication)
    if stage is not None:
        q = q.filter(models.JobApplication.stage == stage)
    return q.order_by(models.JobApplication.last_activity_date.desc()).all()


@router.get("/{application_id}", response_model=schemas.JobApplicationRead)
def get_application(application_id: int, db: Session = Depends(get_db)):
    app_obj = db.get(models.JobApplication, application_id)
    if not app_obj:
        raise HTTPException(404, "Application not found")
    return app_obj


@router.get("/{application_id}/history", response_model=list[schemas.StageHistoryRead])
def get_history(application_id: int, db: Session = Depends(get_db)):
    app_obj = db.get(models.JobApplication, application_id)
    if not app_obj:
        raise HTTPException(404, "Application not found")
    return app_obj.stage_history


@router.post("/{application_id}/stage", response_model=schemas.JobApplicationRead)
def change_stage(
    application_id: int, payload: schemas.ChangeStage, db: Session = Depends(get_db)
):
    """Middle-level pipeline loop. Changing stage auto-writes StageHistory."""
    app_obj = db.get(models.JobApplication, application_id)
    if not app_obj:
        raise HTTPException(404, "Application not found")
    app_obj.stage = payload.stage  # triggers the history event listener
    app_obj.last_activity_date = datetime.now(timezone.utc)
    if payload.stage == models.Stage.CLOSED_LOST:
        app_obj.lost_reason = payload.lost_reason
    else:
        app_obj.lost_reason = None
    db.commit()
    db.refresh(app_obj)
    return app_obj
