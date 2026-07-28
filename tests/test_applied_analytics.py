"""Applied-date analytics and the hand-correctable timestamp rules.

Stdlib only. FastAPI/SQLAlchemy aren't installable in the environment these
were written in, so the logic under test is hand-mirrored from
`app/routers/analytics.py` and `app/routers/ui.py`. That's a real duplication
risk, so the mirrors below are kept deliberately literal — if you change the
originals, change these in the same commit.
"""
from datetime import datetime, timedelta
from statistics import median

failures = []
checks = 0


def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
# Mirror of ui._same_to_the_minute
# --------------------------------------------------------------------------- #
def same_to_the_minute(a, b):
    if a is None or b is None:
        return a is b
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


# A datetime-local input can't express seconds, so a value that round-trips
# through the form is "unchanged" even though it isn't equal.
stored = datetime(2026, 7, 12, 16, 30, 47, 123456)
round_tripped = datetime(2026, 7, 12, 16, 30)
check("round trip reads as unchanged", same_to_the_minute(round_tripped, stored), True)
check("raw equality would have been wrong", round_tripped == stored, False)
check(
    "a real edit is detected",
    same_to_the_minute(datetime(2026, 7, 11, 9, 0), stored),
    False,
)
check("null vs value is a difference", same_to_the_minute(None, stored), False)
check("null vs null is not an edit", same_to_the_minute(None, None), True)


# --------------------------------------------------------------------------- #
# Mirror of ui.update_application_ui's timestamp block
# --------------------------------------------------------------------------- #
def apply_timestamps(app, form, now, stage_changed=False):
    """Returns the new (created_at, last_activity_date, updated_at)."""
    created = app["created_at"]
    activity = app["last_activity_date"]
    updated = app["updated_at"]

    if stage_changed:
        activity = now  # the automatic stamp the stage block sets

    if form.get("created_at"):
        created = form["created_at"]
    if form.get("last_activity_date"):
        activity = form["last_activity_date"]

    new_updated = form.get("updated_at")
    if new_updated and not same_to_the_minute(new_updated, updated):
        updated = new_updated
    else:
        updated = now  # onupdate=_utcnow fires when we don't set the column
    return created, activity, updated


NOW = datetime(2026, 7, 28, 12, 0)
base = {
    "created_at": datetime(2026, 6, 28, 9, 0),
    "last_activity_date": datetime(2026, 7, 10, 15, 0),
    "updated_at": datetime(2026, 7, 12, 16, 30, 47),
}

# Plain save: updated_at must advance, not freeze at the value the form echoed.
_, _, updated = apply_timestamps(base, {"updated_at": datetime(2026, 7, 12, 16, 30)}, NOW)
check("unchanged updated_at still advances on save", updated, NOW)

# Deliberate edit: the hand-set value wins over onupdate.
_, _, updated = apply_timestamps(base, {"updated_at": datetime(2026, 5, 1, 8, 0)}, NOW)
check("edited updated_at is honoured", updated, datetime(2026, 5, 1, 8, 0))

# An explicit last_activity_date edit beats the automatic stage-change stamp.
_, activity, _ = apply_timestamps(
    base, {"last_activity_date": datetime(2026, 7, 20, 11, 0)}, NOW, stage_changed=True
)
check("explicit last activity beats stage stamp", activity, datetime(2026, 7, 20, 11, 0))

# ...but with no edit, the stage change stamp still applies.
_, activity, _ = apply_timestamps(base, {}, NOW, stage_changed=True)
check("stage change still stamps last activity", activity, NOW)

# Blank means "leave it alone", not "null it out". Clearing created_at by
# accident would scramble ordering, so an empty field is a no-op.
created, _, _ = apply_timestamps(base, {"created_at": None}, NOW)
check("blank created_at is a no-op", created, base["created_at"])


# --------------------------------------------------------------------------- #
# Mirror of analytics.applied_conversion
# --------------------------------------------------------------------------- #
STAGE_ORDER = [
    "Qualification", "Discovery", "Takehome",
    "Executive Signoff", "Negotiation", "Closed Won",
]


