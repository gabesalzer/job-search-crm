"""Unit tests for app/services/granola.py, using response shapes copied
straight from Granola's own API reference (docs.granola.ai/api-reference),
not guessed. Run with: python3 tests/test_granola_parsing.py

Two real bugs were caught by actually checking the docs instead of assuming:
  1. The base URL is https://public-api.granola.ai/v1, not api.granola.ai
     -- the wrong host 404s outright, which is what surfaced this.
  2. Transcript segments use {"source": "microphone" | "speaker"}, not
     {"source": "microphone" | "system"} -- so "Them" was never matching.

httpx.get is monkeypatched so this needs no network access and no real key.
"""
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("GRANOLA_API_KEY", "grn_test_key")

import httpx  # noqa: E402

from app.services import granola  # noqa: E402


# Exact shape from https://docs.granola.ai/api-reference/list-notes
LIST_NOTES_RESPONSE = {
    "notes": [
        {
            "id": "not_1d3tmYTlCICgjy",
            "object": "note",
            "title": "Interview with Plaid for GTM Strategy & Operations",
            "owner": {"name": "Oat Benson", "email": "oat@granola.ai"},
            "created_at": "2026-07-10T15:30:00Z",
            "updated_at": "2026-07-10T16:45:00Z",
        }
    ],
    "hasMore": False,
    "cursor": "eyJjcmVkZW50aWFsfQ==",
}

# Exact shape from https://docs.granola.ai/api-reference/get-note
GET_NOTE_RESPONSE = {
    "id": "not_1d3tmYTlCICgjy",
    "object": "note",
    "title": "Interview with Plaid for GTM Strategy & Operations",
    "owner": {"name": "Oat Benson", "email": "oat@granola.ai"},
    "created_at": "2026-07-10T15:30:00Z",
    "updated_at": "2026-07-10T16:45:00Z",
    "web_url": "https://notes.granola.ai/t/f4269107-4e5b-4adb-be25",
    "summary_text": "Discussed the GTM Strategy & Operations role and next steps.",
    "summary_markdown": "## Summary\n\nDiscussed the role and next steps.",
    "transcript": [
        {
            "speaker": {"source": "microphone"},
            "text": "Thanks for taking the time to chat today.",
            "start_time": "2026-07-10T15:30:00Z",
            "end_time": "2026-07-10T15:30:05Z",
        },
        {
            "speaker": {"source": "speaker"},
            "text": "Of course -- excited to talk through the GTM role.",
            "start_time": "2026-07-10T15:30:06Z",
            "end_time": "2026-07-10T15:30:10Z",
        },
    ],
}


class FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class GranolaParsingTests(unittest.TestCase):
    def test_api_base_is_public_api_host(self):
        self.assertEqual(granola.API_BASE, "https://public-api.granola.ai/v1")

    @patch("httpx.get")
    def test_list_notes_uses_page_size_not_limit(self, mock_get):
        mock_get.return_value = FakeResponse(LIST_NOTES_RESPONSE)
        notes = granola.list_notes(limit=25)

        called_url, called_kwargs = mock_get.call_args[0][0], mock_get.call_args[1]
        self.assertEqual(called_url, "https://public-api.granola.ai/v1/notes")
        self.assertEqual(called_kwargs["params"], {"page_size": 25})

        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["id"], "not_1d3tmYTlCICgjy")
        self.assertEqual(notes[0]["title"], "Interview with Plaid for GTM Strategy & Operations")

    @patch("httpx.get")
    def test_list_notes_clamps_page_size_to_30(self, mock_get):
        mock_get.return_value = FakeResponse(LIST_NOTES_RESPONSE)
        granola.list_notes(limit=999)
        self.assertEqual(mock_get.call_args[1]["params"], {"page_size": 30})

    @patch("httpx.get")
    def test_get_note_requests_transcript(self, mock_get):
        mock_get.return_value = FakeResponse(GET_NOTE_RESPONSE)
        note = granola.get_note("not_1d3tmYTlCICgjy")

        called_url, called_kwargs = mock_get.call_args[0][0], mock_get.call_args[1]
        self.assertEqual(called_url, "https://public-api.granola.ai/v1/notes/not_1d3tmYTlCICgjy")
        self.assertEqual(called_kwargs["params"], {"include": "transcript"})

        self.assertEqual(note["title"], "Interview with Plaid for GTM Strategy & Operations")
        self.assertEqual(note["link"], "https://notes.granola.ai/t/f4269107-4e5b-4adb-be25")

    @patch("httpx.get")
    def test_get_note_prefers_summary_text(self, mock_get):
        mock_get.return_value = FakeResponse(GET_NOTE_RESPONSE)
        note = granola.get_note("not_1d3tmYTlCICgjy")
        self.assertEqual(
            note["summary"], "Discussed the GTM Strategy & Operations role and next steps."
        )

    @patch("httpx.get")
    def test_get_note_transcript_flattens_with_me_them_labels(self, mock_get):
        mock_get.return_value = FakeResponse(GET_NOTE_RESPONSE)
        note = granola.get_note("not_1d3tmYTlCICgjy")
        self.assertEqual(
            note["transcript"],
            "Me: Thanks for taking the time to chat today.\n"
            "Them: Of course -- excited to talk through the GTM role.",
        )

    def test_speaker_label_prefers_diarization_label(self):
        self.assertEqual(
            granola._speaker_label({"source": "speaker", "diarization_label": "Jane (Plaid)"}),
            "Jane (Plaid)",
        )

    def test_speaker_label_maps_source_when_no_diarization(self):
        self.assertEqual(granola._speaker_label({"source": "microphone"}), "Me")
        self.assertEqual(granola._speaker_label({"source": "speaker"}), "Them")
        # Old (wrong) assumption -- must NOT still be relied on:
        self.assertNotEqual(granola._speaker_label({"source": "system"}), "Them")


if __name__ == "__main__":
    unittest.main(verbosity=2)
