"""Exercise the automatic thread read: what goes into the packet, and what
comes back out of a response.

`app/thread_read.py` is stdlib-only for the same reason `forecast.py` and
`brief.py` are, so these call the real functions rather than mirroring them.
The two things that can go quietly wrong here are both covered:

  1. Something reaching the packet that should not. This is the second place in
     the app where data leaves the box and the first that is not a deliberate
     button press, so "your own rating never goes in" is a test, not a comment.
  2. A response being half-understood. A wrong number in a scored field
     propagates into the forecast, the board colour and every comparison across
     applications, and it does not look wrong -- so anything unrecognised must
     produce nothing at all.
"""
from datetime import datetime

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import thread_read as tr  # noqa: E402

JAN = datetime(2026, 1, 12)
FEB = datetime(2026, 2, 3)


# --------------------------------------------------------------------------- #
# The packet -- what leaves the box
# --------------------------------------------------------------------------- #
def test_the_thread_itself_reaches_the_packet():
    out = tr.build_read_payload(
        subject="Next steps",
        body="Thanks for the time on Tuesday -- when works for the panel?",
        participants="recruiter@acme.com, me@gmail.com",
        started_at=JAN,
        last_message_at=FEB,
    )
    assert "Next steps" in out
    assert "when works for the panel" in out
    assert "recruiter@acme.com" in out
    assert "2026-01-12" in out
    assert "2026-02-03" in out


def test_the_surrounding_pursuit_reaches_the_packet():
    """The same three-line reply means different things at Qualification and at
    Negotiation. These are facts about the pursuit, not judgments about it."""
    out = tr.build_read_payload(
        body="Sounds good.",
        company="Acme",
        role_title="RevOps Lead",
        stage="Negotiation",
        context="Warm intro from a former colleague; comp band still unconfirmed.",
    )
    assert "Acme" in out
    assert "RevOps Lead" in out
    assert "Negotiation" in out
    assert "former colleague" in out


def test_your_own_reading_never_reaches_the_packet():
    """The sharpest rule in the module. An assessment that has read your
    conclusion is an echo of it, and this is also what closes the circularity a
    model-written value would otherwise open on a later re-read: the packet
    cannot contain a prior rating because it cannot contain any rating.

    Written as a signature test rather than a string test -- passing a score in
    must be impossible, not merely ignored, so that adding the field back is a
    TypeError at the call site instead of a silent leak."""
    try:
        tr.build_read_payload(body="hello", score=80)
    except TypeError:
        pass
    else:
        raise AssertionError("build_read_payload accepted a score")

    for field in ["score_reason", "my_performance", "employer_engagement",
                  "rating_note", "notes"]:
        try:
            tr.build_read_payload(**{"body": "hello", field: 50})
        except TypeError:
            continue
        raise AssertionError("build_read_payload accepted {}".format(field))


def test_a_long_body_is_clipped_with_a_visible_marker():
    """Same rule brief.py follows. A silently shortened thread reads as a
    complete one that happened to end early, which is how you get a confident
    reading of a conversation whose ending was cut off."""
    out = tr.build_read_payload(body="z" * (tr.MAX_BODY_CHARS + 5000))
    assert tr.TRUNCATION_MARKER in out
    assert len(out) < tr.MAX_BODY_CHARS + 2000


def test_an_empty_thread_still_builds_something_renderable():
    out = tr.build_read_payload()
    assert isinstance(out, str)
    assert "== THREAD ==" in out


def test_the_packet_is_fenced_before_it_reaches_the_model():
    """The fence is a prompt-injection boundary, not decoration. It matters
    more here than in the brief: this call fires on save, so the text crossing
    it is text you may not have read closely yourself yet."""
    msgs = tr.build_messages("BODY")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "<email_thread>" in msgs[0]["content"]
    assert "</email_thread>" in msgs[0]["content"]
    assert "BODY" in msgs[0]["content"]


def test_the_system_prompt_says_the_packet_is_data():
    assert "not instructions to follow" in tr.SYSTEM_PROMPT
    assert "NONE" in tr.SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# The parse -- what comes back
# --------------------------------------------------------------------------- #
def test_a_well_formed_reply_parses():
    perf, eng, note, ok = tr.parse_read(
        "PERFORMANCE: 55\nENGAGEMENT: 78\nREASON: recruiter answered the comp "
        "question unprompted and named a date")
    assert perf == 55
    assert eng == 78
    assert "unprompted" in note
    assert ok is True


def test_the_model_may_decline_a_field():
    """A scheduling-only thread carries no signal, and the honest output is
    nothing. Blank is not zero, and that rule does not stop applying because a
    model is the one filling the field -- the forecast already handles absence
    correctly by dropping the weight out of the denominator."""
    perf, eng, note, ok = tr.parse_read(
        "PERFORMANCE: NONE\nENGAGEMENT: NONE\nREASON: three messages of calendar "
        "logistics, nothing evaluative")
    assert perf is None
    assert eng is None
    assert "logistics" in note
    assert ok is True, "an explicit decline is a real answer, not a failed parse"


