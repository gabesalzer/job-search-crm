"""The Automated Forecast, tested against the real implementation.

Every other test file in this project hand-mirrors the code it checks, because
SQLAlchemy and FastAPI aren't installable in the environment this gets developed
in and the logic lives inside modules that import them. A mirror can prove a
design is coherent; it can't prove the app does it, and it drifts silently the
moment someone edits one side and not the other.

app/forecast.py exists partly to escape that. It imports nothing but the
standard library, so this file imports and exercises the actual module. When an
assertion here fails, the app is wrong -- not a copy of it.

What's worth proving:

1. The band boundaries. `Commit` is a claim -- "more likely than not, 75%+" --
   and the whole model is only useful if that word keeps meaning that. The
   threshold tests pin the arithmetic that decides it.
2. Blank is not zero. A meeting you didn't rate and a meeting you rated 0 are
   opposite statements, and the forecast has to read them as such. This is the
   same invariant the score column carries, and it's easy to break with a
   truthiness check.
3. The closed short-circuits. A won application must never read Best Case
   because nobody filled in the meeting fields.
4. `confidence` has to be able to say "none". A brand-new empty record sits at
   the default stage with no data at all, and the model must not mistake the
   column default for evidence.
5. The fit index has to actually discriminate. A bag-of-words cosine that scores
   a pastry-chef JD near a RevOps resume is worse than no fit signal at all,
   because it would launder noise into the total.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import forecast as f  # noqa: E402


# --------------------------------------------------------------------------- #
# Sample documents for the fit index
# --------------------------------------------------------------------------- #
RESUME = """
Revenue Operations leader. Built and ran the go-to-market operating system for a
B2B SaaS business: pipeline inspection, forecast accuracy, territory and quota
design, compensation planning, and the Salesforce architecture underneath all of
it. Rebuilt the opportunity stage model and forecast categories so that commit
meant something; forecast accuracy moved from plus or minus thirty percent to
inside eight percent within two quarters. Owned CRM data quality, lead routing,
and the lifecycle from marketing qualified lead through closed won. Partnered
with finance on ARR reporting, net revenue retention, and board metrics. Ran
quarterly business reviews with sales leadership. Managed a team of four
analysts and one Salesforce administrator. Tooling: Salesforce, Marketo,
Outreach, Gong, Looker, dbt, SQL, Snowflake.
"""

MATCHING_JD = """
We are looking for a Revenue Operations lead to own the go-to-market operating
system. You will run pipeline inspection and forecast cadence, own forecast
accuracy, design territories and quotas, and partner with finance on ARR, net
revenue retention, and board reporting. You will own the Salesforce architecture,
CRM data quality, lead routing, and the funnel from marketing qualified lead to
closed won. You will run quarterly business reviews with sales leadership and
manage a small team of analysts. Our stack is Salesforce, Marketo, Outreach,
Gong, Looker, dbt, and Snowflake.
"""

UNRELATED_JD = """
We are hiring a pastry chef for a high-volume bakery. You will produce
laminated doughs, croissants, brioche, and seasonal tarts each morning, manage
oven schedules, maintain sourdough starters, and keep the kitchen compliant with
food safety and sanitation standards. Early mornings and weekend shifts are
required. Culinary school training or an equivalent apprenticeship is expected,
along with several seasons of high-volume production baking.
"""


def m(perf=None, eng=None, when=None):
    """A meeting as the ORM adapter hands it over."""
    return {"when": when, "my_performance": perf, "employer_engagement": eng}


# --------------------------------------------------------------------------- #
# Fit index
# --------------------------------------------------------------------------- #
def test_fit_index_is_none_when_either_document_is_missing():
    """None, not 0.0. A pursuit with no JD attached hasn't scored badly on fit;
    it has no fit reading at all, and the total must not be docked for it."""
    assert f.fit_index(None, MATCHING_JD) is None
    assert f.fit_index(RESUME, None) is None
    assert f.fit_index("", "") is None


def test_fit_index_is_none_for_documents_too_short_to_mean_anything():
    """A JD field holding "see attached" would otherwise produce a wild cosine
    off three tokens. Below the length floor the honest answer is "no reading"."""
    assert f.fit_index(RESUME, "Revenue operations. Apply within.") is None


def test_a_matching_jd_scores_far_above_an_unrelated_one():
    """The whole justification for keeping a crude bag-of-words index in the
    model: it has to separate the obvious cases by a wide margin, or it is
    laundering noise into the total."""
    close = f.fit_index(RESUME, MATCHING_JD)
    far = f.fit_index(RESUME, UNRELATED_JD)
    assert close > 0.4
    assert far < 0.1


def test_bands_name_the_index_rather_than_exposing_the_number():
    """The UI shows Low/Moderate/Strong. A raw 0.31 would read as "31% fit",
    which is a claim this index cannot support."""
    assert f.fit_band(f.fit_index(RESUME, MATCHING_JD)) == "Strong"
    assert f.fit_band(f.fit_index(RESUME, UNRELATED_JD)) == "Low"
    assert f.fit_band(None) is None


def test_fit_points_are_floored_and_capped():
    """Below the floor a document pair shares nothing beyond chance, so it earns
    nothing; above the ceiling more overlap says more about copied phrasing than
    about fit, so it stops paying."""
    assert f._fit_points(None) == 0.0
    assert f._fit_points(0.0) == 0.0
    assert f._fit_points(f.FIT_FLOOR) == 0.0
    assert f._fit_points(f.FIT_CEILING) == f.W_FIT
    assert f._fit_points(0.95) == f.W_FIT


def test_identical_text_does_not_break_the_cosine():
    """Degenerate but reachable -- paste the JD into the resume field by
    accident. It should saturate, not divide by zero.

    The upper bound is 1.0 plus float slop, not 1.0 flat: the cosine divides by
    two separately-accumulated square roots, so identical inputs land a few ulps
    either side of 1. Nothing downstream cares -- `_fit_points` caps at the
    ceiling and the display rounds to three places -- but asserting a hard 1.0
    here would be testing IEEE 754 rather than the model.
    """
    assert 0.99 <= f.fit_index(RESUME, RESUME) <= 1.0 + 1e-9


# --------------------------------------------------------------------------- #
# Meeting quality
# --------------------------------------------------------------------------- #
def test_no_meetings_and_no_ratings_both_read_as_no_signal():
    assert f.meeting_quality([]) is None
    assert f.meeting_quality([m(), m()]) is None


def test_a_zero_rating_is_a_reading_not_an_absence():
    """The invariant that's easiest to break: `if perf` instead of
    `if perf is not None` would make "that went terribly" indistinguishable from
    "I haven't judged it", and the two point in opposite directions."""
    assert f.meeting_quality([m(perf=0, eng=0)]) == 0.0


def test_one_rated_half_is_used_alone_rather_than_averaged_against_nothing():
    """Rating only their engagement is a common and legitimate half-answer.
    Treating the blank half as 0 would halve the score for a meeting you
    described accurately."""
    assert f.meeting_quality([m(eng=80)]) == 80.0
    assert f.meeting_quality([m(perf=80)]) == 80.0


def test_engagement_outweighs_performance():
    """Their interest is the thing that produces an offer; your performance is a
    leading indicator of interest they haven't shown yet."""
    theirs = f.meeting_quality([m(perf=0, eng=100)])
    mine = f.meeting_quality([m(perf=100, eng=0)])
    assert theirs > mine
    assert theirs == f.ENGAGEMENT_WEIGHT * 100
    assert mine == f.PERFORMANCE_WEIGHT * 100


