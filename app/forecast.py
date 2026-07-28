"""Automated Forecast: a derived Commit / Best Case / Pipeline read.

Deliberately stdlib-only
------------------------
This module imports nothing from SQLAlchemy and nothing from app.models. It
takes plain values in and returns a plain dict out. Two reasons:

  1. The whole app's business logic is otherwise only testable by hand-mirroring
     it in the test files, because SQLAlchemy isn't installable in the
     environment this gets developed in. Keeping the forecast pure means
     tests/test_forecast.py imports and exercises *this* code rather than a
     copy of it that can silently drift.
  2. It forces the boundary to stay honest. The forecast is a function of four
     named inputs. If it needed to reach into the ORM to answer a question, that
     question would be an undeclared input, and the model would be harder to
     argue with than it is to run.

The ORM-side adapter that gathers these inputs lives in routers/ui.py, and it
is intentionally the only thing that knows about the database.

Deliberately no LLM
-------------------
Nothing here calls a model. `fit_index()` is bag-of-words arithmetic. That is a
real limitation and is described honestly at the function, rather than dressed
up: it measures vocabulary overlap, not capability. The alternative -- shipping
transcripts and JDs to an API from inside the app -- is a boundary this project
does not cross (see ARCHITECTURE.md), and a crude number you understand beats a
good one you can't audit.

What the number claims
----------------------
One question, the same one the activity `score` answers: how likely is this to
end in an offer that gets accepted? The bands are the user's own definitions --
Commit is "more likely than not, ~75%+", Best Case is "possible if a few things
break our way", Pipeline is "no signal, not forecastable" and doubles as the
residual bucket for informed-but-weak.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Component weights
# --------------------------------------------------------------------------- #
# These sum to 100 so the intermediate total reads as a rough percentage, which
# is what makes the 75 threshold mean what the user says it means. The split
# reflects a claim worth stating out loud: what happened in the room outranks
# everything else, and paper fit is the weakest of the four because a resume
# that matches a JD's vocabulary has told you very little about whether anyone
# wants to hire you.
W_STAGE = 30
W_MEETINGS = 35
W_FIT = 20
W_SOURCE = 15

# Stage contributes as an *input*, not a cap. A strong early pursuit can
# therefore out-forecast a limp late one, which is the intended behavior: at
# Discovery (12) with perfect meetings (35), perfect fit (20) and a referral
# (15) you reach 82 and read Commit. The counter-argument -- that "more likely
# than not to end in an accepted offer" is a claim about the *whole* remaining
# path, and no amount of first-call warmth should let a Qualification-stage
# record claim it -- is a real one, and the reason the stage weight rises
# steeply rather than linearly. Qualification is worth almost nothing.
STAGE_POINTS = {
    "Staging": 0,             # pre-application; there is no pursuit yet
    "Qualification": 4,
    "Discovery": 12,
    "Takehome": 19,
    "Executive Signoff": 25,
    "Negotiation": 30,
    "Closed Won": 30,
    "Closed Lost": 0,
}

# Referral is the highest-converting origin by a wide margin, which is the whole
# reason the Staging stage exists. Recruiter Inbound sits well above Outbound
# because someone already decided you were worth contacting. Outbound is not
# zero -- a submitted application is still a real pursuit -- but it carries no
# evidence that anyone on the other side has noticed.
SOURCE_POINTS = {
    "Referral": 15,
    "Recruiter Inbound": 9,
    "Outbound": 3,
}

# Band thresholds on the 0-100 total. COMMIT_AT is the user's own "75% or
# higher"; BEST_CASE_AT is where "possible if a few things break our way" stops
# being true and the honest answer becomes "I don't know."
COMMIT_AT = 75
BEST_CASE_AT = 40

# Category strings. Kept as literals rather than imported from models.py so this
# module stays free of SQLAlchemy; tests/test_forecast.py asserts they match the
# ForecastCategory enum by reading models.py as text.
COMMIT = "Commit"
BEST_CASE = "Best Case"
PIPELINE = "Pipeline"

# Employer engagement is weighted above own performance because it is the more
# direct evidence: their interest is the thing that produces an offer, while a
# strong performance is only a leading indicator of interest they haven't shown
# yet. The gap is 60/40 rather than something starker because a great
# performance in front of a flat interviewer genuinely does convert sometimes.
ENGAGEMENT_WEIGHT = 0.6
PERFORMANCE_WEIGHT = 0.4

# Points reserved for how *deep* the process has gone, inside the meetings
# budget. Each meeting that carries a quality read is worth this much, because
# an organization that keeps booking time is spending something real, and that
# is information the latest meeting's quality score does not contain.
DEPTH_POINTS_PER_MEETING = 2
MAX_DEPTH_POINTS = 7
MAX_QUALITY_POINTS = W_MEETINGS - MAX_DEPTH_POINTS  # 28


# --------------------------------------------------------------------------- #
# Resume vs JD fit
# --------------------------------------------------------------------------- #
# Ordinary English, plus the words that appear in essentially every resume and
# every job description. The second group matters more than the first: without
# it, two unrelated documents still score a respectable overlap purely on
# "experience / team / role / responsibilities", and the index loses its ability
# to discriminate at exactly the range where it needs to.
_STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because
been before being below between both but by can cannot could did do does doing
down during each few for from further had has have having he her here hers him
his how i if in into is it its itself just me more most my no nor not of off on
once only or other our ours out over own same she should so some such than that
the their theirs them then there these they this those through to too under
until up very was we were what when where which while who whom why will with
you your yours
ability able across ideal including like made make many may must new one part
plus role roles strong take upon us within work working works year years
applicant applicants apply application benefits candidate candidates career
company companies description employee employees employer employment equal
experience experienced hire hiring job level opportunity opportunities position
positions preferred qualifications qualified req requirement requirements
required responsibilities responsibility skill skills team teams
""".split())

