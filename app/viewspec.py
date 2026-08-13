"""What the chat is allowed to change about the charts.

The chat can reshape the Insights page, and this module is the entire boundary
on how far. It parses a proposed view out of the model's reply, validates it
against allow-lists, and turns it into a plain predicate over application
dicts. Everything it cannot vouch for is dropped and *reported* rather than
silently ignored.

The rule this exists to enforce
-------------------------------
**The model chooses which records to look at. It never produces a number.**

Every figure on the page is still computed by ``analytics.py`` from whatever
survives the filter, under the same min-3 suppression as before. So the worst a
wrong spec can do is show you the right arithmetic over the wrong cohort --
which is visible, because the filter is drawn on screen as chips and encoded in
the URL. A design where the model emitted figures directly would fail the other
way: a plausible number, silently wrong, with nothing on screen to check it
against. That failure is much harder to notice and much worse when it happens.

Two consequences worth keeping
------------------------------
**The spec round-trips through the query string.** A chat-driven view is a URL,
so it is shareable, bookmarkable, undoable with the back button, and reversible
without asking the model anything. Storing it in a session would have made the
chat the only way to undo the chat.

**Filtering makes small samples smaller.** This pipeline is already close to the
sample floor, and "referrals only" can take a cohort of six down to three. That
is not a bug and the page must not hide it: ``describe`` reports the cohort size
alongside every chip so a suddenly-suppressed tile reads as a consequence of the
filter rather than as a broken page.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The model is asked to emit its proposed view in a fenced block. A fence is
# used rather than free-form JSON so a reply that merely *discusses* filtering
# ("you could look at referrals only") cannot be mistaken for a request to do
# it. The block has to be deliberate.
VIEW_BLOCK = re.compile(r"```view\s*\n(.*?)\n?```", re.DOTALL)

# Every filterable field, and how to check a proposed value. Anything not
# listed here cannot be filtered on, however the model phrases it.
TEXT_FIELDS = {"source", "lost_category", "stage", "resume", "company"}
DATE_FIELDS = {"since", "until"}
COMPARE_FIELDS = {"source", "stage", "lost_category", "resume"}

MAX_VALUES = 8          # a filter listing more than this is not a filter


def _parse_date(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def extract_block(text: str) -> Tuple[str, Optional[str]]:
    """Split a reply into (prose, raw view block).

    The block is removed from the prose so it never renders in the transcript.
    A model emitting JSON at a person is a leaked implementation detail; what
    the filter did is shown by the chips instead.
    """
    match = VIEW_BLOCK.search(text or "")
    if not match:
        return (text or "").strip(), None
    prose = (text[: match.start()] + text[match.end():]).strip()
    return prose, match.group(1).strip()


def parse(raw: Optional[str], *, vocabulary: Optional[Dict[str, Sequence[str]]] = None
          ) -> Tuple[Dict[str, Any], List[str]]:
    """Validate a raw view block into (spec, rejected).

    `vocabulary` maps a field to the values that actually exist in this
    database -- the real stage names, the real sources, the resume labels. A
    value outside it is rejected rather than applied, because a filter on a
    source that does not exist silently returns an empty cohort, and an empty
    cohort renders as "not enough data" -- indistinguishable from a real
    answer. Rejections are surfaced to the reader instead.

    Returns an empty spec and a rejection list on anything malformed. Never
    raises: this parses output from a language model, so bad input is the
    expected case rather than an exceptional one.
    """
    spec: Dict[str, Any] = {}
    rejected: List[str] = []
    if not raw:
        return spec, rejected

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return spec, ["The proposed view wasn't valid JSON, so nothing was applied."]
    if not isinstance(payload, dict):
        return spec, ["The proposed view wasn't an object, so nothing was applied."]

    vocabulary = vocabulary or {}

    for key, value in payload.items():
        if key in TEXT_FIELDS:
            values = _as_list(value)
            if not values:
                continue
            if len(values) > MAX_VALUES:
                rejected.append(
                    "{}: {} values is more than a filter, so it was ignored.".format(
                        key, len(values)))
                continue
            known = vocabulary.get(key)
            if known is not None:
                lowered = {str(k).lower(): k for k in known}
                kept, unknown = [], []
                for v in values:
                    match = lowered.get(v.lower())
                    (kept if match else unknown).append(match or v)
                if unknown:
                    rejected.append(
                        "{}: no record has {} — that filter was dropped.".format(
                            key, ", ".join(repr(u) for u in unknown)))
                if kept:
                    spec[key] = kept
            else:
                spec[key] = values

        elif key in DATE_FIELDS:
            when = _parse_date(value)
            if when is None:
                rejected.append(
                    "{}: {!r} isn't a date I could read, so it was ignored.".format(
                        key, value))
            else:
                spec[key] = when

        elif key == "compare_by":
            field = str(value or "").strip().lower()
            if field in COMPARE_FIELDS:
                spec["compare_by"] = field
            elif field:
                rejected.append(
                    "compare_by: {!r} isn't something records can be grouped by.".format(
                        value))

        else:
            rejected.append("{!r} isn't a field on an application.".format(key))

    return spec, rejected


def _value_of(app: Dict[str, Any], field: str) -> Optional[str]:
    if field == "company":
        return app.get("company")
    if field == "resume":
        return app.get("resume_label")
    return app.get(field)


def apply(applications: List[Dict[str, Any]],
          spec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Narrow the application set. Never widens it, never reorders it.

    Text filters match case-insensitively and are OR within a field, AND
    across fields -- "referrals or inbound, closed since March" is the shape
    these questions actually take.

    A record with no value for a filtered field is excluded. That is the
    honest reading: "source is Referral" is a claim an application with no
    source recorded cannot satisfy, and including it would let blanks pad a
    cohort that is being counted.
    """
    if not spec:
        return list(applications)

    out = []
    for app in applications:
        keep = True
        for field in TEXT_FIELDS:
            wanted = spec.get(field)
            if not wanted:
                continue
            have = _value_of(app, field)
            if not have or str(have).lower() not in {str(w).lower() for w in wanted}:
                keep = False
                break
        if not keep:
            continue

        # Date bounds run against `applied_date`, the only date on an
        # application that describes an event rather than a row's lifecycle.
        # A record with no applied date drops out of any date-bounded view --
        # same reasoning as a blank text field.
        applied = _naive(app.get("applied_date"))
        if spec.get("since") or spec.get("until"):
            if applied is None:
                continue
            if spec.get("since") and applied < _naive(spec["since"]):
                continue
            if spec.get("until") and applied > _naive(spec["until"]):
                continue
        out.append(app)
    return out