def test_quality_reads_the_most_recent_meeting_not_an_average():
    """Same argument the score rollup makes: a 20 and an 80 average to 50, which
    describes a process that never happened. The latest conversation is where
    the pursuit actually stands."""
    old = m(perf=90, eng=90, when=1000)
    new = m(perf=20, eng=20, when=2000)
    assert f.meeting_quality([old, new]) == 20.0
    assert f.meeting_quality([new, old]) == 20.0  # list order must not matter


def test_undated_meetings_do_not_raise_when_picking_the_latest():
    """Regression: sorting on a (has_date, date) tuple compares None against
    None as soon as two meetings are undated, which raises TypeError. Meeting
    dates are nullable, so this is reachable from the UI."""
    assert f.meeting_quality([m(perf=40), m(perf=60)]) == 60.0


def test_a_dated_meeting_wins_over_an_undated_one():
    """An undated row carries no claim about when it happened, so it must not
    displace a meeting that does."""
    assert f.meeting_quality([m(perf=10, when=500), m(perf=90)]) == 10.0


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
def test_an_empty_application_has_nothing_to_say_and_says_so():
    """The state every record is born in. It must read Pipeline with confidence
    "none" -- not "thin". Counting the default stage as evidence would let a
    record that has never been touched claim it had something to go on."""
    out = f.automated_forecast(stage="Qualification", source=None)
    assert out["category"] == f.PIPELINE
    assert out["confidence"] == "none"
    assert out["total"] == 4


