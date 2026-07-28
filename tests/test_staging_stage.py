"""Design proof for the `Staging` stage (July 2026).

Staging is where a role sits before you've applied to it: the posting exists,
you want it, and you're working the angle in -- finding the referral, warming
the intro. It is a state you occupy and do work in, which is why it's a stage
and not a nullable date column the way `applied_date` is.

It is deliberately **not** in `STAGE_ORDER`, and that's the whole point of this
file. `analytics._reached_sets` credits the entire prefix of stages below
whichever stage an application has reached, and every application is *born* at
`DEFAULT_STAGE`. Those two facts together mean the default stage is reached by
100% of applications by construction -- it can never be a meaningful funnel
denominator. Putting Staging at the front of STAGE_ORDER would move that dead
denominator onto Staging rather than removing it, which is exactly the
objection that keeps `Applied` from being a stage (see
analytics.applied_conversion's docstring).

So the claims proved here are:

1. Adding Staging changes no existing funnel number at all.
2. An application sitting at Staging counts toward nothing -- it hasn't
   entered the pipeline.
3. The monotonicity invariant fixed earlier still holds with Staging present.
4. The metric Staging actually enables ("of the roles I staged, how many
   converted, and how long did the angle take") is computable from real dated
   `Staging -> Qualification` StageHistory rows, with no prefix crediting.

Stdlib only; STAGE_ORDER and the reached-set logic are hand-mirrored from
app/models.py and app/routers/analytics.py. If those change, change these.
"""
from datetime import datetime, timedelta
from statistics import median

# Mirrors models.Stage member order -- this is what the board columns use.
STAGE_VALUES = [
    "Staging", "Qualification", "Discovery", "Takehome",
    "Executive Signoff", "Negotiation", "Closed Won", "Closed Lost",
]

# Mirrors models.STAGE_ORDER. Staging and Closed Lost are both absent.
STAGE_ORDER = [
    "Qualification", "Discovery", "Takehome",
    "Executive Signoff", "Negotiation", "Closed Won",
]

DEFAULT_STAGE = "Qualification"


def reached_sets(apps, history):
    """Mirror of analytics._reached_sets()."""
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


D = datetime(2026, 7, 1, 9, 0)


# --------------------------------------------------------------------------- #
# Where Staging sits
# --------------------------------------------------------------------------- #
def test_staging_is_a_real_stage_you_can_park_a_record_in():
    """Jellyfish and Popl: postings worth pursuing, no application submitted
    yet. Before this stage existed they sat at Qualification, indistinguishable
    from a role applied to that never replied."""
    assert "Staging" in STAGE_VALUES


def test_staging_is_first_on_the_board():
    """Pre-application work comes before pipeline work, so the column reads
    left to right in the order the pursuit actually moves."""
    assert STAGE_VALUES[0] == "Staging"


def test_staging_is_not_a_funnel_rung():
    assert "Staging" not in STAGE_ORDER


def test_closed_lost_precedent_still_holds():
    """Staging isn't a new kind of exception -- STAGE_ORDER has always been a
    subset of Stage, because Closed Lost is a terminal exit rather than a
    depth. Staging is the same shape at the other end."""
    assert "Closed Lost" not in STAGE_ORDER
    assert set(STAGE_ORDER).issubset(STAGE_VALUES)


def test_the_default_stage_is_unchanged_by_adding_staging():
    """The load-bearing one. If Staging became the column default it would be
    reached by 100% of applications by construction and the funnel would have
    simply acquired a new meaningless denominator."""
    assert DEFAULT_STAGE == "Qualification"
    assert DEFAULT_STAGE in STAGE_ORDER


# --------------------------------------------------------------------------- #
# What Staging does to the numbers: nothing
# --------------------------------------------------------------------------- #
def test_a_staged_application_counts_toward_no_stage():
    """It hasn't entered the pipeline. Counting it at Qualification would
    inflate the top of the funnel with roles never actually pursued."""
    apps = [{"id": 1, "stage": "Staging"}]
    history = [{"application_id": 1, "to_stage": "Staging", "changed_at": D}]
    r = reached_sets(apps, history)
    assert all(len(r[s]) == 0 for s in STAGE_ORDER)


def test_staged_records_do_not_dilute_conversion():
    """Two live applications, one of which reached Discovery: 0.5. Adding
    three staged roles must not drag that to 0.2 -- they aren't in the
    denominator because they aren't in the funnel."""
    live = [
        {"id": 1, "stage": "Discovery"},
        {"id": 2, "stage": "Qualification"},
    ]
    staged = [{"id": i, "stage": "Staging"} for i in (3, 4, 5)]
    r_live = reached_sets(live, [])
    r_all = reached_sets(live + staged, [])
    assert len(r_live["Discovery"]) / len(r_live["Qualification"]) == 0.5
    assert len(r_all["Discovery"]) / len(r_all["Qualification"]) == 0.5


def test_history_rows_naming_staging_are_ignored_by_the_funnel():
    """A record that passed through Staging on its way in carries a Staging
    history row forever. The funnel must skip it rather than raise on a stage
    it can't index."""
    apps = [{"id": 1, "stage": "Discovery"}]
    history = [
        {"application_id": 1, "to_stage": "Staging", "changed_at": D},
        {"application_id": 1, "to_stage": "Qualification", "changed_at": D + timedelta(days=6)},
        {"application_id": 1, "to_stage": "Discovery", "changed_at": D + timedelta(days=13)},
    ]
    r = reached_sets(apps, history)
    assert len(r["Qualification"]) == 1
    assert len(r["Discovery"]) == 1


