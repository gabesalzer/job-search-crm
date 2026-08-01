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
W_STAGE = 25
W_MEETINGS = 30
W_EMAIL = 10
W_FIT = 15
W_SOURCE = 10
W_CHAMPION = 10

# Fit fell from 20 to 15 in the consolidation, and it should keep falling if it
# ever competes with something better. Bag-of-words overlap was carrying a fifth
# of the whole model while being, by its own docstring, the weakest evidence in
# it -- Condor was earning 19.3 of 20 points from vocabulary similarity while
# the app had no idea how any of its meetings went.
#
# Email is deliberately a third of meetings. A thread is real evidence, but a
# recruiter typing "great, let's find time" is not the same order of information
# as an hour in front of a hiring manager, and the old rollup's habit of letting
# one email wholesale replace a meeting's reading is the exact failure this
# split exists to prevent.

# Stage contributes as an *input*, not a cap. A strong early pursuit can
# therefore out-forecast a limp late one, which is the intended behavior: at
# Discovery (10) with perfect meetings (30), perfect fit (15), a referral (10)
# and a champion (10) you reach exactly 75 and read Commit. The
# counter-argument -- that "more likely than not to end in an accepted offer"
# is a claim about the *whole* remaining path, and no amount of first-call
# warmth should let a Qualification-stage record claim it -- is a real one, and
# the reason the stage weight rises steeply rather than linearly. Qualification
# is worth almost nothing.
STAGE_POINTS = {
    "Staging": 0,             # pre-application; there is no pursuit yet
    "Qualification": 3,
    "Discovery": 10,
    "Takehome": 16,
    "Executive Signoff": 21,
    "Negotiation": 25,
    "Closed Won": 25,
    "Closed Lost": 0,
}

# Referral is the highest-converting origin by a wide margin, which is the whole
# reason the Staging stage exists. Recruiter Inbound sits well above Outbound
# because someone already decided you were worth contacting. Outbound is not
# zero -- a submitted application is still a real pursuit -- but it carries no
# evidence that anyone on the other side has noticed.
SOURCE_POINTS = {
    "Referral": 10,
    "Recruiter Inbound": 6,
    "Outbound": 2,
}

# A champion is someone inside the company actively spending their own capital
# to get you hired -- pushing the process along, arguing for you in a room you
# aren't in. It is not "I know someone there", and it is not a friendly
# interviewer. The distinction is the whole reason it earns as much as source
# does: a warm introduction gets your resume read once, while a champion keeps
# working after every meeting ends.
#
# None means you haven't formed a view and is not the same as False. Neither
# earns points, but False counts as evidence and None does not, so declining to
# answer never quietly reads as "no".
CHAMPION_POINTS = 10

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
MAX_MEETING_DEPTH = 6
MAX_MEETING_QUALITY = W_MEETINGS - MAX_MEETING_DEPTH  # 24

# Threads get the same shape on a smaller budget. One point per rated thread
# rather than two: a company that emails you four times has told you less than
# a company that met you twice, and the depth term should say so.
DEPTH_POINTS_PER_THREAD = 1
MAX_EMAIL_DEPTH = 2
MAX_EMAIL_QUALITY = W_EMAIL - MAX_EMAIL_DEPTH  # 8


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
    because it is a good judge of fit. That is why it carries half the weight
    meetings do despite being the only component that reads the actual role,
    and why the UI shows it as a band rather than as a number that would invite
    false precision.

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
def _rating(activity: dict) -> Optional[float]:
    """One 0-100 quality reading for a single activity, or None if unrated.

    Precedence, and the reason for it. The decomposed fields win because they
    say more: `my_performance` and `employer_engagement` are two independent
    causes, and the 60/40 blend between them is itself a claim about which one
    produces offers. The flat `score` covers the same ground at lower
    resolution, so it is a fallback, not a peer.

    Reading them in this order is also what stops the consolidation from
    counting one judgment twice. Rate a meeting on both the decomposed fields
    and the flat score -- which is the same opinion written down twice -- and
    only the decomposed reading is used.

    The two are not quite the same scale, and pretending otherwise would be
    dishonest. `score` answers "how likely is this to end Closed Won"; the
    blend answers "how did this interaction go". They correlate strongly and
    both run 0-100 in the same direction, which is enough for something that
    only fires when the better fields are blank -- but it is exactly why it is
    a fallback, and why the breakdown names which one it used.
    """
    perf = activity.get("my_performance")
    eng = activity.get("employer_engagement")
    if perf is not None and eng is not None:
        return ENGAGEMENT_WEIGHT * eng + PERFORMANCE_WEIGHT * perf
    if eng is not None:
        return float(eng)
    if perf is not None:
        return float(perf)
    score = activity.get("score")
    return None if score is None else float(score)