def reached_sets(apps, history):
    """Mirror of analytics._reached_sets."""
    reached = {s: set() for s in STAGE_ORDER}

    def credit(app_id, stage):
        if stage in STAGE_ORDER:
            for s in STAGE_ORDER[: STAGE_ORDER.index(stage) + 1]:
                reached[s].add(app_id)

    for row in history:
        credit(row["application_id"], row["to_stage"])
    for a in apps:
        credit(a["id"], a["stage"])
    return reached


def applied_conversion(apps, history):
    cohort = {a["id"]: a for a in apps if a.get("applied_date") is not None}

    first_reach = {}
    for row in history:
        if row["application_id"] not in cohort or row["changed_at"] is None:
            continue
        if row["to_stage"] not in STAGE_ORDER:
            continue
        key = (row["application_id"], row["to_stage"])
        if key not in first_reach or row["changed_at"] < first_reach[key]:
            first_reach[key] = row["changed_at"]

    reached = reached_sets(apps, history)

    applied_ids = set(cohort)
    denom = len(applied_ids)
    out = []
    for s in STAGE_ORDER:
        if s == "Qualification":
            continue
        hit = reached[s] & applied_ids
        gaps = []
        for app_id in hit:
            when = first_reach.get((app_id, s))
            if when is not None:
                gaps.append(round((when - cohort[app_id]["applied_date"]).total_seconds() / 86400, 1))
        out.append({
            "stage": s,
            "reached": len(hit),
            "conversion_from_applied": None if denom == 0 else round(len(hit) / denom, 3),
            "median_days_from_applied": round(median(gaps), 1) if gaps else None,
            "timed_sample": len(gaps),
        })
    return {"applied_count": denom, "unapplied_count": len(apps) - denom, "stages": out}


D = datetime(2026, 6, 1, 9, 0)
apps = [
    # Applied, got to Discovery after 6 days.
    {"id": 1, "applied_date": D, "stage": "Discovery"},
    # Applied, got to Discovery after 10 days, then Takehome.
    {"id": 2, "applied_date": D, "stage": "Takehome"},
    # Applied, never heard back — still sitting at Qualification.
    {"id": 3, "applied_date": D, "stage": "Qualification"},
    # Recruiter came to us; never applied. Must not affect the cohort at all.
    {"id": 4, "applied_date": None, "stage": "Negotiation"},
]
history = [
    {"application_id": 1, "to_stage": "Qualification", "changed_at": D},
    {"application_id": 1, "to_stage": "Discovery", "changed_at": D + timedelta(days=6)},
    {"application_id": 2, "to_stage": "Qualification", "changed_at": D},
    {"application_id": 2, "to_stage": "Discovery", "changed_at": D + timedelta(days=10)},
    {"application_id": 2, "to_stage": "Takehome", "changed_at": D + timedelta(days=21)},
    {"application_id": 3, "to_stage": "Qualification", "changed_at": D},
    {"application_id": 4, "to_stage": "Negotiation", "changed_at": D + timedelta(days=3)},
]

res = applied_conversion(apps, history)
check("cohort is applications actually submitted", res["applied_count"], 3)
check("inbound role excluded from cohort", res["unapplied_count"], 1)

by_stage = {row["stage"]: row for row in res["stages"]}
check("Qualification is not reported", "Qualification" in by_stage, False)
check("two of three applications reached Discovery", by_stage["Discovery"]["reached"], 2)
check("Discovery conversion", by_stage["Discovery"]["conversion_from_applied"], 0.667)
check("median days to Discovery", by_stage["Discovery"]["median_days_from_applied"], 8.0)
check("Takehome reached", by_stage["Takehome"]["reached"], 1)
check("Takehome timing", by_stage["Takehome"]["median_days_from_applied"], 21.0)

# The inbound application sits at Negotiation, which would drag conversion up
# if the cohort filter leaked. It must contribute nothing.
check("inbound does not inflate Negotiation", by_stage["Negotiation"]["reached"], 0)
check("unreached stage reports no median", by_stage["Negotiation"]["median_days_from_applied"], None)

