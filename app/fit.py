"""How well an opportunity matches what you said you wanted.

Stdlib-only and ORM-free, like ``forecast.py``, ``analytics.py`` and the rest.
Plain dicts in, plain dicts out.

This is the second axis. Everything else the app scores answers *will I win
this* -- the forecast, the thread reads, the meeting ratings. Nothing answered
*do I want it*, even though the forecast's own definition ends "...in an offer
you accept" and quietly assumed that half. Fit is that half, made explicit.

The two together are the point. A high fit you are unlikely to win is worth
fighting for; a low fit you are cruising through is worth closing out. Neither
number says that on its own.

Three rules, and each is the same rule that governs every other score here.

**Blank is not zero.** An unrated criterion is "I have not judged this yet",
which is the state most pairs are in most of the time. It stays out of the
average rather than dragging it down, and the count of what was actually rated
travels with the number so a 9.0 from one criterion never reads like a 9.0 from
six.

**Blank never disqualifies.** Unknown and failed are opposite claims. Treating
the first as the second would disqualify every application the moment the
criteria were written, which is exactly when none of them have been rated.

**A floor beats an average.** Any rated criterion below the threshold
disqualifies outright, however good the mean. An average is easy to talk
yourself into -- one glorious axis pulls a fatal one up out of sight -- and the
whole reason to write criteria down beforehand is to stop that happening in the
moment. Same shape as the forecast's confidence gate, where `none` forces
Pipeline no matter how strong the setup facts look.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# The rating scale. Ten points because that is the granularity a person
# actually holds an opinion at -- finer would be false precision, coarser
# loses the difference between "fine" and "good".
SCALE_MIN = 1
SCALE_MAX = 10

# Used when the singleton row predates the column, or holds NULL. Four is a
# starting point rather than a considered constant: it is meant to be tuned on
# the page once real ratings exist, which is the only way to know where your
# own floor actually sits.
DEFAULT_THRESHOLD = 4


# The axes to start from, seeded once on first visit. Gabe's, not defaults --
# they are his evaluation vectors, and the descriptions are prompts to himself
# rather than definitions, because the point of the description field is to
# make a rating six weeks from now mean the same thing it means today.
STARTER_CRITERIA = [
    ("Talent density",
     "The people I would work with and for. Would this raise my ceiling?"),
    ("Role opportunity",
     "Scope, ownership, and what the job itself lets me build or prove."),
    ("Company opportunity",
     "Trajectory of the business. Is it going somewhere worth being early to?"),
    ("Company brand",
     "What this name does for me afterwards, on a resume and in a network."),
    ("Lifestyle fit",
     "Hours, travel, location, and what the job costs outside work."),
    ("Compensation",
     "Cash, equity and the realistic value of it, against what I need."),
]


def clamp_score(value: Any) -> Optional[int]:
    """Parse one rating off a form field, or None.

    Blank is a legitimate answer and returns None. Anything outside the scale
    is **rejected** rather than clamped: a 47 means the scale was misread, and
    quietly storing 10 would hide that behind a plausible value -- the same
    reasoning `thread_read._one_number` uses for refusing an out-of-range
    reading instead of pinning it to the ceiling.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    if SCALE_MIN <= number <= SCALE_MAX:
        return number
    return None


def threshold_of(raw: Optional[int]) -> int:
    """The disqualifying floor, defaulted and kept inside the scale."""
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return max(SCALE_MIN, min(SCALE_MAX, number))


def score(ratings: Optional[Iterable[Dict[str, Any]]],
          *, threshold: Optional[int] = None) -> Dict[str, Any]:
    """Roll a set of per-criterion ratings into one reading.

    ``ratings`` is a list of ``{"name": str, "score": int|None}``. Returns the
    mean over rated criteria, how many were rated out of how many exist,
    whether anything fell below the floor, and which ones did.

    `failing` carries names rather than a count, because "disqualified" on its
    own is not actionable -- you need to know *which* axis failed to decide
    whether to walk away or to go and change the fact.
    """
    rows = list(ratings or [])
    floor = threshold_of(threshold)
    rated = [r for r in rows if r.get("score") is not None]
    failing = [r.get("name") for r in rated if r["score"] < floor]

    out: Dict[str, Any] = {
        "total": len(rows),
        "rated": len(rated),
        "threshold": floor,
        "mean": None,
        "disqualified": bool(failing),
        "failing": failing,
        # True once every criterion has a rating. A mean over two of seven is
        # a real number about two criteria, not about the opportunity, and the
        # page says so rather than presenting it as complete.
        "complete": bool(rows) and len(rated) == len(rows),
    }
    if rated:
        out["mean"] = round(sum(r["score"] for r in rated) / len(rated), 1)
    return out


def rank(applications: Optional[List[Dict[str, Any]]],
         *, threshold: Optional[int] = None) -> List[Dict[str, Any]]:
    """Order applications by fit, disqualified ones last.

    Sorting puts anything disqualified at the bottom regardless of its mean,
    so the list reads the way the rule works: a fatal axis is not a slightly
    lower position, it is out. Unrated records sort below rated ones, because
    "no opinion" should not outrank a considered 6.
    """
    out = []
    for app in applications or []:
        reading = score(app.get("ratings"), threshold=threshold)
        out.append({**app, "fit": reading})
    out.sort(key=lambda a: (
        a["fit"]["disqualified"],
        a["fit"]["mean"] is None,
        -(a["fit"]["mean"] or 0),
        (a.get("company") or "").lower(),
    ))
    return out
