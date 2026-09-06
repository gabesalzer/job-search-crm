"""The fit score: the average, the floor, and what a blank means.

Stdlib only, against the real module. The three rules under test are the ones
that make this score honest rather than merely present, and each of them is a
place where the obvious implementation is wrong.

Run: python3 tests/test_fit.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import fit  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


def r(name, value):
    return {"name": name, "score": value}


# --------------------------------------------------------------------------- #
# Parsing one rating
# --------------------------------------------------------------------------- #
check("a number in range parses", fit.clamp_score("7") == 7)
check("blank is a legitimate answer, not a zero", fit.clamp_score("") is None)
check("whitespace is blank too", fit.clamp_score("   ") is None)
check("None is blank", fit.clamp_score(None) is None)
check("out of range is refused rather than clamped",
      fit.clamp_score("47") is None and fit.clamp_score("0") is None,
      "a 47 means the scale was misread, and storing 10 would hide that "
      "behind a plausible value")
check("both ends of the scale are legal",
      (fit.clamp_score("1"), fit.clamp_score("10")) == (1, 10))
check("nonsense is blank, not an error", fit.clamp_score("high") is None)

# --------------------------------------------------------------------------- #
# The threshold
# --------------------------------------------------------------------------- #
check("a missing threshold falls back to the default",
      fit.threshold_of(None) == fit.DEFAULT_THRESHOLD)
check("a stored threshold is used", fit.threshold_of(6) == 6)
check("a threshold outside the scale is pulled inside it",
      fit.threshold_of(99) == fit.SCALE_MAX and fit.threshold_of(-4) == fit.SCALE_MIN)
check("garbage falls back rather than raising",
      fit.threshold_of("nine") == fit.DEFAULT_THRESHOLD)

# --------------------------------------------------------------------------- #
# The average
# --------------------------------------------------------------------------- #
full = fit.score([r("Comp", 8), r("Scope", 6), r("Brand", 7)], threshold=4)
check("the score is the plain average", full["mean"] == 7.0)
check("and it reports how much of the list it covers",
      (full["rated"], full["total"], full["complete"]) == (3, 3, True))

partial = fit.score([r("Comp", 9), r("Scope", None), r("Brand", None)], threshold=4)
check("a blank stays out of the average rather than dragging it down",
      partial["mean"] == 9.0,
      "counting blanks as zero would make every part-rated record look awful")
check("but the page can tell it is only part-rated",
      (partial["rated"], partial["total"], partial["complete"]) == (1, 3, False))

check("nothing rated yields no score at all",
      fit.score([r("Comp", None)], threshold=4)["mean"] is None)
check("an empty criteria list does not raise",
      fit.score([], threshold=4)["mean"] is None)
check("None instead of a list does not raise",
      fit.score(None, threshold=4)["total"] == 0)

# --------------------------------------------------------------------------- #
# The floor
# --------------------------------------------------------------------------- #
fatal = fit.score([r("Comp", 10), r("Lifestyle", 2), r("Brand", 9)], threshold=4)
check("one axis below the floor disqualifies the whole thing",
      fatal["disqualified"] is True,
      "an average is easy to talk yourself into; one glorious axis must not "
      "pull a fatal one up out of sight")
check("and the average is still reported alongside it",
      fatal["mean"] == 7.0,
      "the number is not suppressed -- you need to see what you were tempted by")
check("the failing axis is named, not just counted",
      fatal["failing"] == ["Lifestyle"],
      "'disqualified' alone is not actionable; you need to know which axis to "
      "either accept or go and change")

edge = fit.score([r("Comp", 4), r("Scope", 5)], threshold=4)
check("a rating exactly at the floor is not disqualifying",
      edge["disqualified"] is False,
      "the rule is 'below the threshold', so the threshold itself passes")

blanks = fit.score([r("Comp", 8), r("Lifestyle", None)], threshold=4)
check("a blank never disqualifies",
      blanks["disqualified"] is False,
      "unknown and failed are opposite claims -- treating one as the other "
      "would disqualify every application the day the axes were written")

multi = fit.score([r("A", 1), r("B", 2), r("C", 9)], threshold=5)
check("every failing axis is listed", multi["failing"] == ["A", "B"])

check("raising the floor disqualifies more",
      fit.score([r("Comp", 5)], threshold=6)["disqualified"] is True)

# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
ranked = fit.rank([
    {"id": 1, "company": "Alpha", "ratings": [r("A", 9), r("B", 9)]},
    {"id": 2, "company": "Beta", "ratings": [r("A", 10), r("B", 1)]},
    {"id": 3, "company": "Gamma", "ratings": [r("A", 7), r("B", 7)]},
    {"id": 4, "company": "Delta", "ratings": [r("A", None), r("B", None)]},
], threshold=4)
order = [a["company"] for a in ranked]
check("the best fit comes first", order[0] == "Alpha")
check("a disqualified record sorts last however high its average",
      order[-1] == "Beta",
      "Beta averages 5.5 and outranks nothing, because a fatal axis is not a "
      "slightly lower position -- it is out")
check("an unrated record sorts below a rated one",
      order.index("Gamma") < order.index("Delta"),
      "'no opinion' should not outrank a considered 7")
check("ranking preserves the original fields", ranked[0]["id"] == 1)
check("ranking an empty list is fine", fit.rank([], threshold=4) == [])

# --------------------------------------------------------------------------- #
# The starter list
# --------------------------------------------------------------------------- #
names = [n for n, _ in fit.STARTER_CRITERIA]
check("the six axes are seeded",
      names == ["Talent density", "Role opportunity", "Company opportunity",
                "Company brand", "Lifestyle fit", "Compensation"])
check("each carries a description",
      all(blurb.strip() for _, blurb in fit.STARTER_CRITERIA),
      "the description is what keeps a rating comparable six weeks later")

print("\n{} failed".format(len(failures)) if failures else "\nall passed")
sys.exit(1 if failures else 0)