# Below this, two documents share almost nothing beyond chance and the index
# should report zero rather than a small encouraging number. Above the ceiling,
# further overlap says more about copied phrasing than about fit. Both are
# calibrated for sublinear-TF cosine between a one-page resume and a JD, where
# a genuinely good match lands around 0.25-0.35 and 0.5+ generally means the
# resume was written against that specific posting.
FIT_FLOOR = 0.08
FIT_CEILING = 0.30

_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#./\-]*")


def _tokens(text: str) -> Counter:
    """Bag of content words, sublinear-TF weighted.

    Sublinear rather than raw counts (1 + log n) so a JD that says "revenue"
    eleven times doesn't let that single word dominate the vector. Tokens of
    one or two characters are dropped: they're overwhelmingly initials, list
    markers and units, and they add noise that survives the stopword list.
    """
    raw = Counter(
        tok for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) > 2 and tok not in _STOPWORDS
    )
    return Counter({tok: 1.0 + math.log(n) for tok, n in raw.items()})


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    # Iterate the smaller vector; the dot product is symmetric and most tokens
    # in a JD never appear in a given resume.
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    dot = sum(weight * large.get(tok, 0.0) for tok, weight in small.items())
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(w * w for w in a.values()))
    norm_b = math.sqrt(sum(w * w for w in b.values()))
    return dot / (norm_a * norm_b)


def fit_index(resume_text: Optional[str], jd_text: Optional[str]) -> Optional[float]:
    """How much vocabulary a resume and a job description share, 0.0 to 1.0.

    This is cosine similarity over stopword-filtered, sublinear-TF token
    vectors. No IDF: with a corpus of exactly two documents, inverse document
    frequency is degenerate (every term is in one or both), so weighting by it
    would be arithmetic theater rather than information.

    Be clear about what this can and cannot see. It measures *whether the two
    documents talk about the same things in the same words*. It does not know
    whether you can do the job, it cannot tell a claim from an accomplishment,
    and it will happily reward a JD stuffed with the same buzzwords your resume
    happens to use. It is in the forecast because it is free, deterministic,
    reruns on every page load, and moves in roughly the right direction -- not
    because it is a good judge of fit. That is why it carries the smallest
    weight of the four components and why the UI shows it as a band rather than
    as a number that would invite false precision.

    Returns None -- not 0.0 -- when either side is missing or too thin to say
    anything, keeping the same blank-is-not-zero rule the score fields use. A
    document with fewer than 20 content words after filtering is treated as
    absent; that's a stub or a URL, not a JD.
    """
    if not resume_text or not jd_text:
        return None
    a, b = _tokens(resume_text), _tokens(jd_text)
    if len(a) < 20 or len(b) < 20:
        return None
    return _cosine(a, b)


