"""The voice-log parser: every way a proposed change can fail to survive.

Stdlib only, against the real `app/logspec.py`. This module is the only thing
standing between a mis-heard sentence and a write to the record, so the
rejection paths carry far more weight here than the happy one. A proposal that
gets through when it should not is a wrong value in a field, and a wrong value
is indistinguishable from a right one the moment it lands.

Run: python3 tests/test_logspec.py
"""
import json
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import logspec  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


STAGES = ["Staging", "Qualification", "Discovery", "Takehome",
          "Executive Signoff", "Negotiation", "Closed Won", "Closed Lost"]
CATEGORIES = ["Compensation", "Seniority mismatch", "Role scope"]

APPS = [
    {"id": 3, "company": "Condor", "title": "Head of RevOps",
     "stage": "Qualification", "next_steps": "Wait for Todd",
     "notes": "Applied via referral.", "pain": None, "process": None,
     "risks": None, "context": None, "lost_reason": None,
     "people": ["Todd Grant"]},
    {"id": 7, "company": "Sierra", "title": "RevOps Manager",
     "stage": "Discovery", "next_steps": None, "notes": None, "pain": None,
     "process": None, "risks": None, "context": None, "lost_reason": None,
     "people": []},
]


TODAY = date(2026, 9, 6)


def parse(payload, today=TODAY):
    return logspec.parse(json.dumps(payload), applications=APPS,
                         stages=STAGES, categories=CATEGORIES, today=today)


# --------------------------------------------------------------------------- #
# The prompt is generated from the vocabularies, so it cannot drift
# --------------------------------------------------------------------------- #
prompt = logspec.system_prompt(stages=STAGES, categories=CATEGORIES,
                               today=TODAY.isoformat())
for stage in STAGES:
    check("the prompt offers the stage '{}'".format(stage), stage in prompt)
for field in logspec.WRITABLE:
    check("the prompt offers the field '{}'".format(field),
          field.replace("_", " ") in prompt)
check("the prompt says the note is data, not instructions",
      "not a command" in prompt and "DATA" in prompt)
check("the prompt forbids inventing an application id",
      "Never invent one" in prompt)

# The three deliberate exclusions. If one of these ever appears in WRITABLE it
# will be because someone added it without reading why it was left out.
for excluded in ("champion", "score", "manual_forecast", "seniority",
                 "speciality"):
    check("'{}' is not writable from a note".format(excluded),
          excluded not in logspec.WRITABLE)

# Every writable field carries a definition. Without this a field added to
# WRITABLE ships as a bare name the model has to guess the meaning of, which
# is the failure that put "comp is light" under `pain` instead of `risks`.
for field in logspec.WRITABLE:
    check("'{}' has a definition in the prompt".format(field),
          bool(logspec.DEFINITIONS.get(field, "").strip()))
check("the prompt says pain is the employer's problem, not yours",
      "EMPLOYER" in prompt)
check("the prompt distinguishes context from a running log",
      "Not a running log" in prompt)
check("the prompt says the stages are not interview rounds",
      "NOT interview rounds" in prompt)
check("the prompt tells the model what today is", TODAY.isoformat() in prompt)
check("the prompt demands an exact date format", "YYYY-MM-DD" in prompt)


# --------------------------------------------------------------------------- #
# The fenced block has to be deliberate
# --------------------------------------------------------------------------- #
prose, block = logspec.extract_block(
    "You could move Condor to Discovery if the panel is confirmed.")
check("a reply that merely discusses an update proposes nothing",
      block is None)
check("...and its prose survives intact", prose.startswith("You could move"))

prose, block = logspec.extract_block(
    'Heard a stage move.\n```changes\n{"changes": []}\n```\ntrailing')
check("a fenced block is extracted", block == '{"changes": []}')
check("the block is stripped out of the prose",
      "```" not in prose and "Heard a stage move." in prose)

changes, unmatched, questions, rejected = logspec.parse(
    None, applications=APPS, stages=STAGES, categories=CATEGORIES)
check("no block at all proposes nothing", changes == [])
check("...and says so rather than failing silently", len(rejected) == 1)

changes, _, _, rejected = logspec.parse(
    "not json at all", applications=APPS, stages=STAGES, categories=CATEGORIES)