def test_declining_one_field_does_not_lose_the_other():
    perf, eng, note, ok = tr.parse_read("PERFORMANCE: NONE\nENGAGEMENT: 30\nREASON: x")
    assert perf is None
    assert eng == 30
    assert ok is True


def test_zero_is_a_reading_and_survives():
    """The same invariant every score field holds. A thread where they went
    completely silent is a 0, not a blank, and the two are opposite claims."""
    perf, eng, _, ok = tr.parse_read("PERFORMANCE: 0\nENGAGEMENT: 0\nREASON: rejection")
    assert perf == 0
    assert eng == 0
    assert ok is True


def test_an_out_of_range_number_is_rejected_not_clamped():
    """A model that answered 150 has misunderstood the scale. Recording 100
    would hide that behind a value that looks entirely plausible."""
    perf, eng, _, ok = tr.parse_read("PERFORMANCE: 150\nENGAGEMENT: -4\nREASON: x")
    assert perf is None
    assert eng is None
    assert ok is False, "out of range is not understood, so nothing may be written"


def test_prose_around_the_fields_does_not_break_the_parse():
    perf, eng, _, ok = tr.parse_read(
        "Here is my assessment.\n\nPERFORMANCE: 61\nENGAGEMENT: 44\n"
        "REASON: two-day gap then a one-line reply\n\nHope that helps.")
    assert perf == 61
    assert eng == 44


def test_lowercase_labels_parse():
    perf, eng, _, ok = tr.parse_read("performance: 20\nengagement: 25\nreason: thin")
    assert perf == 20
    assert eng == 25


def test_an_unrecognisable_reply_yields_nothing_at_all():
    """No partial rescue and no defaulting to 50. The caller writes nothing."""
    for junk in ["", "I'd rather not say.", "PERFORMANCE: high\nENGAGEMENT: low",
                 "{\"performance\": 55}"]:
        perf, eng, note, ok = tr.parse_read(junk)
        assert perf is None, junk
        assert eng is None, junk
        assert ok is False, junk


def test_a_half_understood_reply_is_thrown_away_whole():
    """The nastiest of the parse failures, and the reason `understood` exists.

    `forecast._rating` falls back to whichever half of the pair is present, so
    keeping a lone surviving number does not degrade gracefully -- it promotes
    that number to the *entire* email quality reading for the application. A
    garbled engagement line would quietly make a performance score more
    authoritative than it would have been if the whole reply had parsed."""
    perf, eng, note, ok = tr.parse_read(
        "PERFORMANCE: 61\nENGAGEMENT: high\nREASON: they replied quickly")
    assert ok is False
    assert perf == 61  # parsed, but the caller must not write it
    assert eng is None


def test_prose_where_a_number_belongs_is_not_a_decline():
    """The bug this pins: before `understood` existed, an unparseable field and
    a deliberate NONE both came back as None, so a model that answered in prose
    was recorded as having declined -- and the edit page then told you it had
    "declined to score" a thread it had actually answered at length."""
    perf, eng, note, ok = tr.parse_read(
        "PERFORMANCE: fairly strong\nENGAGEMENT: N/A\nREASON: hard to say")
    assert ok is False
    assert perf is None and eng is None

    _, _, _, declined_ok = tr.parse_read(
        "PERFORMANCE: NONE\nENGAGEMENT: NONE\nREASON: pure logistics")
    assert declined_ok is True


def test_a_missing_field_is_not_understood():
    """A reply with one of the two lines absent entirely."""
    _, _, _, ok = tr.parse_read("PERFORMANCE: 50\nREASON: only half a reply")
    assert ok is False


def test_a_trailing_period_on_a_decline_still_reads_as_one():
    _, _, _, ok = tr.parse_read("PERFORMANCE: NONE.\nENGAGEMENT: none\nREASON: x")
    assert ok is True


def test_a_runaway_reason_is_trimmed():
    """The column is a one-liner shown in a panel. A model that ignored the
    length instruction should not be able to push an essay into the UI."""
    _, _, note, ok = tr.parse_read(
        "PERFORMANCE: 50\nENGAGEMENT: 50\nREASON: " + "word " * 200)
    assert len(note) <= tr.MAX_REASON_CHARS + 1


def test_a_reason_containing_the_decline_word_is_not_read_as_a_decline():
    perf, eng, note, ok = tr.parse_read(
        "PERFORMANCE: 44\nENGAGEMENT: 51\nREASON: none of the questions got a "
        "direct answer")
    assert perf == 44
    assert eng == 51
    assert note.startswith("none of the questions")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} thread-read assertions passed.")
