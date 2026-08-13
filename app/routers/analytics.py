"""Analytics endpoints — the funnel and traction views that justify a real
database over a flat tracker. Built on Stage History, not current-stage
snapshots, so they measure *movement* rather than a moment in time.

The arithmetic itself lives in `app/analytics.py`, which is stdlib-only and
knows nothing about SQLAlchemy. This module is the adapter: it walks the ORM,
hands plain dicts over, and returns what comes back.

That split arrived late and fixed a real problem. The funnel maths used to live
here, inline, and `/analytics` (the page) would have needed its own copy — two
implementations of "how many applications reached Discovery" that could drift
apart and disagree on screen, in a tool whose whole purpose is telling you the
truth about your own pipeline. There is now one implementation and two
presentations of it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, selectinload

from .. import analytics as analytics_model
from .. import models
from ..database import get_db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

STAGE_ORDER_VALUES = [s.value for s in models.STAGE_ORDER]


def _apps(db: Session):
    """Flatten every application into the shape `analytics.py` consumes.

    Mirrors `ui._analytics_apps`. The duplication is two dict literals and is
    deliberate: the alternative is importing a private helper out of the UI
    router into the API router, which couples the two presentations together in
    exactly the direction that makes the JSON API hostage to a template change.
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
    return apps, [
        {
            "id": a.id,
            "company": a.company.name if a.company else None,
            "title": a.title,
            "stage": a.stage.value if a.stage else None,
            "source": a.source.value if a.source else None,
            "applied_date": analytics_model._naive(a.applied_date),
            "created_at": analytics_model._naive(a.created_at),
            "lost_category": a.lost_category.value if a.lost_category else None,
            "stage_history": [
                {
                    "from_stage": h.from_stage.value if h.from_stage else None,
                    "to_stage": h.to_stage.value if h.to_stage else None,
                    "changed_at": analytics_model._naive(h.changed_at),
                }
                for h in a.stage_history
            ],
        }
        for a in apps
    ]


@router.get("/funnel")
def funnel(db: Session = Depends(get_db)):
    """How many applications ever *reached* each stage, plus step conversion.

    Uses StageHistory so an application that has already moved past a stage
    still counts toward that stage — a true funnel, not a current snapshot.
    """
    _, payload = _apps(db)
    rows = analytics_model.funnel(payload, STAGE_ORDER_VALUES)
    # `ids` is dropped from the API response: it is a UI affordance (click a bar,
    # see the records) and returning it here would make every consumer of this
    # endpoint carry a list that grows with the pipeline.
    return {"funnel": [{k: v for k, v in r.items() if k != "ids"} for r in rows]}


@router.get("/durations")
def durations(db: Session = Depends(get_db)):
    """The named intervals, summarised, with the sample size behind each.

    `enough` is false when fewer than `min_sample` observations exist, and in
    that case `mean` and `median` are null rather than computed. A consumer
    that ignores the flag still cannot accidentally render a two-point average,
    because there is no number there to render — the suppression is in the
    data, not in the template.
    """
    _, payload = _apps(db)
    return {
        "min_sample": analytics_model.MIN_SAMPLE,
        "intervals": [
            {k: v for k, v in row.items() if k != "ids"}
            for row in analytics_model.interval_summary(payload)
        ],
    }


@router.get("/losses")
def losses(db: Session = Depends(get_db)):
    """Why the closed-lost applications were lost, counted by category."""
    _, payload = _apps(db)
    breakdown = analytics_model.loss_breakdown(payload)
    return {
        "total_lost": breakdown["total_lost"],
        "rows": [{k: v for k, v in r.items() if k != "ids"}
                 for r in breakdown["rows"]],
    }


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
    _, payload = _apps(db)
    cohort = [a for a in payload if a.get("applied_date") is not None]
    cohort_ids = {a["id"] for a in cohort}
    denominator = len(cohort)

    reached = analytics_model.reached(payload, STAGE_ORDER_VALUES)

    stages = []
    for stage in STAGE_ORDER_VALUES:
        if stage == models.Stage.QUALIFICATION.value:
            continue  # see docstring: not an achievement, it's the default
        hit = reached[stage] & cohort_ids
        gaps = []
        for app in cohort:
            when = analytics_model.first_reach(app).get(stage)
            gap = analytics_model._days(app["applied_date"], when)
            if gap is not None:
                gaps.append(gap)
        summary = analytics_model.stats(gaps)
        stages.append({
            "stage": stage,
            "reached": len(hit),
            "conversion_from_applied": (
                None if denominator == 0 else round(len(hit) / denominator, 3)),
            "median_days_from_applied": summary["median"],
            "mean_days_from_applied": summary["mean"],
            "timed_sample": summary["n"],
            "enough_to_average": summary["enough"],
        })

    return {
        "applied_count": denominator,
        "unapplied_count": len(payload) - denominator,
        "min_sample": analytics_model.MIN_SAMPLE,
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