check("malformed JSON proposes nothing", changes == [])
check("...and is reported", "valid JSON" in rejected[0])


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
changes, unmatched, questions, rejected = parse({
    "changes": [
        {"application": 3, "field": "stage", "value": "Discovery",
         "why": "panel being scheduled"},
        {"application": 3, "field": "next_steps",
         "value": "Send Todd the RevOps deck by Friday", "why": "committed"},
        {"application": 7, "field": "notes", "value": "Went quiet.",
         "why": "no reply in two weeks"},
    ],
    "unmatched": ["mentioned a Vercel recruiter — no application on file"],
})
check("three good changes survive", len(changes) == 3, str(rejected))
check("nothing was rejected", rejected == [], str(rejected))
check("the model's unmatched list is passed through",
      unmatched == ["mentioned a Vercel recruiter — no application on file"])
check("a change carries the current value for the review screen",
      changes[1]["current"] == "Wait for Todd")
check("a change carries a stable key naming both id and field",
      changes[0]["key"] == "3:stage")
check("notes default to appending", changes[2]["mode"] == "append")
check("other fields default to replacing", changes[1]["mode"] == "set")
check("the company name rides along for the review screen",
      changes[0]["company"] == "Condor")


# --------------------------------------------------------------------------- #
# Entity resolution: an id that is not in the record
# --------------------------------------------------------------------------- #
changes, _, _, rejected = parse({"changes": [
    {"application": 99, "field": "notes", "value": "Something."},
]})
check("a change to an unknown application is dropped", changes == [])
check("...and names the id it could not find", "99" in rejected[0])

changes, _, _, rejected = parse({"changes": [
    {"field": "notes", "value": "Something."},
]})
check("a change with no application id is dropped", changes == [])

changes, _, _, rejected = parse({"changes": [
    {"application": "Condor", "field": "notes", "value": "Something."},
]})
check("a change naming a company instead of an id is dropped", changes == [])


# --------------------------------------------------------------------------- #
# The field allow-list
# --------------------------------------------------------------------------- #
changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "champion", "value": "true"},
]})
check("a note cannot set champion however it is phrased", changes == [])
check("...and is told which field it tried", "champion" in rejected[0])

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "next steps", "value": "Call Todd"},
]})
check("a field named with a space still resolves",
      len(changes) == 1 and changes[0]["field"] == "next_steps")

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "notes about pain", "value": "x"},
]})
check("field matching is exact, not fuzzy", changes == [])


# --------------------------------------------------------------------------- #
# Picklists are validated against the real vocabulary
# --------------------------------------------------------------------------- #
changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "stage", "value": "Panel"},
]})
check("an invented stage is dropped", changes == [])
check("...and is quoted back", "Panel" in rejected[0])

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "stage", "value": "discovery"},
]})
check("a stage matches case-insensitively and is normalised",
      len(changes) == 1 and changes[0]["value"] == "Discovery")

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "lost_category", "value": "Nonsense"},
]})
check("an invented lost category is dropped", changes == [])

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "stage", "value": "Discovery",
     "mode": "append"},
]})
check("appending to a picklist is silently corrected to replacing",
      len(changes) == 1 and changes[0]["mode"] == "set")


# --------------------------------------------------------------------------- #
# Blank is not zero: a note may never clear a field
# --------------------------------------------------------------------------- #
for empty in ("", "   ", None, 0):
    changes, _, _, rejected = parse({"changes": [
        {"application": 3, "field": "next_steps", "value": empty},
    ]})
    check("an empty value ({!r}) never clears a field".format(empty),
          changes == [])
check("...and the rejection says clearing is a hand edit",
      "hand edit" in rejected[0])


# --------------------------------------------------------------------------- #
# Restating what a field already says is not a change
# --------------------------------------------------------------------------- #
changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "next_steps", "value": "Wait for Todd"},
]})
check("a value identical to the current one is not proposed", changes == [])
check("...and says so rather than vanishing", "already says" in rejected[0])


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #
changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "notes", "value": "x" * 5000},
]})
check("an absurdly long value is dropped", changes == [])

