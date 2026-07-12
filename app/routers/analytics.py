"""Analytics endpoints — the funnel and traction views that justify a real
database over a flat tracker. Built on Stage History, not current-stage
snapshots, so they measure *movement* rather than a moment in time.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/funnel")
def funnel(db: Session = Depends(get_db)):
    """How many applications ever *reached* each stage, plus step conversion.

    Uses StageHistory so an application that has already moved past a stage
    still counts toward that stage — a true funnel, not a current snapshot.
    """
    reached: dict[str, set[int]] = {s.value: set() for s in models.STAGE_ORDER}
    rows = db.query(models.StageHistory).all()
    for row in rows:
        if row.to_stage in models.STAGE_ORDER:
            reached[row.to_stage.value].add(row.application_id)
        # Reaching a later stage implies you passed through earlier ones.
    # Also credit applications for their current stage (covers the opening row).
    for app_obj in db.query(models.JobApplication).all():
        if app_obj.stage in models.STAGE_ORDER:
            idx = models.STAGE_ORDER.index(app_obj.stage)
            for s in models.STAGE_ORDER[: idx + 1]:
                reached[s.value].add(app_obj.id)

    result = []
    prev_count = None
    for s in models.STAGE_ORDER:
        count = len(reached[s.value])
        conv = None if prev_count in (None, 0) else round(count / prev_count, 3)
        result.append(
            {"stage": s.value, "reached": count, "conversion_from_prev": conv}
        )
        prev_count = count
    return {"funnel": result}


@router.get("/resume-traction")
def resume_traction(db: Session = Depends(get_db)):
    """For each resume version, how far its applications have progressed."""
    out = []
    for resume in db.query(models.Resume).all():
        apps = resume.applications
        if not apps:
            out.append({"resume": resume.label, "applications": 0})
            continue
        furthest = max(
            (models.STAGE_ORDER.index(a.stage) for a in apps if a.stage in models.STAGE_ORDER),
            default=-1,
        )
        offers = sum(1 for a in apps if a.stage == models.Stage.OFFER)
        out.append(
            {
                "resume": resume.label,
                "applications": len(apps),
                "furthest_stage": models.STAGE_ORDER[furthest].value if furthest >= 0 else None,
                "offers": offers,
            }
        )
    return {"resume_traction": out}
