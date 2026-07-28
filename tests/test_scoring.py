"""Design proof for the win-likelihood score on Meeting and Email Thread.

Mirrors `_parse_score`, `_apply_score`, and `_score_rollup` from
app/routers/ui.py by hand, the same way test_cascade_design.py mirrors the
DDL -- this sandbox can't import FastAPI/SQLAlchemy, so ui.py itself can't be
imported here. **If the real helpers in ui.py change, mirror the change here
too**, or this file starts proving something the app no longer does.

Two things are worth proving out, and neither is obvious from reading the
code:

1. `score` has no database-level constraint. `ensure_schema()` only ever
   issues ADD COLUMN and could never add a CHECK to the existing tables, so
   the 0-100 range is enforced entirely at the one door that writes the
   column. If that clamp is wrong, nothing downstream catches it.

2. The rollup is derived at display time from a *mixed* set of activities --
   meetings and threads, each falling back to a different date field, and
   `scored_at` stamped in UTC while form-entered dates come back naive.
   Getting the ordering wrong silently reports the wrong trend rather than
   raising, which is the kind of bug that survives a long time.
"""
from datetime import datetime, timedelta, timezone


# --------------------------------------------------------------------------- #
# Hand-mirrored from app/routers/ui.py
# --------------------------------------------------------------------------- #
def parse_score(value):
    """Mirror of ui._parse_score()."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        score = int(float(text))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


class Activity:
    """Stand-in for a Meeting or EmailThread row -- only the fields the
    scoring code actually touches."""

    def __init__(self, when=None, score=None, score_reason=None, scored_at=None):
        self.when = when
        self.score = score
        self.score_reason = score_reason
        self.scored_at = scored_at


def apply_score(obj, raw_score, reason, now):
    """Mirror of ui._apply_score(), with `now` injected so the test can assert
    on stamping instead of racing the clock."""
    new_score = parse_score(raw_score)
    if new_score is None:
        obj.score = None
        obj.score_reason = None
        obj.scored_at = None
        return
    if new_score != obj.score or obj.scored_at is None:
        obj.scored_at = now
    obj.score = new_score
    obj.score_reason = (reason or "").strip() or None


def _sortable(dt):
    if dt is None:
        return datetime.min
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def score_rollup(activities):
    """Mirror of ui._score_rollup(), flattened over one list since the real
    one only differs in which date field each side falls back to."""
    scored = [
        (_sortable(a.scored_at or a.when), a.score)
        for a in activities
        if a.score is not None
    ]
    if not scored:
        return None
    scored.sort(key=lambda row: row[0])
    latest = scored[-1][1]
    previous = scored[-2][1] if len(scored) > 1 else None
    return {
        "latest": latest,
        "previous": previous,
        "delta": None if previous is None else latest - previous,
        "count": len(scored),
    }


JULY = datetime(2026, 7, 1, 12, 0)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_blank_is_unscored_not_zero():
    """The distinction the whole feature rests on. A blank field means "I
    haven't formed a judgment"; 0 means "I think this is dead". Collapsing
    the first into the second would fill the calibration data with confident
    zeros nobody ever asserted."""
    assert parse_score("") is None
    assert parse_score("   ") is None
    assert parse_score(None) is None
    assert parse_score("0") == 0


def test_range_is_clamped_at_both_ends():
    """There is no CHECK constraint behind this column -- see the module
    docstring. The clamp here IS the invariant."""
    assert parse_score("-5") == 0
    assert parse_score("101") == 100
    assert parse_score("999999") == 100
    assert parse_score("100") == 100


def test_ordinary_values_pass_through():
    for raw, expected in [("1", 1), ("50", 50), ("99", 99)]:
        assert parse_score(raw) == expected


def test_decimal_input_truncates_rather_than_erroring():
    """A number input can still hand back "70.5" (some locales/steppers do).
    Truncating keeps the form savable; erroring would lose the rest of it."""
    assert parse_score("70.5") == 70
    assert parse_score("70.9") == 70


def test_garbage_reads_as_unscored_not_a_crash():
    """A typo in an optional field must not 500 the whole form post and throw
    away the transcript the user just pasted."""
    assert parse_score("abc") is None
    assert parse_score("7o") is None
    assert parse_score("--") is None


def test_whitespace_is_tolerated():
    assert parse_score("  80  ") == 80


# --------------------------------------------------------------------------- #
# Stamping
# --------------------------------------------------------------------------- #
def test_first_score_stamps_scored_at():
    a = Activity(when=JULY)
    apply_score(a, "70", "went well", now=JULY)
    assert (a.score, a.score_reason, a.scored_at) == (70, "went well", JULY)


def test_changing_the_number_restamps():
    a = Activity(when=JULY)
    apply_score(a, "70", "went well", now=JULY)
    later = JULY + timedelta(days=3)
    apply_score(a, "30", "they went quiet", now=later)
    assert a.score == 30
    assert a.scored_at == later


def test_editing_only_the_reason_does_not_restamp():
    """Fixing a typo in the rationale isn't a new judgment. Re-dating it
    would reorder the trend series and change what the rollup reports."""
    a = Activity(when=JULY)
    apply_score(a, "70", "went wel", now=JULY)
    later = JULY + timedelta(days=3)
    apply_score(a, "70", "went well", now=later)
    assert a.score_reason == "went well"
    assert a.scored_at == JULY


def test_clearing_the_score_clears_reason_and_timestamp():
    """An unscored activity must never carry a stale "scored on" date, or the
    rollup would sort on a judgment that no longer exists."""
    a = Activity(when=JULY)
    apply_score(a, "70", "went well", now=JULY)
    apply_score(a, "", "went well", now=JULY + timedelta(days=1))
    assert (a.score, a.score_reason, a.scored_at) == (None, None, None)


def test_rescoring_to_zero_is_a_real_score_not_a_clear():
    """0 and blank take different branches. Easy to get wrong, because both
    are falsy in Python."""
    a = Activity(when=JULY)
    apply_score(a, "70", "hopeful", now=JULY)
    later = JULY + timedelta(days=1)
    apply_score(a, "0", "rejected", now=later)
    assert a.score == 0
    assert a.scored_at == later
    assert a.score_reason == "rejected"


def test_blank_reason_stored_as_null_not_empty_string():
    a = Activity(when=JULY)
    apply_score(a, "70", "   ", now=JULY)
    assert a.score == 70
    assert a.score_reason is None


def test_same_score_with_no_prior_stamp_gets_stamped():
    """Backfill case: a row scored before `scored_at` existed. Re-saving the
    same number must give it a timestamp rather than leaving it unsortable."""
    a = Activity(when=JULY, score=70, scored_at=None)
    apply_score(a, "70", "unchanged", now=JULY)
    assert a.scored_at == JULY


# --------------------------------------------------------------------------- #
# Rollup
# --------------------------------------------------------------------------- #
def test_nothing_scored_returns_none():
    """So the template can skip the widget entirely rather than render an
    empty box on every brand-new application."""
    assert score_rollup([Activity(when=JULY), Activity(when=JULY)]) is None


def test_single_score_has_no_delta():
    roll = score_rollup([Activity(when=JULY, score=40, scored_at=JULY)])
    assert roll == {"latest": 40, "previous": None, "delta": None, "count": 1}


def test_latest_and_delta_follow_scored_at_not_the_activity_date():
    """The point of a separate `scored_at`: a meeting from three weeks ago
    that you scored yesterday reflects your most recent thinking, and has to
    land last in the series even though its own date is oldest."""
    old_meeting_scored_late = Activity(
        when=JULY - timedelta(days=21), score=30, scored_at=JULY + timedelta(days=1)
    )
    recent_thread = Activity(when=JULY, score=80, scored_at=JULY)
    roll = score_rollup([old_meeting_scored_late, recent_thread])
    assert roll["latest"] == 30
    assert roll["previous"] == 80
    assert roll["delta"] == -50


def test_falls_back_to_the_activity_date_when_unstamped():
    a = Activity(when=JULY - timedelta(days=5), score=20, scored_at=None)
    b = Activity(when=JULY, score=60, scored_at=None)
    roll = score_rollup([b, a])  # deliberately out of order
    assert roll["latest"] == 60
    assert roll["delta"] == 40


def test_mixing_aware_and_naive_datetimes_does_not_raise():
    """`scored_at` is stamped with datetime.now(timezone.utc) (aware) while
    form-entered dates parse naive. Comparing the two raises TypeError, so
    the rollup flattens everything to naive UTC first. Without that, an
    application with one hand-dated and one app-stamped score would 500."""
    aware = Activity(when=None, score=90, scored_at=datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc))
    naive = Activity(when=datetime(2026, 7, 1, 0, 0), score=50, scored_at=None)
    roll = score_rollup([aware, naive])
    assert roll["latest"] == 90
    assert roll["previous"] == 50
    assert roll["count"] == 2


def test_unscored_activities_are_ignored_in_the_count():
    """`count` is "how many readings back this number", not "how much
    activity happened" -- an unscored meeting must not inflate it."""
    roll = score_rollup([
        Activity(when=JULY, score=50, scored_at=JULY),
        Activity(when=JULY),
        Activity(when=JULY + timedelta(days=1), score=60, scored_at=JULY + timedelta(days=1)),
    ])
    assert roll["count"] == 2