many = [{"application": 3, "field": "notes", "value": "n{}".format(i)}
        for i in range(logspec.MAX_CHANGES + 1)]
changes, _, _, rejected = parse({"changes": many})
check("a note proposing more changes than the cap applies none",
      changes == [])
check("...and suggests dictating it in parts", "in parts" in rejected[0])

changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "notes", "value": "first"},
    {"application": 3, "field": "notes", "value": "second"},
]})
check("the same field proposed twice keeps only the first",
      len(changes) == 1 and changes[0]["value"] == "first")

changes, _, _, rejected = parse({"changes": "not a list"})
check("a changes value that is not a list proposes nothing", changes == [])

changes, _, _, _ = parse({"changes": [None, {"application": 3, "field": "notes",
                                          "value": "ok"}]})
check("a junk entry is dropped without losing the good one alongside it",
      len(changes) == 1)


# --------------------------------------------------------------------------- #
# Dates: strict, because a plausible wrong one is worse than none
# --------------------------------------------------------------------------- #
check("an ISO date parses", logspec.parse_date("2026-10-31") == "2026-10-31")
check("...and is returned normalised", logspec.parse_date("  2026-10-31 ")
      == "2026-10-31")
for bad in ("10/31/26", "31 October 2026", "October 31, 2026", "2026-10",
            "next Friday", "soon", "2026-13-01", "2026-02-31", "", None, 20261031):
    check("{!r} is refused rather than guessed at".format(bad),
          logspec.parse_date(bad) is None)

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "expected_close_date", "value": "2026-10-31",
     "why": "said they would decide by the end of October"},
]})
check("a good close date survives",
      len(changes) == 1 and changes[0]["value"] == "2026-10-31")
check("...as a set, never an append", changes[0]["mode"] == "set")

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "expected_close_date", "value": "2026-10-31",
     "mode": "append"},
]})
check("appending to a date is corrected to replacing",
      len(changes) == 1 and changes[0]["mode"] == "set")

changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "expected_close_date", "value": "soon"},
]})
check("a vague date is dropped", changes == [])
check("...and says a day is needed", "say the day" in rejected[0])

changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "expected_close_date", "value": "2028-01-01"},
]})
check("a date more than a year out is dropped as a likely misread year",
      changes == [])
check("...and says so", "misread year" in rejected[0])

changes, _, _, rejected = parse({"changes": [
    {"application": 3, "field": "expected_close_date", "value": "2026-01-01"},
]})
check("a date well in the past is dropped", changes == [])
check("...and says an expected close is about what is ahead",
      "still ahead" in rejected[0])

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "expected_close_date", "value": "2026-09-01"},
]})
check("a date a few days past is allowed — a slipped date is real news",
      len(changes) == 1)

changes, _, _, _ = logspec.parse(
    json.dumps({"changes": [{"application": 3,
                             "field": "expected_close_date",
                             "value": "2031-01-01"}]}),
    applications=APPS, stages=STAGES, categories=CATEGORIES, today=None)
check("with no today to measure against, only the format is enforced",
      len(changes) == 1)

changes, _, _, _ = parse({"changes": [
    {"application": 3, "field": "expected close date", "value": "2026-10-31"},
]})
check("the date field resolves when named with spaces", len(changes) == 1)


# --------------------------------------------------------------------------- #
# coerce_value: one validator, used by the parser and by the edit box
# --------------------------------------------------------------------------- #
def coerce(field, raw):
    return logspec.coerce_value(field, raw, stages=STAGES,
                                categories=CATEGORIES, today=TODAY)


check("a good text value passes", coerce("notes", "Something")[0] == "Something")
check("...trimmed", coerce("notes", "  Something  ")[0] == "Something")
for empty in ("", "   ", None, 7):
    value, reason = coerce("notes", empty)
    check("{!r} is refused as a value".format(empty),
          value is None and reason is not None)
check("clearing is named as a hand edit", "hand edit" in coerce("notes", "")[1])
check("a stage is normalised", coerce("stage", "discovery")[0] == "Discovery")
check("an invented stage is refused", coerce("stage", "Panel")[0] is None)
check("a category is validated",
      coerce("lost_category", "Nonsense")[0] is None)