def test_a_strong_late_stage_pursuit_reaches_commit():
    out = f.automated_forecast(
        stage="Executive Signoff",
        source="Referral",
        meetings=[m(perf=85, eng=90, when=1), m(perf=80, eng=95, when=2)],
        resume_text=RESUME,
        jd_text=MATCHING_JD,
    )
    assert out["category"] == f.COMMIT
    assert out["total"] >= f.COMMIT_AT
    assert out["confidence"] == "ok"


def test_a_cold_late_stage_pursuit_does_not_coast_on_its_stage():
    """Stage is an input, not a floor. A process that has gone deep but is
    visibly dying must be allowed to fall out of Commit -- otherwise the
    forecast just restates the board column."""
    out = f.automated_forecast(
        stage="Executive Signoff",
        source="Outbound",
        meetings=[m(perf=30, eng=5, when=1)],
        resume_text=RESUME,
        jd_text=UNRELATED_JD,
    )
    assert out["category"] != f.COMMIT


def test_a_strong_early_pursuit_can_outrank_a_limp_late_one():
    """The consequence of stage-as-input, stated as a test so it can't be
    changed by accident. This is intended behaviour, not a bug: a referral with
    two excellent conversations and a matching resume is genuinely a better bet
    than a stalled final-round nobody is excited about."""
    early = f.automated_forecast(
        stage="Discovery", source="Referral",
        meetings=[m(perf=90, eng=95, when=1), m(perf=88, eng=92, when=2)],
        resume_text=RESUME, jd_text=MATCHING_JD,
    )
    late = f.automated_forecast(
        stage="Negotiation", source="Outbound",
        meetings=[m(perf=25, eng=15, when=1)],
    )
    assert early["total"] > late["total"]


def test_referral_beats_outbound_all_else_equal():
    """The single input the user named first, isolated."""
    kw = dict(stage="Discovery", meetings=[m(perf=70, eng=70, when=1)],
              resume_text=RESUME, jd_text=MATCHING_JD)
    ref = f.automated_forecast(source="Referral", **kw)
    out = f.automated_forecast(source="Outbound", **kw)
    assert ref["total"] - out["total"] == f.SOURCE_POINTS["Referral"] - f.SOURCE_POINTS["Outbound"]


def test_band_edges_land_on_the_stated_definitions():
    """Commit is "75% or higher" in the user's own words, so the threshold is
    inclusive at 75 and the point below it is not Commit."""
    assert f._band_for(f.COMMIT_AT) == f.COMMIT
    assert f._band_for(f.COMMIT_AT - 0.01) == f.BEST_CASE
    assert f._band_for(f.BEST_CASE_AT) == f.BEST_CASE
    assert f._band_for(f.BEST_CASE_AT - 0.01) == f.PIPELINE


# --------------------------------------------------------------------------- #
# Closed stages
# --------------------------------------------------------------------------- #
def test_closed_won_is_commit_regardless_of_missing_inputs():
    """A won application with no meeting ratings would otherwise score ~30 and
    render Best Case, which is an absurdity the user would see on the board."""
    out = f.automated_forecast(stage="Closed Won", source=None)
    assert out["category"] == f.COMMIT
    assert out["total"] == 100
    assert out["confidence"] == "high"


def test_closed_lost_is_pipeline_regardless_of_strong_inputs():
    out = f.automated_forecast(
        stage="Closed Lost", source="Referral",
        meetings=[m(perf=95, eng=95, when=1)],
        resume_text=RESUME, jd_text=MATCHING_JD,
    )
    assert out["category"] == f.PIPELINE
    assert out["total"] == 0


def test_closed_results_carry_the_same_component_keys_as_open_ones():
    """The short-circuits return _no_components() rather than {} so a template
    rendering the breakdown never needs a closed-case guard -- the first one to
    forget it would raise on a won deal."""
    closed = f.automated_forecast(stage="Closed Won", source=None)
    open_ = f.automated_forecast(stage="Discovery", source="Referral")
    assert set(closed["components"]) == set(open_["components"])


# --------------------------------------------------------------------------- #
# Shape and reporting
# --------------------------------------------------------------------------- #
def test_every_result_carries_the_full_contract():
    for out in [
        f.automated_forecast(stage="Qualification", source=None),
        f.automated_forecast(stage="Closed Won", source=None),
        f.automated_forecast(stage="Discovery", source="Referral",
                             meetings=[m(perf=50, eng=50)]),
    ]:
        assert set(out) == {"category", "total", "components", "confidence", "reason"}
        assert out["category"] in {f.COMMIT, f.BEST_CASE, f.PIPELINE}
        assert isinstance(out["total"], int)
        assert 0 <= out["total"] <= 100
        assert out["confidence"] in {"none", "thin", "ok", "high"}
        assert out["reason"] and out["reason"][-1] in ".!"


