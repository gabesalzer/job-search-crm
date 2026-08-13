"""Durations, the funnel, and the refusal to average two records.

Stdlib only, against the real `app/analytics.py` rather than a mirror of it.
That distinction matters more here than anywhere else in this suite: an average
looks equally authoritative whether or not the arithmetic behind it is right,
so this is the module where a hand-mirrored test would be least likely to catch
a drift and most costly when it didn't.

Run: python3 tests/test_analytics.py
"""
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import analytics  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


STAGE_ORDER = ["Qualification", "Discovery", "Takehome",
               "Executive Signoff", "Negotiation", "Closed Won"]


def hist(to_stage, when, from_stage=None):
    return {"from_stage": from_stage, "to_stage": to_stage, "changed_at": when}


def app(app_id, **kw):
    base = {"id": app_id, "company": "Co{}".format(app_id), "title": "Role",
            "stage": "Discovery", "source": None, "applied_date": None,
            "lost_category": None, "stage_history": []}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# _days: the guard rails
# --------------------------------------------------------------------------- #
check("a plain interval is measured in days",
      analytics._days(datetime(2026, 6, 1), datetime(2026, 6, 11)) == 10.0)
check("a missing end returns None, not zero",
      analytics._days(datetime(2026, 6, 1), None) is None)
check("a missing start returns None, not zero",
      analytics._days(None, datetime(2026, 6, 1)) is None)
check("a backwards interval is dropped rather than counted as negative",
      analytics._days(datetime(2026, 6, 11), datetime(2026, 6, 1)) is None,
      "a negative duration silently drags a 'how long does this take' average down")
check("an aware end and a naive start still subtract",
      analytics._days(datetime(2026, 6, 1),
                      datetime(2026, 6, 11, tzinfo=timezone.utc)) == 10.0)

# --------------------------------------------------------------------------- #
# stats: the min-3 rule
# --------------------------------------------------------------------------- #
two = analytics.stats([10.0, 20.0])
check("two observations are not averaged", two["mean"] is None and two["median"] is None)
check("but they are still counted", two["n"] == 2 and two["enough"] is False)
check("and their range is still reported",
      (two["min"], two["max"]) == (10.0, 20.0),
      "a range is honest at n=2 in a way a mean is not")

three = analytics.stats([10.0, 20.0, 30.0])
check("three observations are averaged", three["mean"] == 20.0)
check("median is reported alongside the mean", three["median"] == 20.0)
check("three is enough", three["enough"] is True and three["n"] == 3)

check("None values are excluded from n, not counted as zero",
      analytics.stats([10.0, None, 20.0, None])["n"] == 2)
check("an empty set does not raise",
      analytics.stats([])["n"] == 0)

skewed = analytics.stats([1.0, 2.0, 3.0, 200.0])
check("mean and median can disagree, and both are shown",
      skewed["mean"] == 51.5 and skewed["median"] == 2.5,
      "the disagreement is the signal that one record is carrying the mean")

check("the floor is overridable for callers that want a different one",
      analytics.stats([10.0], min_sample=1)["mean"] == 10.0)

# --------------------------------------------------------------------------- #
# first_reach
# --------------------------------------------------------------------------- #
revisit = app(1, stage_history=[
    hist("Discovery", datetime(2026, 5, 1)),
    hist("Takehome", datetime(2026, 5, 10)),
    hist("Discovery", datetime(2026, 5, 20)),   # sent back a stage
])
check("re-entering a stage keeps the first arrival, not the latest",
      analytics.first_reach(revisit)["Discovery"] == datetime(2026, 5, 1))
check("an undated history row is skipped rather than crashing",
      analytics.first_reach(app(2, stage_history=[hist("Discovery", None)])) == {})

# --------------------------------------------------------------------------- #
# reached: prefix crediting
# --------------------------------------------------------------------------- #
skipper = app(1, stage="Negotiation", stage_history=[
    hist("Qualification", datetime(2026, 5, 1)),
    hist("Negotiation", datetime(2026, 6, 1)),   # skipped two stages outright
])
hits = analytics.reached([skipper], STAGE_ORDER)
check("skipping a stage still credits it",
      1 in hits["Takehome"] and 1 in hits["Executive Signoff"],
      "otherwise a later stage can report more applications than an earlier one")
check("a stage past the furthest reached is not credited",
      1 not in hits["Closed Won"])

no_history = app(2, stage="Takehome", stage_history=[])
check("current stage is credited when history was never written",
      2 in analytics.reached([no_history], STAGE_ORDER)["Takehome"])

closed = app(3, stage="Closed Lost", stage_history=[
    hist("Qualification", datetime(2026, 5, 1)),
    hist("Discovery", datetime(2026, 5, 15)),
])
closed_hits = analytics.reached([closed], STAGE_ORDER)
check("a closed-lost application keeps the depth its history earned",
      3 in closed_hits["Discovery"] and 3 not in closed_hits["Takehome"],
      "Closed Lost is not a rung, so it must neither add nor reset depth")

# --------------------------------------------------------------------------- #
# funnel
# --------------------------------------------------------------------------- #
cohort = [
    app(1, stage="Negotiation", stage_history=[hist("Negotiation", datetime(2026, 6, 1))]),
    app(2, stage="Discovery", stage_history=[hist("Discovery", datetime(2026, 6, 1))]),
    app(3, stage="Discovery", stage_history=[hist("Discovery", datetime(2026, 6, 1))]),
    app(4, stage="Qualification", stage_history=[hist("Qualification", datetime(2026, 6, 1))]),
]
rows = {r["stage"]: r for r in analytics.funnel(cohort, STAGE_ORDER)}
check("the funnel never widens as it deepens",
      [rows[s]["reached"] for s in STAGE_ORDER] == [4, 3, 1, 1, 1, 0],
      "monotonically non-increasing is the defining property of a funnel")
