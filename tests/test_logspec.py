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


def parse(payload):
    return logspec.parse(json.dumps(payload), applications=APPS,
                         stages=STAGES, categories=CATEGORIES)


# --------------------------------------------------------------------------- #
# The prompt is generated from the vocabularies, so it cannot drift
# --------------------------------------------------------------------------- #
prompt = logspec.system_prompt(stages=STAGES, categories=CATEGORIES)
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

changes, unmatched, rejected = logspec.parse(
    None, applications=APPS, stages=STAGES, categories=CATEGORIES)
check("no block at all proposes nothing", changes == [])
check("...and says so rather than failing silently", len(rejected) == 1)

changes, _, rejected = logspec.parse(
    "not json at all", applications=APPS, stages=STAGES, categories=CATEGORIES)
check("malformed JSON proposes nothing", changes == [])
check("...and is reported", "valid JSON" in rejected[0])


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
changes, unmatched, rejected = parse({
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
changes, _, rejected = parse({"changes": [
    {"application": 99, "field": "notes", "value": "Something."},
]})
check("a change to an unknown application is dropped", changes == [])
check("...and names the id it could not find", "99" in rejected[0])

changes, _, rejected = parse({"changes": [
    {"field": "notes", "value": "Something."},
]})
check("a change with no application id is dropped", changes == [])

changes, _, rejected = parse({"changes": [
    {"application": "Condor", "field": "notes", "value": "Something."},
]})
check("a change naming a company instead of an id is dropped", changes == [])


# --------------------------------------------------------------------------- #
# The field allow-list
# --------------------------------------------------------------------------- #
changes, _, rejected = parse({"changes": [
    {"application": 3, "field": "champion", "value": "true"},
]})
check("a note cannot set champion however it is phrased", changes == [])
check("...and is told which field it tried", "champion" in rejected[0])

changes, _, _ = parse({"changes": [
    {"application": 3, "field": "next steps", "value": "Call Todd"},
]})
check("a field named with a space still resolves",
      len(changes) == 1 and changes[0]["field"] == "next_steps")

changes, _, _ = parse({"changes": [
    {"application": 3, "field": "notes about pain", "value": "x"},
]})
check("field matching is exact, not fuzzy", changes == [])


# --------------------------------------------------------------------------- #
# Picklists are validated against the real vocabulary
# --------------------------------------------------------------------------- #
changes, _, rejected = parse({"changes": [
    {"application": 3, "field": "stage", "value": "Panel"},
]})
check("an invented stage is dropped", changes == [])
check("...and is quoted back", "Panel" in rejected[0])

changes, _, _ = parse({"changes": [
    {"application": 3, "field": "stage", "value": "discovery"},
]})
check("a stage matches case-insensitively and is normalised",
      len(changes) == 1 and changes[0]["value"] == "Discovery")

changes, _, _ = parse({"changes": [
    {"application": 3, "field": "lost_category", "value": "Nonsense"},
]})
check("an invented lost category is dropped", changes == [])

changes, _, _ = parse({"changes": [
    {"application": 3, "field": "stage", "value": "Discovery",
     "mode": "append"},
]})
check("appending to a picklist is silently corrected to replacing",
      len(changes) == 1 and changes[0]["mode"] == "set")


# --------------------------------------------------------------------------- #
# Blank is not zero: a note may never clear a field
# --------------------------------------------------------------------------- #
for empty in ("", "   ", None, 0):
    changes, _, rejected = parse({"changes": [
        {"application": 3, "field": "next_steps", "value": empty},
    ]})
    check("an empty value ({!r}) never clears a field".format(empty),
          changes == [])
check("...and the rejection says clearing is a hand edit",
      "hand edit" in rejected[0])


# --------------------------------------------------------------------------- #
# Restating what a field already says is not a change
# --------------------------------------------------------------------------- #
changes, _, rejected = parse({"changes": [
    {"application": 3, "field": "next_steps", "value": "Wait for Todd"},
]})
check("a value identical to the current one is not proposed", changes == [])
check("...and says so rather than vanishing", "already says" in rejected[0])


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #
changes, _, rejected = parse({"changes": [
    {"application": 3, "field": "notes", "value": "x" * 5000},
]})
check("an absurdly long value is dropped", changes == [])

many = [{"application": 3, "field": "notes", "value": "n{}".format(i)}
        for i in range(logspec.MAX_CHANGES + 1)]
changes, _, rejected = parse({"changes": many})
check("a note proposing more changes than the cap applies none",
      changes == [])
check("...and suggests dictating it in parts", "in parts" in rejected[0])

changes, _, rejected = parse({"changes": [
    {"application": 3, "field": "notes", "value": "first"},
    {"application": 3, "field": "notes", "value": "second"},
]})
check("the same field proposed twice keeps only the first",
      len(changes) == 1 and changes[0]["value"] == "first")

changes, _, rejected = parse({"changes": "not a list"})
check("a changes value that is not a list proposes nothing", changes == [])

changes, _, _ = parse({"changes": [None, {"application": 3, "field": "notes",
                                          "value": "ok"}]})
check("a junk entry is dropped without losing the good one alongside it",
      len(changes) == 1)


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
