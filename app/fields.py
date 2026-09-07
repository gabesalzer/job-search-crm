"""What every field on an application means, and who is allowed to write it.

Stdlib-only, like the other reasoning modules. Plain data in, plain data out.

This is two things at once, deliberately.

**It is the app's data dictionary.** Every field on the pipeline's central
record, in one place, in one sentence each. Those sentences already existed --
scattered across `models.py` docstrings, help text on the edit page, and the
Log's prompt -- which meant three copies that could disagree and did. The
Settings page renders this; nothing else has to.

**It is the Log's prompt vocabulary.** `logspec.DEFINITIONS` is the writable
subset of this catalogue, so the sentence you read on the Settings page is
byte-for-byte the sentence the model is given. A page describing the prompt
rather than *being* it would be documentation, and documentation drifts.

The `writer` field is the part worth having built
-------------------------------------------------
Four values, and the distinctions are load-bearing rather than decorative:

* ``you`` -- only a human ever writes this. `champion` and `manual_forecast`
  live here on purpose, and the reason is in their definitions.
* ``log`` -- a dictated note may propose a change, which you approve. These
  are exactly the fields whose definitions reach the model.
* ``classifier`` -- written by the job-description classifier, under the
  separate regime described in `classify.py`: checkable against a document the
  app already holds, and never overwritten once you have typed a value.
* ``auto`` -- stamped by the app. Editable on the edit page, because a date
  recorded when you got round to data entry measures your evening rather than
  your job search, but nothing you set by hand here.

Showing the excluded fields alongside the included ones is the point of the
page. "Why can't a voice note set champion" is a question the catalogue
answers in the same breath as it lists the field, rather than in a commit
message nobody will read.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

# Who writes a field, in the order they should be explained to a reader.
WRITERS = {
    "you": "You, by hand",
    "log": "You, or a note you approve",
    "classifier": "You, or the posting classifier",
    "auto": "Stamped automatically",
}

# The catalogue. Order within a group is display order; groups are rendered in
# the order they first appear here.
#
# Every definition is one sentence, written to be read by a person and by a
# model at the same time -- which is a real constraint, not a slogan. "What the
# employer is trying to fix" tells you what belongs in `pain`; "the employer's
# problem, not yours" is what stops a model filing your comp worry there.
CATALOGUE: List[Dict[str, str]] = [
    # --- What this record is ------------------------------------------- #
    {"field": "company", "label": "Company", "group": "Identity",
     "writer": "you",
     "definition": "The employer you would actually work for, kept separate "
                   "from whichever agency or posting sourced the role."},
    {"field": "title", "label": "Role title", "group": "Identity",
     "writer": "you",
     "definition": "The role as advertised, copied onto the application so a "
                   "record with no linked posting still says what it is."},
    {"field": "job_posting", "label": "From posting", "group": "Identity",
     "writer": "you",
     "definition": "The advertisement you applied to, if there was one. "
                   "Optional: cold outreach has no posting."},
    {"field": "resume", "label": "Resume", "group": "Identity",
     "writer": "you",
     "definition": "Which version of your resume went in, so traction can be "
                   "compared between them later."},
    {"field": "source", "label": "Source", "group": "Identity",
     "writer": "you",
     "definition": "How this application came to exist — referral, inbound, "
                   "outbound. Blank means you have not recorded it, which the "
                   "forecast treats as no evidence rather than as a bad one."},

    # --- Where it stands ------------------------------------------------ #
    {"field": "stage", "label": "Stage", "group": "Pipeline",
     "writer": "log",
     "definition": "How far the pursuit has got in your own judgment — not "
                   "which interview round is next. Every employer's loop is "
                   "shaped differently; your evaluation of the opportunity is "
                   "the thing that generalises."},
    {"field": "next_steps", "label": "Next steps", "group": "Pipeline",
     "writer": "log",
     "definition": "What you have decided to do next on this pursuit, or what "
                   "you committed to. A concrete action, not a status."},
    {"field": "expected_close_date", "label": "Expected close",
     "group": "Pipeline", "writer": "log",
     "definition": "When you expect to KNOW either way — an offer, a "
                   "rejection, or a decision to walk. Not the date of the "
                   "next interview."},
    {"field": "manual_forecast", "label": "Manual forecast",
     "group": "Pipeline", "writer": "you",
     "definition": "Your own call on where this lands. Deliberately yours "
                   "alone: the automated forecast sits beside it and is never "
                   "allowed to overwrite it, because two independent reads "
                   "that can visibly disagree is the whole point."},

    # --- Sales qualification -------------------------------------------- #
    {"field": "pain", "label": "Pain", "group": "Qualification",
     "writer": "log",
     "definition": "What the EMPLOYER is trying to fix by hiring for this "
                   "role — their problem, not yours. Not what worries you "
                   "about the job."},
    {"field": "process", "label": "Process", "group": "Qualification",
     "writer": "log",
     "definition": "How they decide: who is involved, what the remaining "
                   "rounds are, what has to happen internally before an offer."},
    {"field": "risks", "label": "Risks", "group": "Qualification",
     "writer": "log",
     "definition": "What could kill this pursuit — including things that "
                   "worry you about the role, the comp, the team, or the "
                   "timing."},
    {"field": "champion", "label": "Champion inside",
     "group": "Qualification", "writer": "you",
     "definition": "Someone who will argue for you in a room you are not in — "
                   "not an interviewer who was friendly for an hour. Off "
                   "limits to automation on purpose: the bar is the field's "
                   "entire value, and a model reading enthusiasm as advocacy "
                   "would turn a forecast bonus into a reward for being "
                   "treated politely."},

    # --- What you know about it ----------------------------------------- #
    {"field": "context", "label": "Context", "group": "Judgment",
     "writer": "log",
     "definition": "Durable background on the opportunity you would re-read "
                   "when judging whether it is worth pursuing. Not a running "
                   "log."},
    {"field": "notes", "label": "Notes", "group": "Judgment",
     "writer": "log",
     "definition": "A running scratchpad of what happened lately. The "
                   "catch-all, and the wrong home for anything that fits a "
                   "field above."},
    {"field": "seniority", "label": "Seniority", "group": "Judgment",
     "writer": "classifier",
     "definition": "Director+ or Manager, read from the job description. "
                   "Blank is a real answer — an individual-contributor role "
                   "is neither, and rounding it into the nearer value would "
                   "quietly corrupt every later comparison."},
    {"field": "speciality", "label": "Speciality", "group": "Judgment",
     "writer": "classifier",
     "definition": "Systems, Strategy, or genuinely both, read from the job "
                   "description. Same rule about blank."},

    # --- How it ended ---------------------------------------------------- #
    {"field": "lost_category", "label": "Closed lost category",
     "group": "Outcome", "writer": "log",
     "definition": "The countable reason a pursuit was lost. Stays blank "
                   "until you actually know — a loss you have not diagnosed "
                   "is a real state, and forcing a value fills this with "
                   "whichever option is least wrong."},
    {"field": "lost_reason", "label": "Closed lost reason",
     "group": "Outcome", "writer": "log",
     "definition": "In your own words, what actually happened when this was "
                   "lost. The half a picklist cannot hold."},

    # --- Dates and derived ------------------------------------------------ #
    {"field": "applied_date", "label": "Applied", "group": "Dates",
     "writer": "you",
     "definition": "When the application actually went in. Blank for roles "
                   "that came to you."},
    {"field": "last_activity_date", "label": "Last activity", "group": "Dates",
     "writer": "auto",
     "definition": "Stamped on a stage change or an approved note; an edit "
                   "here wins. Drives the age shown on the board card."},
    {"field": "created_at", "label": "Created", "group": "Dates",
     "writer": "auto",
     "definition": "When this record was first added here — which is usually "
                   "later than when anything happened."},
    {"field": "updated_at", "label": "Updated", "group": "Dates",
     "writer": "auto",
     "definition": "Re-stamped on every save unless you change it by hand."},
    {"field": "brief", "label": "Brief", "group": "Dates",
     "writer": "auto",
     "definition": "Generated prose about this pursuit, written on request "
                   "and stored rather than recomputed because it costs a paid "
                   "call. Dated, so a stale one cannot pass for a fresh one."},
]

# Index for lookups. Built once; the catalogue is a constant.
BY_FIELD: Dict[str, Dict[str, str]] = {row["field"]: row for row in CATALOGUE}


def groups() -> List[str]:
    """Group names in the order they first appear in the catalogue."""
    seen: List[str] = []
    for row in CATALOGUE:
        if row["group"] not in seen:
            seen.append(row["group"])
    return seen


def default_definition(field: str) -> Optional[str]:
    row = BY_FIELD.get(field)
    return row["definition"] if row else None


def defaults_for(writable: Sequence[str]) -> Dict[str, str]:
    """The default definitions for a set of fields, skipping any not catalogued.

    This is what `logspec.DEFINITIONS` is built from, which is the mechanism
    that stops the Settings page and the prompt from drifting apart: there is
    one sentence per field and both read it from here.
    """
    out = {}
    for field in writable:
        text = default_definition(field)
        if text:
            out[field] = text
    return out


def rows(overrides: Optional[Dict[str, str]] = None,
         writable: Optional[Sequence[str]] = None) -> List[Dict[str, object]]:
    """The catalogue as display rows, with any overrides applied.

    `overrides` maps field to an edited definition. A row that has one carries
    `edited: True` and keeps the default alongside, so the page can offer a
    reset without another lookup and you can see what you changed away from.

    `writable` names the fields whose definitions actually reach the model.
    Passed in rather than imported so this module stays free of even its own
    siblings, and so the page cannot claim a field is read by the Log when
    `logspec.WRITABLE` says otherwise.
    """
    overrides = overrides or {}
    reaches = set(writable or [])
    out: List[Dict[str, object]] = []
    for row in CATALOGUE:
        field = row["field"]
        edited = field in overrides and overrides[field].strip() != ""
        out.append({
            "field": field,
            "label": row["label"],
            "group": row["group"],
            "writer": row["writer"],
            "writer_label": WRITERS.get(row["writer"], row["writer"]),
            "definition": overrides[field] if edited else row["definition"],
            "default": row["definition"],
            "edited": edited,
            # Whether editing this row changes anything the model is told.
            # Rendered plainly, because a page where half the edits are inert
            # and it does not say which half is a page that misleads you.
            "in_prompt": field in reaches,
        })
    return out
