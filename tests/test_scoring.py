"""Design proof for the win-likelihood score, and for the age that outlived it.

Mirrors `_parse_score`, `_apply_score`, and `_activity_age` from
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

   The score is still written and still read -- it is now the *fallback*
   reading inside the forecast, used for an activity whose performance and
   engagement fields are blank, which is every activity in the database as it
   stands today. See `forecast._rating`. So this parsing and stamping still
   guards a live path; only the arithmetic that used to sit on top of it went
   away.

2. What went away was `_score_rollup`, which derived a second headline number
   from those scores and displayed it beside the automated forecast with no
   stated rule for which one won. `_activity_age` is what replaced it, and it
   answers a much smaller question: how long has this pursuit been quiet. It
   is derived at display time from a *mixed* set of activities -- meetings and
   threads, each reaching for a different date field, and `scored_at` stamped
   in UTC while form-entered dates come back naive. Getting the dates wrong
   reports a plausible wrong number rather than raising, which is the kind of
   bug that survives a long time. It did: the rollup shipped measuring from
   `scored_at` first, so a card could go quiet for a month and still read
   "1d ago" because you'd tidied up your data entry. The age tests below carry
   that lesson forward and are deliberately worded to say what the old ones
   got wrong.
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


def naive_utc(dt):
    """Mirror of ui._naive_utc()."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def activity_age(activities, now=None):
    """Mirror of ui._activity_age(), flattened over one list since the real one
    only differs in which date field each side falls back to -- `meeting_date`
    for a meeting, `last_message_at` then `started_at` for a thread, and
    `scored_at` behind both. That fallback chain is `when or scored_at` here.

    `now` is a parameter here only so the assertions are deterministic; the
    real one reads the clock.
    """
    dates = [naive_utc(a.when or a.scored_at) for a in activities]
    usable = [d for d in dates if d is not None]
    if not usable:
        return None
    now = naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    return max((now - max(usable)).days, 0)


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
    """Fixing a typo in the rationale isn't a new judgment, and `scored_at` is
    load-bearing in two places downstream: it is the fallback date an undated
    activity is aged off, and it is how `forecast.activity_quality` picks which
    activity is the latest one worth reading. Re-dating a typo fix would move
    both."""
    a = Activity(when=JULY)
    apply_score(a, "70", "went wel", now=JULY)
    later = JULY + timedelta(days=3)
    apply_score(a, "70", "went well", now=later)
    assert a.score_reason == "went well"
    assert a.scored_at == JULY


def test_clearing_the_score_clears_reason_and_timestamp():
    """An unscored activity must never carry a stale "scored on" date, or an
    activity whose judgment was explicitly withdrawn would go on presenting
    itself to `forecast.activity_quality` as the most recent reading."""
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
# Activity age -- how long this pursuit has been quiet
# --------------------------------------------------------------------------- #
def test_no_activity_at_all_reports_no_age():
    """So the template can skip the widget entirely rather than render an empty
    box on every brand-new application. None is "nothing has ever happened
    here", which is a different fact from a large number of days."""
    assert activity_age([]) is None


def test_an_activity_with_no_usable_date_reports_no_age():
    """Every date field on both objects is nullable, so an activity can exist
    carrying no date at all. That is "unknown when", not "very old" -- the old
    rollup sorted such a row onto datetime.min, and subtracting from that
    yields roughly 740,000 days, which would render as nonsense on a card."""
    assert activity_age([Activity(when=None, scored_at=None)], now=JULY) is None


def test_age_measures_the_most_recent_activity_not_the_oldest():
    """The board shows one age per card and it has to describe the pursuit's
    current silence. Measuring from the first activity would make an
    application that met with someone yesterday look abandoned."""
    age = activity_age([
        Activity(when=JULY - timedelta(days=60)),
        Activity(when=JULY),
    ], now=JULY + timedelta(days=3))
    assert age == 3