# An application credited with a stage only by implication has no dated
# transition, so it counts toward `reached` but not toward the timing sample.
apps2 = [{"id": 5, "applied_date": D, "stage": "Takehome"}]
res2 = applied_conversion(apps2, [])
t2 = {r["stage"]: r for r in res2["stages"]}["Takehome"]
check("implied stage still counts as reached", t2["reached"], 1)
check("implied stage contributes no timing", t2["timed_sample"], 0)
check("implied stage median is None", t2["median_days_from_applied"], None)

# --------------------------------------------------------------------------- #
# A funnel has to be monotonic: no stage can report more applications than the
# stage before it, because reaching a stage means you passed through the
# earlier ones. Crediting only `to_stage` broke this for any application whose
# history skips a stage -- which is common, not exotic:
#   * an application created directly at Discovery has an opening history row
#     of None -> Discovery and NO Qualification row at all
#   * the stage model explicitly allows skipping (plenty of loops have no
#     Takehome), so a Discovery -> Executive Signoff jump is legal
# The result was Discovery outnumbering Qualification and conversion above 1.0.
# --------------------------------------------------------------------------- #
lance = [{"id": 1, "applied_date": D, "stage": "Closed Lost"}]
lance_hist = [
    {"application_id": 1, "to_stage": "Discovery", "changed_at": D + timedelta(days=4)},
    {"application_id": 1, "to_stage": "Closed Lost", "changed_at": D + timedelta(days=7)},
]
r = reached_sets(lance, lance_hist)
check("no Qualification row still counts as reaching it", len(r["Qualification"]), 1)
check("closed application keeps the depth it earned", len(r["Discovery"]), 1)
check("closing doesn't credit stages never reached", len(r["Takehome"]), 0)

# Two applications, one of which reached Discovery. Conversion must be 0.5 --
# the old code reported 1.0 here.
mixed = lance + [{"id": 2, "applied_date": D, "stage": "Qualification"}]
mixed_hist = lance_hist + [
    {"application_id": 2, "to_stage": "Qualification", "changed_at": D},
]
r = reached_sets(mixed, mixed_hist)
check("earlier stage is not undercounted", len(r["Qualification"]), 2)
check("Discovery conversion is not inflated", len(r["Discovery"]) / len(r["Qualification"]), 0.5)

# A skipped stage must not create a hole partway down the funnel.
skipper = [{"id": 3, "applied_date": D, "stage": "Executive Signoff"}]
r = reached_sets(skipper, [
    {"application_id": 3, "to_stage": "Discovery", "changed_at": D + timedelta(days=5)},
    {"application_id": 3, "to_stage": "Executive Signoff", "changed_at": D + timedelta(days=30)},
])
check("skipped Takehome still counts as passed through", len(r["Takehome"]), 1)

# Monotonicity as a property, over everything assembled above.
r = reached_sets(mixed + skipper, mixed_hist + [
    {"application_id": 3, "to_stage": "Executive Signoff", "changed_at": D + timedelta(days=30)},
])
counts = [len(r[s]) for s in STAGE_ORDER]
check("funnel never widens as it deepens", counts == sorted(counts, reverse=True), True)

# The same guarantee has to hold through applied_conversion's output.
rows = applied_conversion(mixed + skipper, mixed_hist + [
    {"application_id": 3, "to_stage": "Executive Signoff", "changed_at": D + timedelta(days=30)},
])["stages"]
convs = [r_["conversion_from_applied"] for r_ in rows]
check("reported conversion never increases downstream", convs == sorted(convs, reverse=True), True)
check("conversion never exceeds 1.0", max(convs) <= 1.0, True)

# Empty database must not divide by zero.
res3 = applied_conversion([], [])
check("empty cohort conversion is None", res3["stages"][0]["conversion_from_applied"], None)
check("empty cohort count", res3["applied_count"], 0)


print(f"{checks - len(failures)}/{checks} assertions passed")
for f in failures:
    print("FAIL", f)
raise SystemExit(1 if failures else 0)
