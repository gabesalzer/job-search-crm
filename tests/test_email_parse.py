"""Tests for app.services.email_parse against real Gmail export text.

The fixture below is the actual text pypdf extracts from a real Gmail
"Print all" export (File > Print on an open thread) -- verified directly in
this sandbox against a sample PDF, not hand-written to fit the regex. If
Gmail ever changes its print layout, this is the test that will catch it.
No FastAPI/SQLAlchemy needed: email_parse.py is stdlib-only, so this can
import the real module directly, unlike ui.py's other tests.
"""
import sys
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.services.email_parse import parse_gmail_export

GMAIL_EXPORT_TEXT = """Gabe Salzer <gbsalzer@gmail.com>
Great catching up today! (resume attached)
3 messages
Gabe Salzer <gbsalzer@gmail.com> Tue, Jul 21, 2026 at 3:08 PM
To: deirdre.mullen@condorsoftware.com
Hi Deirdre,
It was great catching up today. Thanks for walking me through the role.
Best,
Gabe
Deirdre Mullen <deirdre.mullen@condorsoftware.com> Wed, Jul 22, 2026 at 8:35 PM
To: Gabe Salzer <gbsalzer@gmail.com>
Hi Gabe,
Thanks so much for taking the time yesterday!
Best,
Deirdre
Gabe Salzer <gbsalzer@gmail.com> Fri, Jul 24, 2026 at 8:21 PM
To: Deirdre Mullen <deirdre.mullen@condorsoftware.com>
Hi Deirdre,
Thank you again for the intro to Alexey.
Best,
Gabe
"""


def test_subject_extracted_from_real_gmail_export():
    result = parse_gmail_export(GMAIL_EXPORT_TEXT)
    assert result["subject"] == "Great catching up today! (resume attached)"


def test_started_and_last_message_at_span_the_whole_thread():
    result = parse_gmail_export(GMAIL_EXPORT_TEXT)
    assert result["started_at"] == datetime(2026, 7, 21, 15, 8)
    assert result["last_message_at"] == datetime(2026, 7, 24, 20, 21)


def test_participants_include_both_sides_deduped():
    result = parse_gmail_export(GMAIL_EXPORT_TEXT)
    emails = [e.strip() for e in result["participants"].split(",")]
    assert sorted(emails) == ["deirdre.mullen@condorsoftware.com", "gbsalzer@gmail.com"]


def test_empty_text_returns_all_none():
    result = parse_gmail_export("")
    assert result == {
        "subject": None, "participants": None, "started_at": None, "last_message_at": None,
    }


def test_non_gmail_text_returns_all_none_rather_than_guessing():
    """Plain pasted prose with no Gmail-shaped headers shouldn't produce a
    false-positive subject or dates -- silence is the safe failure mode,
    since callers only use these values to fill in blanks."""
    result = parse_gmail_export("Hey, just checking in on the role. Thanks!\n- Jane")
    assert result["subject"] is None
    assert result["started_at"] is None
    assert result["last_message_at"] is None


def test_single_message_thread_has_no_message_count_line():
    """A thread with only one message doesn't get an 'N messages' line at
    all in Gmail's export -- the subject line still needs to be found
    correctly without that anchor."""
    text = (
        "Gabe Salzer <gbsalzer@gmail.com>\n"
        "Intro to the team\n"
        "Gabe Salzer <gbsalzer@gmail.com> Mon, Jul 20, 2026 at 9:00 AM\n"
        "To: alex@company.com\n"
        "Hi Alex, great meeting you.\n"
    )
    result = parse_gmail_export(text)
    assert result["subject"] == "Intro to the team"
    assert result["started_at"] == datetime(2026, 7, 20, 9, 0)
    assert result["last_message_at"] == datetime(2026, 7, 20, 9, 0)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} email-parse assertions passed.")
