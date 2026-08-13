"""How long the pipeline takes, and where it leaks.

Stdlib-only and ORM-free, the same contract as ``forecast.py``, ``brief.py``
and ``chat.py``: plain dicts in, plain dicts out. That was worth doing here for
a reason beyond testability. Every number on the analytics page is an average
over a handful of records, and an average is exactly the kind of output that
looks authoritative whether or not the arithmetic under it is right. Being able
to assert the whole thing against literals is what keeps it honest.

Two facts about this schema shape everything below.

**Qualification is where applications are born.** ``DEFAULT_STAGE`` is
Qualification, so every record reaches it by construction and "days to
Qualification" is zero for anything created normally. The interval worth
measuring at the front of the funnel is therefore ``Staging -> Qualification``
-- real work, working an angle in before applying -- and it only exists for
records that were actually staged first.

**Reaching a stage implies passing through the ones before it.** History rows
are not guaranteed for every stage: an application created straight at
Discovery has one opening row and no Qualification row, and loops legitimately
skip stages. So reach credits the whole prefix. Without that a later stage can
report more applications than an earlier one, which is not a funnel -- it
produced conversion rates above 100% before it was fixed.

The two ideas pull in opposite directions and both are needed. Prefix credit is
right for *counting* (did this pursuit get that far) and wrong for *timing* (a
stage credited by implication never happened on a date). So timings are
measured only from real dated transitions, and the sample size is reported
everywhere precisely because those two populations differ.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Below this many observations a mean says more about which two records
# happened to have dates than about the process. The page renders "not enough
# data" instead of a number.
#
# Three is not a statistical threshold and is not defended as one -- no honest
# threshold exists at this scale. It is the smallest n at which a single
# outlier cannot *be* the answer, which is the specific way a two-point average
# misleads. The sample size is displayed next to every number that survives the
# cut, because the cut is a floor and not a warranty.
MIN_SAMPLE = 3


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    """Flatten to naive UTC so mixed-awareness rows can be subtracted.

    Form-entered dates come back naive, `_utcnow`-stamped ones aware, and this
    module subtracts one from the other constantly. Same hazard handled in
    `ui.py`, `brief.py` and `chat.py`.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _days(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """Elapsed days, or None if either end is missing or the order is wrong.

    A negative interval is returned as None rather than as a negative number.
    It means the two dates disagree -- typically an `applied_date` typed in
    later than the stage move it supposedly preceded -- and averaging a
    negative duration into a "how long does this take" figure silently drags
    the answer down. Dropping it costs one observation and says so through `n`.
    """
    start, end = _naive(start), _naive(end)
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds() / 86400.0
    return None if delta < 0 else round(delta, 1)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def stats(values: Iterable[Optional[float]], *,
          min_sample: int = MIN_SAMPLE) -> Dict[str, Any]:
    """Summarise a set of durations, refusing to average too few of them.

    Returns `enough=False` with `n` still populated when the sample is under
    the floor, so the page can say "2 records, not enough to average" -- which
    is a more useful sentence than either a number or a blank.

    Both mean and median are returned. The median is the one to lead with at
    this scale (one 90-day pursuit moves a mean of four far more than it moves
    reality), but the mean is what was asked for and the two disagreeing is
    itself information worth being able to see.
    """
    usable = [v for v in values if v is not None]
    out: Dict[str, Any] = {
        "n": len(usable),
        "enough": len(usable) >= min_sample,
        "mean": None, "median": None, "min": None, "max": None,
    }
    if usable:
        out["min"], out["max"] = min(usable), max(usable)
        if out["enough"]:
            out["mean"] = round(_mean(usable), 1)
            out["median"] = round(_median(usable), 1)
    return out


def first_reach(app: Dict[str, Any]) -> Dict[str, datetime]:
    """Earliest *dated* transition into each stage, for one application.

    Earliest rather than latest because a pursuit can re-enter a stage (a
    second onsite, a stage logged twice by hand) and the question every
    interval here asks is when it first got there.
    """
    out: Dict[str, datetime] = {}
    for row in app.get("stage_history") or []:
        to_stage, when = row.get("to_stage"), _naive(row.get("changed_at"))
        if not to_stage or when is None:
            continue
        if to_stage not in out or when < out[to_stage]:
            out[to_stage] = when
    return out


def reached(applications: List[Dict[str, Any]],
            stage_order: Sequence[str]) -> Dict[str, set]:
    """Application ids that ever reached each stage, crediting the prefix.

    Credits both every `to_stage` in history and the current stage, so an
    application whose history was never written still counts. A closed
    application keeps whatever depth its history earned it -- the terminal
    stages are not in `stage_order`, so crediting the current stage is a no-op
    for them rather than a reset.
    """
    order = list(stage_order)
    out: Dict[str, set] = {s: set() for s in order}

    def credit(app_id, stage) -> None:
        if stage in out:
            for s in order[: order.index(stage) + 1]:
                out[s].add(app_id)

    for app in applications:
        app_id = app.get("id")
        for row in app.get("stage_history") or []:
            credit(app_id, row.get("to_stage"))
        credit(app_id, app.get("stage"))
    return out


def funnel(applications: List[Dict[str, Any]],
           stage_order: Sequence[str]) -> List[Dict[str, Any]]:
    """Reach and drop-off per stage, widest first.

    `dropped` is against the previous stage and `share_of_first` scales the
    bar. Both are reported rather than only a conversion percentage, because
    "60%" and "3 of 5" are the same fact and only one of them makes you
    remember how little you are looking at.
    """
    hits = reached(applications, stage_order)
    rows: List[Dict[str, Any]] = []
    first_count = None
    prev = None
    for stage in stage_order:
        ids = hits[stage]
        count = len(ids)
        if first_count is None:
            first_count = count
        rows.append({
            "stage": stage,
            "reached": count,
            "ids": sorted(ids),
            "dropped": None if prev is None else prev - count,
            "conversion_from_prev": (
                None if not prev else round(count / prev, 3)),
            "share_of_first": (
                0.0 if not first_count else round(count / first_count, 4)),
        })
        prev = count
    return rows


# Named intervals. Each returns days or None, and each is a separate function
# rather than a generic "between these two stages" helper because they measure
# genuinely different things: two read stage history, one reads a date column
# you typed, and one ends at either of two terminal stages.

def staging_to_qualification(app: Dict[str, Any]) -> Optional[float]:
    """How long a role sat in Staging before you committed to pursuing it.

    This is the honest version of "application to qualification". Qualification
    is the default stage, so measuring *to* it from record creation is measuring
    nothing; measuring it from Staging measures the work of getting an angle in.
    Only applications actually staged first have this, which is correct -- it is
    absent rather than zero for a role you qualified immediately.
    """
    reach = first_reach(app)
    return _days(reach.get("Staging"), reach.get("Qualification"))


def applied_to_discovery(app: Dict[str, Any]) -> Optional[float]:
    """From the date you submitted to the first time someone engaged.

    Measured off `applied_date` rather than a stage, because applying is a fact
    about the world with a date, while "Qualification" is a column default. The
    cohort is exactly "applications I actually submitted": a recruiter-inbound
    role you never applied to has no applied date and correctly drops out
    rather than counting as an instant zero.
    """
    return _days(app.get("applied_date"), first_reach(app).get("Discovery"))


def applied_to_close(app: Dict[str, Any]) -> Optional[float]:
    """Total cycle: submitted, to closed either way.

    Won and lost are one interval on purpose. They are different outcomes but
    the same question -- how long a pursuit occupies you before it resolves --
    and splitting them at this sample size would leave two numbers too small to
    read instead of one that is merely small.
    """
    reach = first_reach(app)
    ends = [reach.get("Closed Won"), reach.get("Closed Lost")]
    ends = [e for e in ends if e is not None]
    return _days(app.get("applied_date"), min(ends)) if ends else None


INTERVALS = [
    {
        "key": "staging_to_qualification",
        "label": "Staging to Qualification",
        "blurb": "How long you work an angle in before committing to pursue it.",
        "fn": staging_to_qualification,
    },
    {
        "key": "applied_to_discovery",
        "label": "Applied to Discovery",
        "blurb": "From submitting to the first time someone actually engages.",
        "fn": applied_to_discovery,
    },
    {
        "key": "applied_to_close",
        "label": "Applied to closed",
        "blurb": "Full cycle, won or lost.",
        "fn": applied_to_close,
    },
]


def timings(applications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per application, carrying every interval it can answer.

    This is the drill-down. An average hides both the spread and which record
    produced which number, and at a sample size of four the individual rows are
    frankly more informative than the summary above them -- so the table is not
    a supporting detail, it is the part you will actually read.

    Every application appears, including ones contributing nothing, because a
    row of blanks is the visible answer to "why is n only 3".
    """
    rows = []
    for app in applications:
        reach = first_reach(app)
        row = {
            "id": app.get("id"),
            "company": app.get("company"),
            "title": app.get("title"),
            "stage": app.get("stage"),
            "source": app.get("source"),
            "applied_date": _naive(app.get("applied_date")),
            "lost_category": app.get("lost_category"),
            "reached": sorted(reach),
        }
        for spec in INTERVALS:
            row[spec["key"]] = spec["fn"](app)
        rows.append(row)
    # Longest cycle first, then anything without one, so the rows that carry
    # the most information are not below the fold.
    rows.sort(key=lambda r: (r["applied_to_close"] is None,
                             -(r["applied_to_close"] or 0),
                             (r["company"] or "").lower()))
    return rows


def interval_summary(applications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every named interval, summarised, with the contributing ids kept.

    The ids ride along so the page can link a number straight to the records
    behind it. A statistic you cannot open is a statistic you cannot check.
    """
    out = []
    for spec in INTERVALS:
        pairs = [(app.get("id"), spec["fn"](app)) for app in applications]
        contributing = [i for i, v in pairs if v is not None]
        summary = stats(v for _, v in pairs)
        summary.update({
            "key": spec["key"], "label": spec["label"], "blurb": spec["blurb"],
            "ids": contributing,
            "eligible": len(applications),
        })
        out.append(summary)
    return out


def loss_breakdown(applications: List[Dict[str, Any]],
                   *, closed_lost: str = "Closed Lost") -> Dict[str, Any]:
    """Why the lost ones were lost, counted.

    Unset is counted and shown rather than dropped. A breakdown that quietly
    omits the records with no category makes the categorised ones look like the
    whole story, which at this scale is the difference between "mostly comp"
    and "one of my six losses was about comp".
    """
    lost = [a for a in applications if a.get("stage") == closed_lost]
    counts: Dict[str, List[int]] = {}
    for app in lost:
        key = app.get("lost_category") or "Not recorded"
        counts.setdefault(key, []).append(app.get("id"))
    rows = [{"category": k, "count": len(v), "ids": v}
            for k, v in counts.items()]
    # Commonest first; "Not recorded" is pinned last regardless of size,
    # because it is an absence of an answer rather than one of the answers.
    rows.sort(key=lambda r: (r["category"] == "Not recorded", -r["count"],
                             r["category"]))
    return {"total_lost": len(lost), "rows": rows}


def overview(applications: List[Dict[str, Any]],
             stage_order: Sequence[str]) -> Dict[str, Any]:
    """Everything the analytics page renders, in one call.

    `off_funnel` is the gap between the total and the funnel's widest rung, and
    it exists because that gap otherwise reads as a bug. An application sitting
    in Staging has not entered the funnel yet, and one closed out with no
    history rows never earned a rung -- both are correct, and both make the
    first bar smaller than the application count sitting above it. Naming the
    difference is the cheapest way to stop it looking like something is being
    dropped silently.
    """
    rows = funnel(applications, stage_order)
    widest = rows[0]["reached"] if rows else 0
    return {
        "total": len(applications),
        "applied": sum(1 for a in applications if a.get("applied_date")),
        "off_funnel": len(applications) - widest,
        "funnel": rows,
        "intervals": interval_summary(applications),
        "timings": timings(applications),
        "losses": loss_breakdown(applications),
        "min_sample": MIN_SAMPLE,
    }