def activity_quality(activities: Iterable[dict]) -> Optional[float]:
    """Blend the latest activity's quality reads into one 0-100 number.

    `activities` is an iterable of dicts with `when`, `my_performance`,
    `employer_engagement` and `score`; the caller is responsible for having
    sorted nothing, since this picks the latest itself.

    Latest rather than averaged, for the same reason the score rollup takes the
    latest reading: averaging a strong early screen against a bad recent panel
    produces a number describing a moment that never happened, and it gets more
    sluggish exactly as more evidence arrives. Depth is credited separately, as
    a count, rather than by letting old meetings drag the quality term.

    Either field alone is enough -- if only one is filled in, that one *is* the
    quality. Activities carrying no reading at all are invisible here.
    """
    usable = [a for a in activities if _rating(a) is not None]
    if not usable:
        return None
    # Partition rather than sorting on a (has_date, date) tuple: two undated
    # meetings would make that tuple compare None against None, which raises.
    # Undated meetings fall back to insertion order, which for an ORM
    # relationship is the ordering the model declares.
    dated = [a for a in usable if a.get("when") is not None]
    latest = max(dated, key=lambda a: a["when"]) if dated else usable[-1]
    return _rating(latest)


# --------------------------------------------------------------------------- #
# The forecast itself
# --------------------------------------------------------------------------- #
def automated_forecast(
    stage: Optional[str],
    source: Optional[str],
    meetings: Optional[Iterable[dict]] = None,
    threads: Optional[Iterable[dict]] = None,
    resume_text: Optional[str] = None,
    jd_text: Optional[str] = None,
    champion: Optional[bool] = None,
) -> dict:
    """Derive one read from the six declared inputs.

    This replaced a pair of numbers that answered the same question two
    different ways -- a hand-entered score rolled up off the latest scored
    activity, and this derived forecast. Two numbers with no stated rule for
    which one wins cost attention on every glance, and the disagreement between
    them only taught you something if you already knew which to trust. The
    hand-entered score did not disappear in the merge; it moved *inside*, as
    the fallback reading for an activity whose decomposed fields are blank.
    See `_rating`.

    Returns the category, both totals (below), the component point values, a
    `confidence` describing how much of the model actually had data, and a
    one-line `reason`. Components are returned rather than hidden because a
    number you can't take apart is one you can only believe or ignore.

    Two totals, on purpose
    ----------------------
    `total` is the raw sum over all six components, out of 100. Anything not
    filled in scores zero and drags the number down with it.

    `total_known` renormalizes over only the components that had data. It
    answers "given what we actually know, how does this look" -- the right
    question when half a record is empty, and the wrong one when the empty half
    is the half that matters. Not knowing how four meetings went is itself
    information, and `total_known` discards it: a record can climb purely
    because nobody rated anything on it.

    `category` is banded off `total_known`, and `confidence` carries the
    completeness that `total_known` throws away. Banding off the raw `total`
    was the original choice, and it stopped surviving the day this model went
    from four components to six: every record already in the database would
    have dropped roughly twenty points overnight for champion and email fields
    that did not exist the day before, and read as a collapse in prospects
    rather than as two new empty columns. A raw total cannot tell those apart.
    The guard against `total_known`'s own failure -- a record scoring a perfect
    100 off stage and source alone -- is the confidence gate below, which
    refuses to let anything reach Commit until some actual interaction has
    been read.

    Closed stages short-circuit. A Closed Won application is not forecast, it
    is finished, and running the arithmetic on it would produce the absurdity
    of a won deal reading Best Case because nobody filled in the meeting
    fields. Closed Lost goes to Pipeline for the same reason in reverse.
    """
    meetings = list(meetings or [])
    threads = list(threads or [])

    if stage == "Closed Won":
        return _result(COMMIT, 100.0, 100.0, _no_components(), "high",
                       "Closed Won — this one is decided.")
    if stage == "Closed Lost":
        return _result(PIPELINE, 0.0, 0.0, _no_components(), "high",
                       "Closed Lost — this one is decided.")

    quality = activity_quality(meetings)
    email_quality = activity_quality(threads)
    scored_meetings = sum(1 for m in meetings if _rating(m) is not None)
    scored_threads = sum(1 for t in threads if _rating(t) is not None)
    index = fit_index(resume_text, jd_text)

    stage_pts = float(STAGE_POINTS.get(stage or "", 0))
    source_pts = float(SOURCE_POINTS.get(source or "", 0))
    fit_pts = _fit_points(index)
    # `champion` is tri-state and only True earns. False and None both score
    # zero; they differ in whether they count as evidence, which happens below.
    champion_pts = float(CHAMPION_POINTS) if champion else 0.0
    if quality is None:
        meeting_pts = 0.0
    else:
        meeting_pts = (quality / 100.0) * MAX_MEETING_QUALITY + min(
            scored_meetings * DEPTH_POINTS_PER_MEETING, MAX_MEETING_DEPTH
        )
    if email_quality is None:
        email_pts = 0.0
    else:
        email_pts = (email_quality / 100.0) * MAX_EMAIL_QUALITY + min(
            scored_threads * DEPTH_POINTS_PER_THREAD, MAX_EMAIL_DEPTH
        )

    total = (stage_pts + source_pts + fit_pts + meeting_pts
             + email_pts + champion_pts)

    # Renormalization denominator. Stage and source are unconditionally in it
    # because every application has both from the moment it exists -- there is
    # no such thing as an application with an unknown stage -- which is what
    # stops `total_known` from ever being computed over an empty denominator,
    # or off a single lucky component.
    known = [
        (True, W_STAGE, stage_pts),
        (True, W_SOURCE, source_pts),
        (quality is not None, W_MEETINGS, meeting_pts),
        (email_quality is not None, W_EMAIL, email_pts),
        (index is not None, W_FIT, fit_pts),
        (champion is not None, W_CHAMPION, champion_pts),
    ]
    available = sum(w for present, w, _ in known if present)
    earned = sum(p for present, _, p in known if present)
    total_known = (earned / available) * 100.0 if available else 0.0

    components = {
        "stage": round(stage_pts, 1),
        "meetings": round(meeting_pts, 1),
        "email": round(email_pts, 1),
        "fit": round(fit_pts, 1),
        "source": round(source_pts, 1),
        "champion": round(champion_pts, 1),
        "quality": None if quality is None else round(quality, 1),
        "email_quality": None if email_quality is None else round(email_quality, 1),
        "fit_index": None if index is None else round(index, 3),
        "fit_band": fit_band(index),
        "scored_meetings": scored_meetings,
        "scored_threads": scored_threads,
        # How many exist at all, rated or not. `scored_*` alone cannot answer
        # "is there anything here", and the difference matters to whoever reads
        # the breakdown even though it does not matter to the arithmetic. A
        # zero because nothing is linked is a data-entry gap; a zero because
        # nothing is rated is a judgment not yet made. Both earn no points and
        # both drop their weight out of `available` -- absence of evidence is
        # not evidence either way -- but they call for different actions, and a
        # breakdown that renders them with one sentence sends you looking in
        # the wrong place.
        "total_meetings": len(meetings),
        "total_threads": len(threads),
        # How many of the 100 points were even in play. Shown so "46" can be
        # read as "46 of a possible 50" rather than as a flat failure.
        "available": available,
    }

    # Confidence counts how many of the six components had real data behind
    # them, and is reported separately from the category on purpose. A Pipeline
    # that means "I have nothing to go on" and a Pipeline that means "I have
    # plenty to go on and it's bad" are the same category and completely
    # different situations; folding that distinction into the band would hide
    # it, so it rides alongside instead.
    have = sum([
        quality is not None,
        email_quality is not None,
        index is not None,
        source is not None,
        # False is a real answer and counts; None means you haven't looked.
        champion is not None,
        # Stage counts as evidence only once the pursuit is past Qualification.
        # Sitting at Qualification is the state every application is born in
        # (models.DEFAULT_STAGE) -- it is a fact about the column default, not
        # something anybody learned, and counting it would let a brand-new
        # empty record claim it had something to go on.
        stage_pts >= STAGE_POINTS["Discovery"],
    ])
    confidence = ("none" if have == 0 else "thin" if have <= 2
                  else "ok" if have <= 4 else "high")
    # Interaction evidence is privileged over the rest. Stage, source, fit and
    # champion are all facts about the *setup* -- knowable before anyone has
    # spoken to you -- and a record can satisfy three of them while the app has
    # no idea how a single conversation went. Letting that read "ok evidence"
    # is how Condor came to sit at 46 with a confident-looking label and an
    # empty meetings column. Nothing is above thin until something happened.
    if quality is None and email_quality is None:
        confidence = "none" if confidence == "none" else "thin"

    # Banded off `total_known`, not `total`. The raw total conflates two
    # different things -- how good this looks, and how much of the form you
    # filled in -- and adding champion and email as components made that
    # conflation untenable: every record in the app would have silently dropped
    # 20 points for fields that did not exist yesterday. `total_known` is a
    # weighted average over the components that spoke, so it measures quality
    # alone, and `confidence` measures completeness alongside it. One number per
    # question rather than one number doing both badly.
    #
    # That split has a failure mode which the cap below closes. A Negotiation
    # record with a referral and nothing else scores 35 of an available 35 --
    # a perfect 100 built entirely out of facts known before anyone spoke to
    # you. So a category above Best Case requires evidence that something
    # actually happened, and `confidence` is already gated on exactly that.
    category = _band_for(total_known)
    if confidence == "none":
        category = PIPELINE
    elif confidence == "thin" and category == COMMIT:
        category = BEST_CASE

    return _result(category, total, total_known, components, confidence,
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
        "stage": 0.0, "meetings": 0.0, "email": 0.0, "fit": 0.0,
        "source": 0.0, "champion": 0.0,
        "quality": None, "email_quality": None,
        "fit_index": None, "fit_band": None,
        "scored_meetings": 0, "scored_threads": 0,
        "total_meetings": 0, "total_threads": 0,
        "available": 0,
    }