def test_zero_scores_participate_in_the_rollup():
    """A 0 is a reading, not an absence. If `if a.score` were used instead of
    `is not None`, a "this is dead" score would silently vanish."""
    roll = score_rollup([
        Activity(when=JULY, score=70, scored_at=JULY),
        Activity(when=JULY + timedelta(days=1), score=0, scored_at=JULY + timedelta(days=1)),
    ])
    assert roll["latest"] == 0
    assert roll["delta"] == -70
    assert roll["count"] == 2


def test_flat_trend_reports_zero_delta_not_none():
    """None means "no prior reading" and hides the pill; 0 means "held
    steady" and should still show. Different facts."""
    roll = score_rollup([
        Activity(when=JULY, score=60, scored_at=JULY),
        Activity(when=JULY + timedelta(days=1), score=60, scored_at=JULY + timedelta(days=1)),
    ])
    assert roll["delta"] == 0
    assert roll["previous"] == 60


def test_activity_with_no_dates_at_all_sorts_first():
    """Neither `scored_at` nor an activity date -- possible, since every date
    field on both objects is nullable. It must not crash the sort, and it
    must not steal the "latest" slot from a dated reading."""
    undated = Activity(when=None, score=10, scored_at=None)
    dated = Activity(when=JULY, score=90, scored_at=JULY)
    roll = score_rollup([undated, dated])
    assert roll["latest"] == 90
    assert roll["previous"] == 10


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} scoring assertions passed.")
