"""What the chat is allowed to change, and what it is refused.

Stdlib only, against the real module. This is a boundary test file: everything
here is about what happens when the model proposes something wrong, malformed,
or outside the vocabulary — which for output parsed out of a language model is
the expected case, not the exceptional one.

Run: python3 tests/test_viewspec.py
"""
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import viewspec  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


VOCAB = {
    "source": ["Referral", "Recruiter Inbound", "Outbound"],
    "stage": ["Discovery", "Closed Lost", "Negotiation"],
    "lost_category": ["Compensation gap"],
    "resume": ["Resume v3"],
    "company": ["Condor", "Plaid"],
}


def app(app_id, **kw):
    base = {"id": app_id, "company": "Condor", "source": "Referral",
            "stage": "Discovery", "resume_label": "Resume v3",
            "lost_category": None, "applied_date": datetime(2026, 6, 1)}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Pulling the block out of a reply
# --------------------------------------------------------------------------- #
prose, block = viewspec.extract_block(
    'Referrals do better.\n\n```view\n{"source": ["Referral"]}\n```')
check("the block is lifted out of the reply", block == '{"source": ["Referral"]}')
check("the prose is left clean", prose == "Referrals do better.",
      "raw JSON must never render in the transcript")

check("a reply with no block is returned unchanged",
      viewspec.extract_block("Just an answer.") == ("Just an answer.", None))
check("merely discussing filtering does not filter",
      viewspec.extract_block(
          "You could look at only referrals if you wanted.")[1] is None,
      "the fence is what makes it a request rather than a remark")

# --------------------------------------------------------------------------- #
# Validation and rejection
# --------------------------------------------------------------------------- #
spec, rejected = viewspec.parse('{"source": ["Referral"]}', vocabulary=VOCAB)
check("a valid filter is accepted", spec == {"source": ["Referral"]})
check("and reports nothing rejected", rejected == [])

spec, rejected = viewspec.parse('{"source": ["referral"]}', vocabulary=VOCAB)
check("matching is case-insensitive but stores the canonical value",
      spec == {"source": ["Referral"]})

spec, rejected = viewspec.parse('{"source": ["Cold DM"]}', vocabulary=VOCAB)
check("a value no record carries is rejected, not applied", spec == {})
check("and the reader is told why", rejected and "Cold DM" in rejected[0],
      "an unknown value would filter to an empty set, which renders as "
      "'not enough data' and is indistinguishable from a real finding")

spec, rejected = viewspec.parse(
    '{"source": ["Referral", "Cold DM"]}', vocabulary=VOCAB)
check("the good half of a partly-wrong filter still applies",
      spec == {"source": ["Referral"]})
check("and the dropped half is still reported", len(rejected) == 1)

spec, rejected = viewspec.parse('{"vibes": ["good"]}', vocabulary=VOCAB)
check("an invented field is rejected", spec == {})
check("and named in the rejection", "vibes" in rejected[0])

spec, rejected = viewspec.parse("not json at all", vocabulary=VOCAB)
check("unparseable JSON applies nothing", spec == {})
check("and says so rather than failing silently", len(rejected) == 1)

check("a JSON array is refused as not being an object",
      viewspec.parse('["Referral"]', vocabulary=VOCAB)[0] == {})
check("an empty block is a no-op", viewspec.parse(None)[0] == {})
check("parsing never raises on garbage",
      viewspec.parse('{"since": {"nested": 1}}', vocabulary=VOCAB)[0] == {})

spec, rejected = viewspec.parse(
    '{"company": %s}' % str([str(i) for i in range(20)]).replace("'", '"'),
    vocabulary=None)
check("a filter listing more values than a filter would is refused",
      spec == {} and "more than a filter" in rejected[0])

# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
spec, rejected = viewspec.parse('{"since": "2026-06-01"}', vocabulary=VOCAB)
check("an ISO date parses", spec["since"] == datetime(2026, 6, 1))
check("a written date parses too",
      viewspec.parse('{"until": "June 30, 2026"}')[0]["until"]
      == datetime(2026, 6, 30))
spec, rejected = viewspec.parse('{"since": "last spring"}', vocabulary=VOCAB)
check("a vague date is rejected rather than guessed at",
      spec == {} and "isn't a date" in rejected[0])