def cohorts(applications: List[Dict[str, Any]],
            field: Optional[str]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Split into groups for a comparison, commonest first.

    Comparison renders as a table rather than as one funnel per group, and
    that is a deliberate choice about this dataset's size. Six applications
    split three ways is three funnels of two, each of which would draw a
    confident-looking bar chart over a sample too small to average. A table
    with the sample size in every cell says the same thing without the chart
    lending it authority it has not earned.
    """
    if not field:
        return []
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for app in applications:
        key = _value_of(app, field) or "Not recorded"
        groups.setdefault(str(key), []).append(app)
    rows = sorted(groups.items(), key=lambda kv: (kv[0] == "Not recorded",
                                                  -len(kv[1]), kv[0]))
    return rows


def describe(spec: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The chip row: one entry per active constraint, each removable.

    `param` is the query-string key so a chip can render its own remove link.
    The filter lives in the URL precisely so undoing it never requires asking
    the model to undo it.
    """
    if not spec:
        return []
    chips = []
    for field in sorted(TEXT_FIELDS):
        values = spec.get(field)
        if values:
            chips.append({
                "param": field,
                "label": "{} is {}".format(field.replace("_", " "),
                                           " or ".join(values)),
            })
    if spec.get("since"):
        chips.append({"param": "since",
                      "label": "applied on or after {}".format(
                          spec["since"].strftime("%Y-%m-%d"))})
    if spec.get("until"):
        chips.append({"param": "until",
                      "label": "applied on or before {}".format(
                          spec["until"].strftime("%Y-%m-%d"))})
    if spec.get("compare_by"):
        chips.append({"param": "compare_by",
                      "label": "compared by {}".format(
                          spec["compare_by"].replace("_", " "))})
    return chips


def to_query(spec: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Flatten a spec into query-string parameters."""
    if not spec:
        return {}
    out: Dict[str, str] = {}
    for field in TEXT_FIELDS:
        if spec.get(field):
            out[field] = "|".join(spec[field])
    for field in DATE_FIELDS:
        if spec.get(field):
            out[field] = spec[field].strftime("%Y-%m-%d")
    if spec.get("compare_by"):
        out["compare_by"] = spec["compare_by"]
    return out


def from_query(params: Dict[str, str], *,
               vocabulary: Optional[Dict[str, Sequence[str]]] = None
               ) -> Tuple[Dict[str, Any], List[str]]:
    """Rebuild a spec from query-string parameters.

    Goes through exactly the same validation as a model-proposed one. The URL
    is user-editable and linkable, so a hand-typed `?source=Nonsense` has to be
    rejected as visibly as a hallucinated one -- there is no privileged source
    of specs here.
    """
    payload: Dict[str, Any] = {}
    for field in TEXT_FIELDS:
        raw = (params.get(field) or "").strip()
        if raw:
            payload[field] = [p for p in raw.split("|") if p.strip()]
    for field in DATE_FIELDS:
        raw = (params.get(field) or "").strip()
        if raw:
            payload[field] = raw
    raw = (params.get("compare_by") or "").strip()
    if raw:
        payload["compare_by"] = raw
    if not payload:
        return {}, []
    return parse(json.dumps(payload, default=str), vocabulary=vocabulary)


# The instruction appended to the chat's system prompt. Kept here rather than
# in chat.py so the vocabulary of the spec and the description of it to the
# model cannot drift apart -- they are two halves of one contract.
PROMPT = """\

Changing what the charts show
----------------------------
The page above your answer shows charts computed from the record. You cannot \
write figures onto it and must never try: every number there is calculated from \
the data, and one you worked out yourself would silently disagree with it.

What you *can* do is change which applications those charts are computed from. \
When a question asks to narrow, filter, or compare — "how do referrals compare \
to outbound", "just the ones since June", "what about the roles I lost on comp" \
— end your reply with a fenced block like this:

```view
{"source": ["Referral"], "since": "2026-06-01"}
```

Fields: source, stage, lost_category, resume, company (each a list of exact \
values from the record), since and until (YYYY-MM-DD, matched against the \
applied date), and compare_by (one of source, stage, lost_category, resume) to \
break the figures out by group.

Rules:
- Only emit the block when the question actually asks to change the view. A \
question you can answer in a sentence does not need one.
- Use values exactly as they appear in the record. An invented value is \
rejected and the reader is told, which wastes their question.
- Say in your prose what you filtered to and why, in one short sentence. The \
reader sees chips describing the filter, but not your reasoning for it.
- Filtering shrinks the sample. If narrowing takes a group below three \
records, say so — the figures will correctly refuse to average, and that \
should read as expected rather than as the page breaking.
- Do not filter and compare on the same field. "Referrals only, compared by \
source" is a comparison with one row in it; pick one or the other."""