def fit_band(index: Optional[float]) -> Optional[str]:
    """Human-facing bucket for a fit index.

    The raw cosine is deliberately never shown. A user reading "0.27" next to
    the word "fit" will read it as 27% fit, which is not what it means and not
    a claim this arithmetic can support. Three buckets is about as much
    resolution as bag-of-words overlap honestly carries.
    """
    if index is None:
        return None
    if index >= FIT_CEILING:
        return "Strong"
    if index >= (FIT_FLOOR + FIT_CEILING) / 2:
        return "Moderate"
    return "Low"


def _fit_points(index: Optional[float]) -> float:
    if index is None:
        return 0.0
    if index <= FIT_FLOOR:
        return 0.0
    scaled = (index - FIT_FLOOR) / (FIT_CEILING - FIT_FLOOR)
    return min(scaled, 1.0) * W_FIT


# --------------------------------------------------------------------------- #
# Meeting quality
# --------------------------------------------------------------------------- #
def meeting_quality(meetings: Iterable[dict]) -> Optional[float]:
    """Blend the latest meeting's two quality reads into one 0-100 number.

    `meetings` is an iterable of dicts with `when`, `my_performance` and
    `employer_engagement`; the caller is responsible for having sorted nothing,
    since this picks the latest itself.

    Latest rather than averaged, for the same reason the score rollup takes the
    latest reading: averaging a strong early screen against a bad recent panel
    produces a number describing a moment that never happened, and it gets more
    sluggish exactly as more evidence arrives. Depth is credited separately, as
    a count, rather than by letting old meetings drag the quality term.

    Either field alone is enough -- if only one is filled in, that one *is* the
    quality. Meetings with neither are invisible here.
    """
    usable = [
        m for m in meetings
        if m.get("my_performance") is not None or m.get("employer_engagement") is not None
    ]
    if not usable:
        return None
    # Partition rather than sorting on a (has_date, date) tuple: two undated
    # meetings would make that tuple compare None against None, which raises.
    # Undated meetings fall back to insertion order, which for an ORM
    # relationship is the ordering the model declares.
    dated = [m for m in usable if m.get("when") is not None]
    latest = max(dated, key=lambda m: m["when"]) if dated else usable[-1]
    perf = latest.get("my_performance")
    eng = latest.get("employer_engagement")
    if perf is None:
        return float(eng)
    if eng is None:
        return float(perf)
    return ENGAGEMENT_WEIGHT * eng + PERFORMANCE_WEIGHT * perf


