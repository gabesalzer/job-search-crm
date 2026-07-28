"""Analytics endpoints — the funnel and traction views that justify a real
database over a flat tracker. Built on Stage History, not current-stage
snapshots, so they measure *movement* rather than a moment in time.
"""
from __future__ import annotations

from statistics import median

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _reached_sets(db: Session) -> dict[str, set[int]]:
    """Application IDs that ever reached each stage in STAGE_ORDER.

    Reaching a stage implies having passed through every stage before it, so
    both sources below credit the whole prefix rather than the single stage.

    That prefix rule is load-bearing, not defensive. Applications don't
    reliably have a history row for every stage they passed through: an
    application created directly at Discovery has an opening row of
    `None → Discovery` and no Qualification row at all, and a stage can be
    skipped outright (the model explicitly allows it — plenty of loops have no
    Takehome). Crediting only `to_stage` would then let a *later* stage report
    more applications than an *earlier* one, which is not a funnel — it
    produced conversion rates above 100% before this was fixed.
    """
    reached: dict[str, set[int]] = {s.value: set() for s in models.STAGE_ORDER}

    def credit(app_id: int, stage) -> None:
        if stage in models.STAGE_ORDER:
            idx = models.STAGE_ORDER.index(stage)
            for s in models.STAGE_ORDER[: idx + 1]:
                reached[s.value].add(app_id)

    for row in db.query(models.StageHistory).all():
        credit(row.application_id, row.to_stage)
    # Also credit the current stage, which covers an application whose history
    # was never written (and closed applications, whose terminal stage isn't in
    # STAGE_ORDER, keep whatever depth their history already earned them).
    for app_obj in db.query(models.JobApplication).all():
        credit(app_obj.id, app_obj.stage)
    return reached


@router.get("/funnel")
def funnel(db: Session = Depends(get_db)):
    """How many applications ever *reached* each stage, plus step conversion.

    Uses StageHistory so an application that has already moved past a stage
    still counts toward that stage — a true funnel, not a current snapshot.
    """
    reached = _reached_sets(db)

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


@router.get("/applied-conversion")
def applied_conversion(db: Session = Depends(get_db)):
    """Conversion and elapsed time measured from the date you actually applied.

    Why this exists instead of an `Applied` stage
    ---------------------------------------------
    Every application is *born* at Qualification (it's the column default), so
    Qualification is a starting state rather than something a pursuit reaches.
    That makes it useless as a funnel denominator — 100% of applications
    "reach" it by construction, and the timestamp on its StageHistory row is
    when you created the record, not when anything happened. Adding an
    `Applied` stage in front of it would just relocate the problem: records
    would be born at `Applied` and that would become the meaningless one.

    A nullable `applied_date` column is strictly better here. It's a fact about
    the world (a submission either happened on a date or didn't), it's absent
    exactly when it should be — a recruiter-inbound role you never applied to
    has no applied date, and correctly drops out of this cohort — and it can be
    backfilled for an application whose early stages were never logged.

    The cohort is therefore "applications I actually submitted," and the first
    stage that means anything is Discovery: the first one you have to be let
    into. `days_from_applied` is measured off real StageHistory transitions
    only, so an application credited with a stage purely by implication
    contributes to `reached` but not to the timing.
    """
    apps = db.query(models.JobApplication).all()
    cohort = {a.id: a for a in apps if a.applied_date is not None}

    # First real dated transition into each stage, per application.
    first_reach: dict[tuple[int, str], object] = {}
    for row in db.query(models.StageHistory).all():
        if row.application_id not in cohort or row.changed_at is None:
            continue
        if row.to_stage not in models.STAGE_ORDER:
            continue
        key = (row.application_id, row.to_stage.value)
        if key not in first_reach or row.changed_at < first_reach[key]:
            first_reach[key] = row.changed_at

    reached = _reached_sets(db)
    applied_ids = set(cohort)
    denominator = len(applied_ids)

    stages = []
    for s in models.STAGE_ORDER:
        if s == models.Stage.QUALIFICATION:
            continue  # see docstring: not an achievement, it's the default
        hit = reached[s.value] & applied_ids
        gaps = []
        for app_id in hit:
            when = first_reach.get((app_id, s.value))
            if when is not None:
                delta = when - cohort[app_id].applied_date
                gaps.append(round(delta.total_seconds() / 86400, 1))
        stages.append({
            "stage": s.value,
            "reached": len(hit),
            "conversion_from_applied": (
                None if denominator == 0 else round(len(hit) / denominator, 3)
            ),
            "median_days_from_applied": round(median(gaps), 1) if gaps else None,
            "timed_sample": len(gaps),
        })

    return {
        "applied_count": denominator,
        "unapplied_count": len(apps) - denominator,
        "stages": stages,
    }


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
        reached_negotiation = sum(1 for a in apps if a.stage == models.Stage.NEGOTIATION)
        out.append(
            {
                "resume": resume.label,
                "applications": len(apps),
                "furthest_stage": models.STAGE_ORDER[furthest].value if furthest >= 0 else None,
                "reached_negotiation": reached_negotiation,
            }
        )
    return {"resume_traction": out}
