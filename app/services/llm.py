"""Anthropic Messages API client, used only to write application briefs.

Deliberately thin, and deliberately not the `anthropic` SDK. The app already
depends on httpx for the Granola and Firecrawl clients, and this endpoint is one
POST with three headers -- pulling in an SDK to make that call would add a
dependency, a version to track, and a transitive tree, in exchange for saving
about fifteen lines. ``requirements.txt`` is unchanged by this feature.

Activates only when ANTHROPIC_API_KEY is set, the same contract as
``granola.py`` and ``scrape.py``: no key means the feature quietly turns itself
off and the rest of the app is unaffected. That matters more here than for the
other two, because this repo is public -- anyone who clones it gets an app that
runs fine and simply has no Brief button.

Nothing in this module knows what a job application is. It takes a system
prompt and messages and returns text.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

import httpx

API_URL = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")

# Pinned via env rather than hardcoded, because model IDs get retired on a
# schedule this app has no reason to track. When this one is deprecated, the
# fix is a dashboard edit and a restart, not a commit and a redeploy.
DEFAULT_MODEL = os.getenv("BRIEF_MODEL", "claude-sonnet-5")

# The API version header is a dated contract, not the model version. It is
# pinned on purpose: it is what guarantees the response shape parsed below
# stays the shape that arrives.
API_VERSION = "2023-06-01"

# A brief is two short sections. This ceiling exists to bound a runaway
# response, not to shape the output -- the length instruction lives in the
# prompt, where the model can actually act on it.
MAX_TOKENS = 1500

# Generous: a long packet with several transcripts takes real time to read.
TIMEOUT_SECONDS = 180


class LLMError(RuntimeError):
    """A failed generation, carrying a message fit to show in the UI.

    The API's own error text is preserved where there is one. A brief that
    fails with "an error occurred" gives you nothing to act on, while
    "model: not_found_error" tells you exactly which dashboard field is wrong.
    """


def _key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def model_name() -> str:
    return DEFAULT_MODEL


def generate(
    system: str,
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = MAX_TOKENS,
    timeout: int = TIMEOUT_SECONDS,
) -> Tuple[str, str]:
    """POST to the Messages API and return (text, model_used).

    Raises LLMError with something readable on any failure.

    Both budgets are overridable because the two callers wait very differently.
    The Brief is a button you pressed and are watching, on a packet that can
    carry several full transcripts, so three minutes is a reasonable ceiling.
    The thread read fires when you *save* a thread, on one much smaller packet
    that returns three lines — and a save that appears to hang for three
    minutes reads as a crashed app, which is a worse failure than not getting
    the reading. The defaults are the Brief's, so that caller is unchanged.
    """
    if not enabled():
        raise LLMError(
            "ANTHROPIC_API_KEY is not set. Add it in the Render dashboard "
            "(or your local .env) and restart."
        )

    try:
        resp = httpx.post(
            API_URL,
            headers={
                "x-api-key": _key(),
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": DEFAULT_MODEL,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
            },
            timeout=timeout,
        )
    except httpx.TimeoutException:
        raise LLMError(
            "The request timed out after {}s. A very long transcript can do "
            "this; try again.".format(timeout)
        )
    except httpx.HTTPError as exc:
        raise LLMError("Could not reach the API: {}".format(exc))

    if resp.status_code != 200:
        # Surface the API's own error text when it sent one. Its wording names
        # the actual problem (bad key, unknown model, rate limit) far better
        # than a status code does.
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                detail = payload["error"].get("message") or ""
        except ValueError:
            detail = resp.text[:300]
        raise LLMError(
            "API returned {}{}".format(resp.status_code, ": " + detail if detail else "")
        )

    data = resp.json()
    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        raise LLMError("The API returned no text.")

    # Report the model the API says it used, not the one that was asked for --
    # they differ when an alias resolves to a dated snapshot, and the stored
    # provenance should record what actually wrote the brief.
    return text, data.get("model") or DEFAULT_MODEL