def test_age_measures_the_event_not_the_keystroke():
    """This used to assert the opposite, and the opposite was the wrong claim.
    A meeting from a month ago that you typed in yesterday is not a day-old
    situation -- nothing has happened on that pursuit in thirty-one days, and
    that silence is the thing worth acting on. Measuring from `scored_at` let a
    card go quiet for a month and still read "1d ago" because you had tidied up
    your data entry."""
    age = activity_age(
        [Activity(when=JULY - timedelta(days=30), score=55, scored_at=JULY)],
        now=JULY + timedelta(days=1),
    )
    assert age == 31


def test_an_unrated_activity_still_counts():
    """The sharpest difference from the rollup it replaced. That one only ever
    looked at activities carrying a `score`, because it was averaging readings.
    This one is not reading anything -- "nothing has happened in 21 days" is
    true whether or not you got around to rating what happened, and a recent
    unrated meeting must stop the card reading as stale."""
    stale = activity_age([Activity(when=JULY - timedelta(days=21), score=40)], now=JULY)
    fresh = activity_age([
        Activity(when=JULY - timedelta(days=21), score=40),
        Activity(when=JULY),  # happened, not yet rated
    ], now=JULY)
    assert stale == 21
    assert fresh == 0


def test_falls_back_to_scored_at_when_the_activity_has_no_date_of_its_own():
    """The fallback runs this way round deliberately, and unlike the rollup's
    original it can actually fire: an activity with no date of its own is aged
    off when you scored it, because that is the only date it has."""
    assert activity_age([Activity(when=None, score=60, scored_at=JULY)],
                        now=JULY + timedelta(days=4)) == 4


def test_an_undated_activity_cannot_hide_a_dated_one():
    """Undated rows are dropped rather than sorted to the front. The rollup
    sorted them onto datetime.min, which was harmless for ordering but meant a
    single undated row could take the "latest" slot and report no age at all.
    Here the dated sibling still answers the question."""
    assert activity_age([
        Activity(when=None, scored_at=None),
        Activity(when=JULY - timedelta(days=6)),
    ], now=JULY) == 6


def test_entry_order_does_not_matter():
    """The answer is a max over dates, so nothing about the order rows come
    back from the ORM in may move it."""
    a = Activity(when=JULY - timedelta(days=5))
    b = Activity(when=JULY)
    assert activity_age([a, b], now=JULY) == activity_age([b, a], now=JULY) == 0


def test_mixing_aware_and_naive_datetimes_does_not_raise():
    """`scored_at` is stamped with datetime.now(timezone.utc) (aware) while
    form-entered dates parse naive. max() over that mix raises TypeError, and
    because this is called from inside a template render the raise would take
    the whole board down rather than degrade one card. Everything is flattened
    to naive UTC first."""
    aware = Activity(when=None, scored_at=datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc))
    naive = Activity(when=datetime(2026, 7, 1, 0, 0))
    assert activity_age([aware, naive], now=datetime(2026, 7, 5, 0, 0)) == 3


def test_age_survives_an_aware_now():
    """Same hazard from the other side: the real function builds `now` naive on
    purpose, but nothing stops a caller handing one in aware."""
    assert activity_age(
        [Activity(when=None, scored_at=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))],
        now=datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
    ) == 20


def test_a_future_dated_activity_clamps_to_zero():
    """Activity dates are typed in by hand and can legitimately be in the
    future -- a meeting on the calendar for next week. A negative age is
    meaningless on a card, so it floors at 0."""
    assert activity_age([Activity(when=JULY + timedelta(days=5))], now=JULY) == 0


def test_zero_is_an_age_and_none_is_not():
    """The template tests `activity_age is not none`, so these two must stay
    distinguishable: 0 means "something happened today" and should render,
    None means "nothing has ever happened" and should not. A truthiness test
    at either end would collapse them."""
    assert activity_age([Activity(when=JULY)], now=JULY) == 0
    assert activity_age([Activity(when=None, scored_at=None)], now=JULY) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} scoring assertions passed.")