check("drop-off is measured against the previous stage",
      rows["Discovery"]["dropped"] == 1 and rows["Takehome"]["dropped"] == 2)
check("the first stage has no drop-off to report",
      rows["Qualification"]["dropped"] is None)
check("bar width is a share of the widest rung",
      rows["Qualification"]["share_of_first"] == 1.0
      and rows["Discovery"]["share_of_first"] == 0.75)
check("the ids behind each bar are kept for drill-down",
      rows["Discovery"]["ids"] == [1, 2, 3])
check("conversion is not computed from a zero denominator",
      rows["Closed Won"]["conversion_from_prev"] == 0.0
      and analytics.funnel([], STAGE_ORDER)[1]["conversion_from_prev"] is None)

# --------------------------------------------------------------------------- #
# The named intervals
# --------------------------------------------------------------------------- #
staged = app(1, applied_date=datetime(2026, 6, 1), stage_history=[
    hist("Staging", datetime(2026, 5, 20)),
    hist("Qualification", datetime(2026, 5, 30), "Staging"),
    hist("Discovery", datetime(2026, 6, 11), "Qualification"),
])
check("Staging to Qualification measures the work before applying",
      analytics.staging_to_qualification(staged) == 10.0)
check("Applied to Discovery is measured off applied_date, not a stage",
      analytics.applied_to_discovery(staged) == 10.0)

never_staged = app(2, applied_date=datetime(2026, 6, 1), stage_history=[
    hist("Qualification", datetime(2026, 6, 1)),
])
check("an application that was never staged has no staging interval, not zero",
      analytics.staging_to_qualification(never_staged) is None,
      "absent and zero are different claims and the average must not see a zero")

no_applied = app(3, applied_date=None, stage_history=[
    hist("Discovery", datetime(2026, 6, 11)),
])
check("a role you never applied to drops out of the applied cohort",
      analytics.applied_to_discovery(no_applied) is None)

won = app(4, applied_date=datetime(2026, 4, 1), stage_history=[
    hist("Closed Won", datetime(2026, 5, 1)),
])
lost = app(5, applied_date=datetime(2026, 4, 1), stage_history=[
    hist("Closed Lost", datetime(2026, 4, 21)),
])
check("cycle time ends at Closed Won", analytics.applied_to_close(won) == 30.0)
check("cycle time ends at Closed Lost too", analytics.applied_to_close(lost) == 20.0)

both = app(6, applied_date=datetime(2026, 4, 1), stage_history=[
    hist("Closed Lost", datetime(2026, 4, 21)),
    hist("Closed Won", datetime(2026, 5, 1)),   # reopened, then won
])
check("a reopened pursuit closes at the first terminal date",
      analytics.applied_to_close(both) == 20.0)

check("an open pursuit has no cycle time",
      analytics.applied_to_close(app(7, applied_date=datetime(2026, 4, 1))) is None)

# --------------------------------------------------------------------------- #
# interval_summary and loss_breakdown
# --------------------------------------------------------------------------- #
summary = {s["key"]: s for s in analytics.interval_summary([staged, never_staged, no_applied])}
check("the summary reports how many records could answer each interval",
      summary["staging_to_qualification"]["n"] == 1
      and summary["applied_to_discovery"]["n"] == 1)
check("eligible counts every application, not just the contributors",
      summary["applied_to_discovery"]["eligible"] == 3,
      "'1 of 3' is the sentence; both halves are needed to write it")
check("contributing ids are kept so a number can be opened",
      summary["staging_to_qualification"]["ids"] == [1])

losses = analytics.loss_breakdown([
    app(1, stage="Closed Lost", lost_category="Compensation gap"),
    app(2, stage="Closed Lost", lost_category="Compensation gap"),
    app(3, stage="Closed Lost", lost_category=None),
    app(4, stage="Discovery", lost_category="Compensation gap"),
])
check("only currently-lost applications are counted",
      losses["total_lost"] == 3,
      "a stale category on a live pursuit must not be counted as a loss")
check("uncategorised losses are shown, not dropped",
      any(r["category"] == "Not recorded" and r["count"] == 1
          for r in losses["rows"]))
check("Not recorded sorts last however common it is",
      losses["rows"][-1]["category"] == "Not recorded")
check("categories are ordered commonest first",
      losses["rows"][0]["category"] == "Compensation gap"
      and losses["rows"][0]["count"] == 2)

# --------------------------------------------------------------------------- #
# timings and overview
# --------------------------------------------------------------------------- #
rows = analytics.timings([staged, never_staged, no_applied])
check("every application gets a row, including ones contributing nothing",
      len(rows) == 3,
      "a row of blanks is the visible answer to 'why is n only 1'")
check("rows carry every interval they can answer",
      rows[0]["applied_to_discovery"] is not None or rows[0]["id"] != 1)

over = analytics.overview([staged, never_staged, no_applied], STAGE_ORDER)
check("overview reports the applied cohort size", over["applied"] == 2)
check("off_funnel names the gap between the total and the widest rung",
      over["off_funnel"] == over["total"] - over["funnel"][0]["reached"])

empty = analytics.overview([], STAGE_ORDER)
check("an empty pipeline renders without raising",
      empty["total"] == 0 and empty["off_funnel"] == 0)
check("an empty pipeline still describes every stage",
      len(empty["funnel"]) == len(STAGE_ORDER))
check("an empty pipeline suppresses every interval",
      all(iv["enough"] is False for iv in empty["intervals"]))

print("\n{} failed".format(len(failures)) if failures else "\nall passed")
sys.exit(1 if failures else 0)