check("a date is normalised",
      coerce("expected_close_date", "2026-10-31")[0] == "2026-10-31")
check("a loose date is refused",
      coerce("expected_close_date", "10/31/26")[0] is None)
check("an over-long value is refused",
      coerce("notes", "x" * 5000)[0] is None)
# This is the load-bearing one: the edit box on the review screen calls this,
# so anything it lets through is written to the record by a person who trusts
# the screen in front of them.
check("the parser and the edit box share this function",
      "coerce_value" in open(
          pathlib.Path(__file__).resolve().parents[1]
          / "app" / "routers" / "ui.py").read())


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
_, _, questions, _ = parse({"changes": [],
                            "questions": ["Who were the five people?"]})
check("a question survives", questions == ["Who were the five people?"])

_, _, questions, _ = parse({"changes": [],
                            "questions": ["a", "b", "c", "d", "e"]})
check("questions are capped", len(questions) == logspec.MAX_QUESTIONS)

_, _, questions, _ = parse({"changes": [], "questions": ["", "   ", "real"]})
check("blank questions are dropped", questions == ["real"])

_, _, questions, _ = parse({"changes": []})
check("no questions is the normal case", questions == [])

check("the prompt caps them in words too, not just in code",
      "At most three questions" in prompt)
check("the prompt forbids asking about a merely-empty field",
      "not a question raised by the note" in prompt)
check("...and says why, in terms of the record's own rule",
      "blank field is a legitimate state" in prompt)


# --------------------------------------------------------------------------- #
# The packet
# --------------------------------------------------------------------------- #
packet = logspec.build_packet(APPS, "Talked to Todd today.")
check("the packet fences the record", "<job_search_record>" in packet
      and "</job_search_record>" in packet)
check("the packet fences the note separately", "<spoken_note>" in packet)
check("the packet carries application ids", "Application 3" in packet)
check("the packet carries current values so a restatement is visible",
      "Wait for Todd" in packet)
check("the packet carries people, since notes name humans",
      "Todd Grant" in packet)
check("the note itself is in the packet", "Talked to Todd today." in packet)
answered = logspec.build_packet(APPS, "Talked to Todd today.",
                                "Q: Who?\nA: Todd Grant")
check("answers ride in the packet when there are any",
      "Todd Grant" in answered)
check("...fenced separately, so the note stays verbatim",
      "<answers_to_your_questions>" in answered)
check("no answers means no empty fence",
      "<answers_to_your_questions>" not in packet)
dated = logspec.build_packet(
    [{**APPS[0], "expected_close_date": "2026-10-31"}], "note")
check("the packet carries the current expected close date, so the model can "
      "tell a restatement from a move", "2026-10-31" in dated)

long_note = "y" * (logspec.MAX_NOTE_CHARS + 500)
packet = logspec.build_packet(APPS, long_note)
check("an over-long note is truncated rather than sent whole",
      "truncated for length" in packet
      and len(packet) < logspec.MAX_NOTE_CHARS + 3000)


# --------------------------------------------------------------------------- #
# Applying: how a value is merged
# --------------------------------------------------------------------------- #
check("set replaces outright",
      logspec.merged_value("old", "new", "set") == "new")
check("append onto an empty field is just the value",
      logspec.merged_value(None, "new", "append") == "new")
check("append onto an empty string is just the value",
      logspec.merged_value("   ", "new", "append") == "new")
merged = logspec.merged_value("older entry", "newest entry", "append")
check("append puts the newest entry first, so the log stays skimmable",
      merged.startswith("newest entry") and merged.endswith("older entry"))
check("...separated by a blank line", "\n\n" in merged)


# --------------------------------------------------------------------------- #
# The summary line
# --------------------------------------------------------------------------- #
check("an empty apply summarises honestly",
      logspec.summarise([]) == "nothing changed")
summary = logspec.summarise([
    {"company": "Condor", "application_id": 3, "label": "stage"},
    {"company": "Condor", "application_id": 3, "label": "next steps"},
    {"company": "Sierra", "application_id": 7, "label": "notes"},
])
check("the summary groups fields under their company",
      "Condor (stage, next steps)" in summary and "Sierra (notes)" in summary)


print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