def _result(category: str, total: float, total_known: float, components: dict,
            confidence: str, reason: str) -> dict:
    return {
        "category": category,
        "total": round(total),
        # Kept separate rather than replacing `total`, so a template that wants
        # "46 of a possible 50" and one that wants "92" can both be served
        # without either recomputing the arithmetic for itself.
        "total_known": round(total_known),
        "components": components,
        "confidence": confidence,
        "reason": reason,
    }


def _reason(category, confidence, components, quality, index, source) -> str:
    """One line naming the actual driver, not a summary of the inputs."""
    if confidence == "none":
        return "Nothing to read yet — no rated activity, no fit, no source."
    bits = []
    if quality is not None:
        n = components["scored_meetings"]
        bits.append(
            f"meeting quality {round(quality)} across {n} rated "
            f"{'meeting' if n == 1 else 'meetings'}"
        )
    if components["email_quality"] is not None:
        n = components["scored_threads"]
        bits.append(
            f"email quality {round(components['email_quality'])} across "
            f"{n} rated {'thread' if n == 1 else 'threads'}"
        )
    if components["champion"]:
        bits.append("a champion inside")
    if source:
        bits.append(f"{source.lower()} origin")
    if index is not None:
        # The points are continuous while the band is three buckets, so a 0.292
        # index earns 96% of the fit weight while still reading "moderate".
        # Naming the points alongside the band stops the line contradicting the
        # breakdown directly above it.
        bits.append(
            f"{components['fit_band'].lower()} resume/JD overlap "
            f"({components['fit']}/{W_FIT})"
        )
    else:
        bits.append("no JD or resume text to compare")
    lead = {
        COMMIT: "Commit:",
        BEST_CASE: "Best Case:",
        PIPELINE: "Pipeline:",
    }[category]
    tail = " (thin evidence)" if confidence == "thin" else ""
    return f"{lead} {', '.join(bits)}.{tail}"
