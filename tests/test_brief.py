"""The brief packet builder, tested against the real implementation.

Like app/forecast.py, app/brief.py imports nothing but the standard library, so
this file exercises the actual module rather than a mirror of it. That matters
more here than anywhere else in the project: this builder decides what text
gets sent to a third-party API, and a mirror that drifted would prove the
wrong function safe.

What's worth proving:

1. Chronology. The brief's whole job is "what happened, in order". If the
   packet arrives shuffled, the model will narrate it shuffled, confidently.
2. Truncation leaves a mark. A silently clipped transcript reads to the model
   as a conversation that ended early -- which is how you get a brief reporting
   that a call "closed without next steps" when the next steps were in the
   part that got dropped.
3. The recency budget. When there's more raw text than the ceiling allows, the
   newest conversations keep their verbatim and the oldest degrade. A naive
   chronological fill would do the exact opposite and spend the whole budget on
   the recruiter screen from March.
4. Empty sections vanish. A packet padded with "Meetings: none" invites the
   model to write about absences.
5. Nothing crashes on a blank record. A brand-new application has no company,
   no stage history, no activity, and half its columns NULL.
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import brief as b  # noqa: E402


JAN = datetime(2026, 1, 15, 9, 0)
FEB = datetime(2026, 2, 20, 14, 30)
MAR = datetime(2026, 3, 25, 11, 0)


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

def test_activity_is_chronological_oldest_first():
    out = b.build_brief_payload(
        company="Acme",
        meetings=[
            {"meeting_date": MAR, "title": "Panel"},
            {"meeting_date": JAN, "title": "Recruiter screen"},
        ],
        email_threads=[{"last_message_at": FEB, "subject": "Scheduling"}],
    )
    assert out.index("Recruiter screen") < out.index("Scheduling") < out.index("Panel")


def test_meetings_and_threads_interleave_by_date():
    """The two activity types share one timeline. Grouping meetings together
    and threads together would misrepresent an email that arrived mid-loop."""
    out = b.build_brief_payload(
        meetings=[{"meeting_date": JAN, "title": "First call"}],
        email_threads=[
            {"last_message_at": FEB, "subject": "Middle note"},
        ],
    )
    assert out.index("First call") < out.index("Middle note")


def test_thread_falls_back_to_started_at_when_no_last_message():
    out = b.build_brief_payload(
        meetings=[{"meeting_date": MAR, "title": "Late meeting"}],
        email_threads=[{"started_at": JAN, "subject": "Early thread"}],
    )
    assert out.index("Early thread") < out.index("Late meeting")


def test_stage_history_is_ordered():
    out = b.build_brief_payload(stage_history=[
        {"changed_at": MAR, "from_stage": "Discovery", "to_stage": "Takehome"},
        {"changed_at": JAN, "to_stage": "Qualification"},
    ])
    assert out.index("Qualification") < out.index("Takehome")


def test_aware_and_naive_datetimes_sort_together():
    """Form-entered dates come back naive, stamped ones aware. Comparing the
    two raises TypeError, and both kinds genuinely occur in these tables."""
    aware = datetime(2026, 3, 25, 11, 0, tzinfo=timezone.utc)
    out = b.build_brief_payload(
        meetings=[{"meeting_date": aware, "title": "Aware meeting"}],
        email_threads=[{"last_message_at": JAN, "subject": "Naive thread"}],
    )
    assert out.index("Naive thread") < out.index("Aware meeting")


# --------------------------------------------------------------------------- #
# Truncation and the recency budget
# --------------------------------------------------------------------------- #

def test_long_transcript_is_clipped_with_a_visible_marker():
    out = b.build_brief_payload(
        meetings=[{"meeting_date": JAN, "transcript": "x" * (b.MAX_TRANSCRIPT_CHARS + 5000)}]
    )
    assert b.TRUNCATION_MARKER in out
    assert len(out) < b.MAX_TRANSCRIPT_CHARS + 5000


def test_short_transcript_is_untouched():
    out = b.build_brief_payload(
        meetings=[{"meeting_date": JAN, "transcript": "a normal length transcript"}]
    )
    assert "a normal length transcript" in out
    assert b.TRUNCATION_MARKER not in out


def test_budget_keeps_the_newest_verbatim_and_drops_the_oldest():
    """The point of the whole budgeting exercise. Three transcripts that can't
    all fit: the March one must survive intact and the January one must be the
    one that gives way."""
    # Each transcript sits just under the per-item cap so it survives _clip
    # intact; together they overrun the total ceiling, forcing the choice.
    chunk = 50_000
    count = (b.MAX_TOTAL_CHARS // chunk) + 2
    meetings = [
        {
            "meeting_date": JAN + timedelta(days=i),
            "title": "Meeting %d" % i,
            "transcript": "MARK%d " % i + ("y" * chunk),
        }
        for i in range(count)
    ]
    out = b.build_brief_payload(meetings=meetings)

    # Metadata is never what gets sacrificed -- every meeting still appears.
    for i in range(count):
        assert "Meeting %d" % i in out
    assert "omitted" in out

    # The newest transcript kept its text; the oldest gave way.
    assert "MARK%d" % (count - 1) in out
    assert "MARK0" not in out
    # And the rendering order is still oldest-first regardless of budget order.
    assert out.index("Meeting 0") < out.index("Meeting %d" % (count - 1))


def test_total_stays_under_the_ceiling_with_many_transcripts():
    huge = [
        {"meeting_date": JAN + timedelta(days=i), "title": "M%d" % i, "transcript": "z" * 50_000}
        for i in range(20)
    ]
    out = b.build_brief_payload(meetings=huge)
    assert len(out) <= b.MAX_TOTAL_CHARS + 20_000, len(out)


def test_email_body_is_clipped_too():
    out = b.build_brief_payload(
        email_threads=[{"last_message_at": JAN, "body": "q" * (b.MAX_BODY_CHARS + 2000)}]
    )
    assert b.TRUNCATION_MARKER in out


# --------------------------------------------------------------------------- #
# What gets included, and what doesn't
# --------------------------------------------------------------------------- #

def test_empty_sections_are_omitted_entirely():
    out = b.build_brief_payload(company="Acme", title="RevOps Lead")
    assert "ACTIVITY" not in out
    assert "PEOPLE" not in out
    assert "STAGE HISTORY" not in out


def test_blank_application_does_not_crash():
    out = b.build_brief_payload()
    assert isinstance(out, str)


def test_scores_and_reasons_reach_the_packet():
    """The hand-entered judgment is the most information-dense thing in the
    record. A brief written without it is a transcript summary."""
    out = b.build_brief_payload(meetings=[{
        "meeting_date": JAN,
        "score": 72,
        "score_reason": "HM named a specific problem",
        "my_performance": 70,
        "employer_engagement": 85,
    }])
    assert "72" in out
    assert "HM named a specific problem" in out
    assert "85" in out


def test_zero_score_is_not_dropped_as_falsy():
    """Blank and 0 are opposite claims here, same invariant the forecast holds."""
    out = b.build_brief_payload(meetings=[{"meeting_date": JAN, "score": 0}])
    assert "My score: 0" in out


def test_origin_fields_are_present():
    out = b.build_brief_payload(
        company="Acme",
        source="Referral",
        context="Introduced by a former colleague",
        applied_date=JAN,
        resume_label="v3 metrics-forward",
        posting={"title": "RevOps Lead", "url": "https://example.com/job", "jd_text": "Own the funnel."},
    )
    assert "Referral" in out
    assert "Introduced by a former colleague" in out
    assert "https://example.com/job" in out
    assert "v3 metrics-forward" in out
    assert "Own the funnel." in out


def test_people_are_listed_with_roles():
    out = b.build_brief_payload(people=[
        {"name": "Deirdre Mullen", "role": "Recruiter", "email": "d@example.com"},
    ])
    assert "Deirdre Mullen" in out
    assert "Recruiter" in out


# --------------------------------------------------------------------------- #
# The prompt itself
# --------------------------------------------------------------------------- #

def test_packet_is_fenced_for_injection_safety():
    """Transcripts contain other people's words, and none of them knew this
    would reach a model. The fence plus the system prompt's instruction are
    what make an imperative sentence inside an email stay a sentence."""
    msgs = b.build_messages("SOME PACKET")
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    assert "<application_packet>" in msgs[0]["content"]
    assert "</application_packet>" in msgs[0]["content"]
    assert "SOME PACKET" in msgs[0]["content"]


def test_system_prompt_forbids_prediction_and_names_both_sections():
    """The brief is the record, not the read. Scoring and forecasting live in
    parts of the app that don't cost money and don't hallucinate."""
    assert "## How this started" in b.SYSTEM_PROMPT
    assert "## What's happened so far" in b.SYSTEM_PROMPT
    assert "not instructions" in b.SYSTEM_PROMPT
    for banned in ("predict the outcome", "recommend next steps"):
        assert banned in b.SYSTEM_PROMPT


def test_brief_module_imports_no_app_code():
    """Same guarantee forecast.py carries: if this file can import it with only
    the stdlib present, these tests are exercising the shipped code."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "app", "brief.py")).read()
    assert not re.search(r"^\s*from \.", src, re.M)
    assert not re.search(r"^\s*import (httpx|sqlalchemy|fastapi)", src, re.M)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} brief assertions passed.")