def test_confidence_separates_an_ignorant_pipeline_from_an_informed_one():
    """Both are Pipeline and they are completely different situations. Folding
    that into the band would hide it, so it rides alongside."""
    blank = f.automated_forecast(stage="Qualification", source=None)
    informed = f.automated_forecast(
        stage="Discovery", source="Outbound",
        meetings=[m(perf=10, eng=5, when=1)],
        resume_text=RESUME, jd_text=UNRELATED_JD,
    )
    assert blank["category"] == informed["category"] == f.PIPELINE
    assert blank["confidence"] == "none"
    assert informed["confidence"] == "ok"


def test_components_sum_to_the_total():
    """The breakdown is shown to justify the number. If they can disagree, the
    panel is decoration."""
    out = f.automated_forecast(
        stage="Takehome", source="Recruiter Inbound",
        meetings=[m(perf=70, eng=60, when=1)],
        resume_text=RESUME, jd_text=MATCHING_JD,
    )
    c = out["components"]
    assert round(c["stage"] + c["meetings"] + c["fit"] + c["source"]) == out["total"]


def test_weights_sum_to_one_hundred():
    """What makes the total readable as a rough percentage, which is what makes
    the user's "75% or higher" definition mean what he said."""
    assert f.W_STAGE + f.W_MEETINGS + f.W_FIT + f.W_SOURCE == 100
    assert f.MAX_QUALITY_POINTS + f.MAX_DEPTH_POINTS == f.W_MEETINGS


def test_no_input_combination_can_exceed_one_hundred():
    """Belt and braces on the band thresholds: if any path could overflow, the
    Commit boundary would stop meaning 75 percent of anything."""
    out = f.automated_forecast(
        stage="Negotiation", source="Referral",
        meetings=[m(perf=100, eng=100, when=i) for i in range(10)],
        resume_text=RESUME, jd_text=RESUME,
    )
    assert out["total"] <= 100


def test_depth_credit_is_capped():
    """Booking more time is real evidence, but it must not be able to carry a
    pursuit on volume alone -- ten mediocre conversations are not a Commit."""
    one = f.automated_forecast(stage="Discovery", source=None,
                               meetings=[m(perf=50, eng=50, when=1)])
    many = f.automated_forecast(
        stage="Discovery", source=None,
        meetings=[m(perf=50, eng=50, when=i) for i in range(20)],
    )
    assert many["total"] - one["total"] <= f.MAX_DEPTH_POINTS


# --------------------------------------------------------------------------- #
# Cross-module agreement
# --------------------------------------------------------------------------- #
def test_categories_match_the_enum_in_models():
    """forecast.py deliberately holds these as literals rather than importing
    models.ForecastCategory, because importing it would drag SQLAlchemy in and
    cost this file its ability to test the real code. The cost of that choice is
    exactly one thing -- the two can drift -- so the drift is checked here, by
    reading models.py as text.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    models_src = open(os.path.join(here, "..", "app", "models.py")).read()
    block = models_src.split("class ForecastCategory", 1)[1].split("\nclass ", 1)[0]
    declared = set(re.findall(r'^\s{4}[A-Z_]+ = "([^"]+)"', block, re.M))
    assert {f.COMMIT, f.BEST_CASE, f.PIPELINE} <= declared, declared


def test_stage_points_cover_every_stage_in_models():
    """A stage missing from STAGE_POINTS scores 0 silently, which would make a
    newly-added late stage quietly forecast worse than Qualification."""
    here = os.path.dirname(os.path.abspath(__file__))
    models_src = open(os.path.join(here, "..", "app", "models.py")).read()
    block = models_src.split("class Stage", 1)[1].split("\nclass ", 1)[0]
    declared = set(re.findall(r'^\s{4}[A-Z_]+ = "([^"]+)"', block, re.M))
    assert declared <= set(f.STAGE_POINTS), declared - set(f.STAGE_POINTS)


def test_sources_cover_every_source_in_models():
    here = os.path.dirname(os.path.abspath(__file__))
    models_src = open(os.path.join(here, "..", "app", "models.py")).read()
    block = models_src.split("class ApplicationSource", 1)[1].split("\nclass ", 1)[0]
    declared = set(re.findall(r'^\s{4}[A-Z_]+ = "([^"]+)"', block, re.M))
    assert declared <= set(f.SOURCE_POINTS), declared - set(f.SOURCE_POINTS)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} forecast assertions passed.")