# --------------------------------------------------------------------------- #
# The forecast itself
# --------------------------------------------------------------------------- #
def automated_forecast(
    stage: Optional[str],
    source: Optional[str],
    meetings: Optional[Iterable[dict]] = None,
    resume_text: Optional[str] = None,
    jd_text: Optional[str] = None,
) -> dict:
    """Derive a forecast category from the four declared inputs.

    Returns a dict carrying the category, the 0-100 total behind it, the four
    component point values, a `confidence` describing how much of the model
    actually had data, and a one-line `reason`. The components are returned
    rather than hidden because a forecast you can't take apart is one you can
    only either believe or ignore.

    Closed stages short-circuit. A Closed Won application is not forecast, it
    is finished, and running the arithmetic on it would produce the absurdity
    of a won deal reading Best Case because nobody filled in the meeting
    fields. Closed Lost goes to Pipeline for the same reason in reverse.
    """
    meetings = list(meetings or [])

    if stage == "Closed Won":
        return _result(COMMIT, 100.0, _no_components(), "high",
                       "Closed Won — this one is decided.")
    if stage == "Closed Lost":
        return _result(PIPELINE, 0.0, _no_components(), "high",
                       "Closed Lost — this one is decided.")

    quality = meeting_quality(meetings)
    scored_meetings = sum(
        1 for m in meetings
        if m.get("my_performance") is not None or m.get("employer_engagement") is not None
    )
    index = fit_index(resume_text, jd_text)

    stage_pts = float(STAGE_POINTS.get(stage or "", 0))
    source_pts = float(SOURCE_POINTS.get(source or "", 0))
    fit_pts = _fit_points(index)
    if quality is None:
        meeting_pts = 0.0
    else:
        meeting_pts = (quality / 100.0) * MAX_QUALITY_POINTS + min(
            scored_meetings * DEPTH_POINTS_PER_MEETING, MAX_DEPTH_POINTS
        )

    total = stage_pts + source_pts + fit_pts + meeting_pts
    components = {
        "stage": round(stage_pts, 1),
        "meetings": round(meeting_pts, 1),
        "fit": round(fit_pts, 1),
        "source": round(source_pts, 1),
        "quality": None if quality is None else round(quality, 1),
        "fit_index": None if index is None else round(index, 3),
        "fit_band": fit_band(index),
        "scored_meetings": scored_meetings,
    }

    # Confidence counts how many of the four components had real data behind
    # them, and is reported separately from the category on purpose. A Pipeline
    # that means "I have nothing to go on" and a Pipeline that means "I have
    # plenty to go on and it's bad" are the same category and completely
    # different situations; folding that distinction into the band would hide
    # it, so it rides alongside instead.
    have = sum([
        quality is not None,
        index is not None,
        source is not None,
        # Stage counts as evidence only once the pursuit is past Qualification.
        # Sitting at Qualification is the state every application is born in
        # (models.DEFAULT_STAGE) -- it is a fact about the column default, not
        # something anybody learned, and counting it would let a brand-new
        # empty record claim it had something to go on.
        stage_pts >= STAGE_POINTS["Discovery"],
    ])
    confidence = "none" if have == 0 else "thin" if have <= 2 else "ok"

    category = _band_for(total)

    return _result(category, total, components, confidence,
                   _reason(category, confidence, components, quality, index, source))


def _band_for(total: float) -> str:
    """Map the 0-100 total onto a category.

    Split out from `automated_forecast` so the boundary itself is testable
    without assembling a whole application around it. The comparisons are
    inclusive at the bottom of each band: the user defined Commit as "75% or
    higher", so exactly 75 is Commit.
    """
    if total >= COMMIT_AT:
        return COMMIT
    if total >= BEST_CASE_AT:
        return BEST_CASE
    return PIPELINE


def _no_components() -> dict:
    """The components dict a short-circuited (closed) forecast returns.

    Same keys as the computed path, all empty. Returning a different shape for
    the closed case would push a `{% if %}` into every template that renders a
    breakdown, and the first one to forget it would raise on a won deal.
    """
    return {
        "stage": 0.0, "meetings": 0.0, "fit": 0.0, "source": 0.0,
        "quality": None, "fit_index": None, "fit_band": None,
        "scored_meetings": 0,
    }


def _result(category: str, total: float, components: dict,
            confidence: str, reason: str) -> dict:
    return {
        "category": category,
        "total": round(total),
        "components": components,
        "confidence": confidence,
        "reason": reason,
    }


def _reason(category, confidence, components, quality, index, source) -> str:
    """One line naming the actual driver, not a summary of the inputs."""
    if confidence == "none":
        return "Nothing to read yet — no scored meetings, no fit, no source."
    bits = []
    if quality is not None:
        n = components["scored_meetings"]
        bits.append(
            f"meeting quality {round(quality)} across {n} scored "
            f"{'meeting' if n == 1 else 'meetings'}"
        )
    if source:
        bits.append(f"{source.lower()} origin")
    if index is not None:
        bits.append(f"{components['fit_band'].lower()} resume/JD overlap")
    else:
        bits.append("no JD or resume text to compare")
    lead = {
        COMMIT: "Commit:",
        BEST_CASE: "Best Case:",
        PIPELINE: "Pipeline:",
    }[category]
    tail = " (thin evidence)" if confidence == "thin" else ""
    return f"{lead} {', '.join(bits)}.{tail}"
