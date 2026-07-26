"""Best-effort parser for Gmail's own thread export/print PDF (and the same
text pasted directly), used to pre-fill an Email Thread's subject,
participants, and start/last-message dates from an upload instead of making
you type them by hand.

Kept dependency-free (stdlib only, like resume_extract.py) so it can be
imported and unit-tested without the app's FastAPI/SQLAlchemy stack.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, TypedDict

# Gmail's "Print all" export (the printer icon on an open thread, or File >
# Print) has a very regular shape: each message header is
# "<Sender Name> <email> <Weekday>, <Month> <Day>, <Year> at <H:MM> <AM/PM>",
# and the thread subject sits on its own line right before the first one,
# preceded only by the "your own account" banner line and (for multi-message
# threads) an "N messages" line. Verified against a real Gmail export, not
# guessed at.
_DATE_RE = re.compile(r"([A-Za-z]{3}, [A-Za-z]{3} \d{1,2}, \d{4}) at (\d{1,2}:\d{2}\s*[AP]M)")
_MSG_COUNT_RE = re.compile(r"^\d+\s+messages?$", re.I)
_SENDER_BANNER_RE = re.compile(r"<[^<>@]+@[^<>]+>\s*$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class GmailExportFields(TypedDict):
    subject: Optional[str]
    participants: Optional[str]
    started_at: Optional[datetime]
    last_message_at: Optional[datetime]


def parse_gmail_export(text: str) -> GmailExportFields:
    """Extract subject / participants / started_at / last_message_at from
    Gmail-exported thread text. Every field is None if the text doesn't look
    like a Gmail export (or is empty) -- this never raises, so callers can
    always use the result to fill in blanks without a try/except.
    """
    result: GmailExportFields = {
        "subject": None, "participants": None, "started_at": None, "last_message_at": None,
    }
    if not text:
        return result

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines) if _DATE_RE.search(ln)), None)
    if header_idx:
        candidates = [ln for ln in lines[:header_idx] if not _MSG_COUNT_RE.match(ln)]
        # Drop the leading "Your Name <you@x.com>" account banner line, if present.
        if candidates and _SENDER_BANNER_RE.search(candidates[0]):
            candidates = candidates[1:]
        if candidates:
            result["subject"] = candidates[-1]

    dates = []
    for m in _DATE_RE.finditer(text):
        try:
            dates.append(
                datetime.strptime(f"{m.group(1)} {m.group(2).replace(' ', '')}", "%a, %b %d, %Y %I:%M%p")
            )
        except ValueError:
            continue
    if dates:
        result["started_at"] = min(dates)
        result["last_message_at"] = max(dates)

    emails = sorted(set(_EMAIL_RE.findall(text)))
    if emails:
        result["participants"] = ", ".join(emails)
    return result