# --------------------------------------------------------------------------- #
# compare_by
# --------------------------------------------------------------------------- #
check("a valid compare field is accepted",
      viewspec.parse('{"compare_by": "source"}')[0] == {"compare_by": "source"})
check("an invalid compare field is rejected",
      viewspec.parse('{"compare_by": "astrological sign"}')[0] == {})

# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
POOL = [
    app(1, source="Referral"),
    app(2, source="Outbound"),
    app(3, source=None),
    app(4, source="Referral", applied_date=datetime(2026, 1, 5)),
    app(5, source="Referral", applied_date=None),
]

check("no spec returns everything", len(viewspec.apply(POOL, None)) == 5)
check("a text filter narrows",
      [a["id"] for a in viewspec.apply(POOL, {"source": ["Referral"]})] == [1, 4, 5])
check("a record with no value for the filtered field is excluded",
      3 not in [a["id"] for a in viewspec.apply(POOL, {"source": ["Referral"]})],
      "'source is Referral' is a claim a blank cannot satisfy")
check("two values in one field are an OR",
      len(viewspec.apply(POOL, {"source": ["Referral", "Outbound"]})) == 4)

since = viewspec.apply(POOL, {"since": datetime(2026, 5, 1)})
check("a date bound narrows", [a["id"] for a in since] == [1, 2, 3])
check("a record with no applied date drops out of a dated view",
      5 not in [a["id"] for a in since])

both = viewspec.apply(POOL, {"source": ["Referral"],
                             "since": datetime(2026, 5, 1)})
check("filters across fields are an AND", [a["id"] for a in both] == [1])
check("apply never widens the set",
      len(viewspec.apply(POOL, {"source": ["Referral"]})) <= len(POOL))

# --------------------------------------------------------------------------- #
# cohorts
# --------------------------------------------------------------------------- #
groups = dict(viewspec.cohorts(POOL, "source"))
check("cohorts split by a field",
      sorted(groups) == ["Not recorded", "Outbound", "Referral"])
check("a missing value becomes an explicit group, not a silent drop",
      len(groups["Not recorded"]) == 1)
check("groups are ordered commonest first, Not recorded last",
      [name for name, _ in viewspec.cohorts(POOL, "source")][-1] == "Not recorded")
check("no compare field means no cohorts", viewspec.cohorts(POOL, None) == [])

# --------------------------------------------------------------------------- #
# Round-tripping through the query string
# --------------------------------------------------------------------------- #
original = {"source": ["Referral", "Outbound"], "since": datetime(2026, 6, 1),
            "compare_by": "source"}
query = viewspec.to_query(original)
back, rejected = viewspec.from_query(query, vocabulary=VOCAB)
check("a spec survives a round trip through the URL", back == original,
      "the URL is the only store, so a lossy round trip loses the view")
check("and round-tripping rejects nothing valid", rejected == [])

check("a hand-typed bad URL is rejected exactly like a hallucinated one",
      viewspec.from_query({"source": "Nonsense"}, vocabulary=VOCAB)[0] == {},
      "there is no privileged source of specs")
check("an empty query is an empty spec",
      viewspec.from_query({}, vocabulary=VOCAB) == ({}, []))
check("unrelated query params are ignored",
      viewspec.from_query({"error": "boom", "rejected": "x"},
                          vocabulary=VOCAB) == ({}, []))

# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #
chips = viewspec.describe(original)
check("every constraint gets a chip", len(chips) == 3)
check("a chip names the parameter that removes it",
      {c["param"] for c in chips} == {"source", "since", "compare_by"})
check("a chip reads as a sentence",
      any("source is Referral or Outbound" == c["label"] for c in chips))
check("no spec means no chips", viewspec.describe(None) == [])

# --------------------------------------------------------------------------- #
# The prompt and the parser are one contract
# --------------------------------------------------------------------------- #
for field in sorted(viewspec.TEXT_FIELDS | viewspec.DATE_FIELDS | {"compare_by"}):
    check("the prompt tells the model about '{}'".format(field),
          field in viewspec.PROMPT,
          "a field the parser accepts but the prompt never mentions is dead")
check("the prompt shows the fence the parser looks for",
      "```view" in viewspec.PROMPT)
check("the prompt warns that filtering shrinks the sample",
      "below three" in viewspec.PROMPT)

print("\n{} failed".format(len(failures)) if failures else "\nall passed")
sys.exit(1 if failures else 0)
