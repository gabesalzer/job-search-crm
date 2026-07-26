"""Best-effort parser for Gmail's own thread export/print PDF (and the same
text pasted directly), used to pre-fill an Email Thread's subject,
participants, and start/last-message dates from an upload instead of making
you type them by hand -- and, from there, to auto-create/find the Person
each message's sender corresponds to.

Kept dependency-free (stdlib only, like resume_extract.py) so it can be
imported and unit-tested without the app's FastAPI/SQLAlchemy stack.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional, TypedDict

# Gmail's "Print all" export (the printer icon on an open thread, or File >
# Print) has a very regular shape: each message header is
# "<Sender Name> <email> <Weekday>, <Month> <Day>, <Year> at <H:MM> <AM/PM>",
# and the thread subject sits on its own line right before the first one,
# preceded only by the "your own account" banner line (that account's own
# "<Name> <email>" on its own line, with nothing else) and, for multi-message
# threads, an "N messages" line. Verified against a real Gmail export, not
# guessed at.
_SENDER_RE = re.compile(
    r"([A-Za-z][^\n<]{0,80}?)\s*<([\w.+-]+@[\w-]+\.[\w.-]+)>\s+"
    r"([A-Za-z]{3}, [A-Za-z]{3} \d{1,2}, \d{4}) at (\d{1,2}:\d{2}\s*[AP]M)"
)
_DATE_RE = re.compile(r"([A-Za-z]{3}, [A-Za-z]{3} \d{1,2}, \d{4}) at (\d{1,2}:\d{2}\s*[AP]M)")
_MSG_COUNT_RE = re.compile(r"^\d+\s+messages?$", re.I)
_BANNER_RE = re.compile(r"^([^\n<]+?)\s*<([\w.+-]+@[\w-]+\.[\w.-]+)>\s*$")
_SENDER_BANNER_RE = re.compile(r"<[^<>@]+@[^<>]+>\s*$")  # loose check used for subject-line trimming
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class Sender(TypedDict):
    name: str
    email: str


class GmailExportFields(TypedDict):
    subject: Optional[str]
    participants: Optional[str]
    started_at: Optional[datetime]
    last_message_at: Optional[datetime]
    self_email: Optional[str]
    other_senders: List[Sender]


def parse_gmail_export(text: str) -> GmailExportFields:
    """Extract subject / participants / dates / senders from Gmail-exported
    thread text. Every field is None/empty if the text doesn't look like a
    Gmail export (or is empty) -- this never raises, so callers can always
    use the result to fill in blanks without a try/except.

    ``self_email`` is read off the account-owner banner line Gmail always
    prints at the very top of an export ("Your Name <you@x.com>" on its own
    line) -- this is what lets callers tell "a message you sent" apart from
    "a message someone else sent" without hardcoding an address anywhere.

    ``other_senders`` is every unique (name, email) that actually *sent* a
    message in the thread (not just anyone mentioned or CC'd), excluding
    ``self_email``, in the order they first appear. This is what a caller
    uses to auto-create/find a Person for the thread.
    """
    result: GmailExportFields = {
        "subject": None, "participants": None, "started_at": None, "last_message_at": None,
        "self_email": None, "other_senders": [],
    }
    if not text:
        return result

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Account-owner banner: the first line, if it's a lone "Name <email>".
    self_email = None
    banner_match = _BANNER_RE.match(lines[0]) if lines else None
    if banner_match:
        self_email = banner_match.group(2).strip().lower()
        result["self_email"] = self_email

    # Subject: the last non-"N messages" line before the first message header.
    header_idx = next((i for i, ln in enumerate(lines) if _DATE_RE.search(ln)), None)
    if header_idx:
        candidates = [ln for ln in lines[:header_idx] if not _MSG_COUNT_RE.match(ln)]
        if candidates and _SENDER_BANNER_RE.search(candidates[0]):
            candidates = candidates[1:]  # drop the account banner line itself
        if candidates:
            result["subject"] = candidates[-1]

    # Every message header: gives us senders (name+email, in message order)
    # and dates in one pass, from the same regex, so they can't disagree.
    dates = []
    seen_emails = set()
    other_senders: List[Sender] = []
    for m in _SENDER_RE.finditer(text):
        name, email = m.group(1).strip(), m.group(2).strip().lower()
        try:
            dates.append(datetime.strptime(f"{m.group(3)} {m.group(4).replace(' ', '')}", "%a, %b %d, %Y %I:%M%p"))
        except ValueError:
            continue
        if email == self_email or email in seen_emails:
            continue
        seen_emails.add(email)
        other_senders.append({"name": name, "email": email})
    if dates:
        result["started_at"] = min(dates)
        result["last_message_at"] = max(dates)
    result["other_senders"] = other_senders

    emails = sorted(set(_EMAIL_RE.findall(text)))
    if emails:
        result["participants"] = ", ".join(emails)
    return result