def test_monotonicity_still_holds_with_staged_records_present():
    """The invariant fixed when Qualification/Discovery got crossed: no stage
    may report more applications than the one before it. A new stage is exactly
    the kind of change that could break it again."""
    apps = [
        {"id": 1, "stage": "Staging"},
        {"id": 2, "stage": "Staging"},
        {"id": 3, "stage": "Qualification"},
        {"id": 4, "stage": "Discovery"},
        {"id": 5, "stage": "Negotiation"},
        {"id": 6, "stage": "Closed Lost"},
    ]
    history = [
        {"application_id": 1, "to_stage": "Staging", "changed_at": D},
        {"application_id": 4, "to_stage": "Discovery", "changed_at": D},
        {"application_id": 6, "to_stage": "Discovery", "changed_at": D},
        {"application_id": 6, "to_stage": "Closed Lost", "changed_at": D + timedelta(days=9)},
    ]
    counts = [len(reached_sets(apps, history)[s]) for s in STAGE_ORDER]
    assert counts == sorted(counts, reverse=True)


# --------------------------------------------------------------------------- #
# The metric Staging enables, built the honest way
# --------------------------------------------------------------------------- #
def staged_conversion(history):
    """Of the applications with a real dated `-> Staging` row, how many later
    got a real dated row moving them into the pipeline, and how long did that
    take?

    Deliberately built on dated transitions rather than on prefix crediting.
    A recruiter-inbound role never passes through Staging and must not be
    counted as a staged role that converted instantly, which is precisely what
    an implied-credit version would do.
    """
    staged_at, entered_at = {}, {}
    for row in history:
        if row["changed_at"] is None:
            continue
        app_id = row["application_id"]
        if row["to_stage"] == "Staging":
            if app_id not in staged_at or row["changed_at"] < staged_at[app_id]:
                staged_at[app_id] = row["changed_at"]
        elif row["from_stage"] == "Staging" and row["to_stage"] in STAGE_ORDER:
            # Destination matters: leaving Staging for Closed Lost is the angle
            # failing, not the angle landing. Only a move into the funnel
            # counts as conversion.
            if app_id not in entered_at or row["changed_at"] < entered_at[app_id]:
                entered_at[app_id] = row["changed_at"]

    converted = set(staged_at) & set(entered_at)
    gaps = [
        round((entered_at[i] - staged_at[i]).total_seconds() / 86400, 1)
        for i in converted
    ]
    return {
        "staged": len(staged_at),
        "converted": len(converted),
        "conversion": None if not staged_at else round(len(converted) / len(staged_at), 3),
        "median_days_to_enter": round(median(gaps), 1) if gaps else None,
    }


def test_staged_cohort_is_built_from_real_transitions():
    """Jellyfish converts in 12 days, Popl is still being worked. One of two."""
    history = [
        {"application_id": 1, "from_stage": None, "to_stage": "Staging", "changed_at": D},
        {"application_id": 1, "from_stage": "Staging", "to_stage": "Qualification",
         "changed_at": D + timedelta(days=12)},
        {"application_id": 2, "from_stage": None, "to_stage": "Staging", "changed_at": D},
    ]
    res = staged_conversion(history)
    assert res["staged"] == 2
    assert res["converted"] == 1
    assert res["conversion"] == 0.5
    assert res["median_days_to_enter"] == 12.0


def test_a_role_that_never_staged_is_outside_the_cohort_entirely():
    """A recruiter-inbound application starts at Qualification and has no
    Staging row. It is neither a staged role nor a staged failure -- it simply
    isn't in this measurement."""
    history = [
        {"application_id": 1, "from_stage": None, "to_stage": "Staging", "changed_at": D},
        {"application_id": 1, "from_stage": "Staging", "to_stage": "Qualification",
         "changed_at": D + timedelta(days=5)},
        {"application_id": 9, "from_stage": None, "to_stage": "Qualification", "changed_at": D},
        {"application_id": 9, "from_stage": "Qualification", "to_stage": "Discovery",
         "changed_at": D + timedelta(days=2)},
    ]
    res = staged_conversion(history)
    assert res["staged"] == 1
    assert res["converted"] == 1
    assert res["conversion"] == 1.0


def test_no_staged_roles_reports_none_not_a_division_error():
    assert staged_conversion([])["conversion"] is None


def test_a_staged_role_that_died_without_applying_counts_as_a_failure():
    """The angle never landed and the posting closed. That's the failure mode
    this metric exists to surface, so it stays in the denominator and out of
    the numerator -- leaving Staging isn't the same as converting."""
    history = [
        {"application_id": 1, "from_stage": None, "to_stage": "Staging", "changed_at": D},
        {"application_id": 1, "from_stage": "Staging", "to_stage": "Closed Lost",
         "changed_at": D + timedelta(days=40)},
        {"application_id": 2, "from_stage": None, "to_stage": "Staging", "changed_at": D},
        {"application_id": 2, "from_stage": "Staging", "to_stage": "Qualification",
         "changed_at": D + timedelta(days=8)},
    ]
    res = staged_conversion(history)
    assert res["staged"] == 2
    assert res["converted"] == 1
    assert res["conversion"] == 0.5
    # The 40 days spent on the angle that didn't land contribute no timing --
    # median_days_to_enter answers "how long does a *successful* angle take",
    # and mixing failures in would inflate it with time that bought nothing.
    assert res["median_days_to_enter"] == 8.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} staging design assertions passed.")
