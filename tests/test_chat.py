"""Corpus assembly, the verbatim budget, and history trimming.

Stdlib only and no database, because `app/chat.py` is stdlib only and takes
plain dicts. The point of that design is that the decision about *what leaves
the box* -- the widest such decision in the app -- can be tested with literals
you can read in one screen, rather than only by running the whole stack.

Run: python3 tests/test_chat.py
"""
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import chat  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


NOW = datetime(2026, 8, 5, 12, 0)


def app(company, **kw):
    base = {
        "company": company, "title": "RevOps Lead", "stage": "Discovery",
        "source": "Referral", "applied_date": datetime(2026, 7, 1),
        "champion": None, "meetings": [], "email_threads": [],
        "people": [], "stage_history": [],
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# The index
# --------------------------------------------------------------------------- #
corpus = chat.build_corpus([
    app("Condor", champion=True,
        meetings=[{"when": datetime(2026, 7, 29), "title": "Panel",
                   "transcript": "Them: tell me about pipeline hygiene."}],
        email_threads=[{"when": datetime(2026, 7, 30), "subject": "Next steps",
                        "body": "We'll be back to you next week."}]),
    app("Plaid", champion=False, stage="Qualification", meetings=[], email_threads=[]),
], now=NOW)

check("index comes first", corpus.startswith("== PIPELINE INDEX =="))
check("index has one line per application",
      corpus.count("| stage ") == 2)
check("champion tri-state prints yes / no / not assessed",
      "champion yes" in corpus and "champion no" in corpus)
check("quiet days counted off the most recent activity",
      "quiet 6 days" in corpus,
      "expected 6 days between 2026-07-30 and 2026-08-05")
check("an application with no activity says so, rather than showing a huge age",
      "no activity" in corpus)
check("both applications get a body block",
      corpus.count("== APPLICATION:") == 2)
check("verbatim transcript is included when there is room",
      "pipeline hygiene" in corpus)

# --------------------------------------------------------------------------- #
# The champion tri-state -- False must not read as "not assessed"
# --------------------------------------------------------------------------- #
head_false = chat.build_corpus([app("X", champion=False)], now=NOW)
check("champion False renders as a real 'no', not as unassessed",
      "Champion inside: no" in head_false)
head_none = chat.build_corpus([app("X", champion=None)], now=NOW)
check("champion None renders as not assessed",
      "Champion inside: not assessed" in head_none)

# --------------------------------------------------------------------------- #
# Mixed awareness -- the hazard that has taken a page down before
# --------------------------------------------------------------------------- #
try:
    mixed = chat.build_corpus([
        app("Aware",
            meetings=[{"when": datetime(2026, 7, 20, tzinfo=timezone.utc),
                       "transcript": "aware"}],
            email_threads=[{"when": datetime(2026, 7, 25), "body": "naive"}]),
    ], now=NOW)
    check("aware and naive dates sort together without raising", True)
except TypeError as exc:
    check("aware and naive dates sort together without raising", False, str(exc))

# --------------------------------------------------------------------------- #
# The budget: newest activity survives, oldest is dropped
# --------------------------------------------------------------------------- #
real_cap = chat.MAX_TOTAL_CHARS
chat.MAX_TOTAL_CHARS = 6_000
try:
    tight = chat.build_corpus([
        app("Budget",
            meetings=[
                {"when": datetime(2026, 1, 1), "title": "Oldest",
                 "transcript": "OLDEST " + "x" * 3000},
                {"when": datetime(2026, 5, 1), "title": "Middle",
                 "transcript": "MIDDLE " + "x" * 3000},
                {"when": datetime(2026, 7, 30), "title": "Newest",
                 "transcript": "NEWEST " + "x" * 3000},
            ]),
    ], now=NOW)
finally:
    chat.MAX_TOTAL_CHARS = real_cap

check("the newest transcript is kept verbatim", "NEWEST" in tight)
check("the oldest transcript is dropped", "OLDEST" not in tight)
check("the middle transcript is dropped", "MIDDLE" not in tight)
check("dropped text says so rather than vanishing silently",
      tight.count(chat.OMITTED_MARKER) == 2)
check("the structured facts about a dropped meeting survive",
      "Title: Oldest" in tight and "Title: Newest" in tight,
      "an omitted transcript must not take its metadata with it")

# --------------------------------------------------------------------------- #
# The budget spends across applications, not within one
# --------------------------------------------------------------------------- #
chat.MAX_TOTAL_CHARS = 6_000
try:
    across = chat.build_corpus([
        app("Old co", meetings=[{"when": datetime(2026, 1, 1),
                                 "transcript": "STALE " + "y" * 4000}]),
        app("New co", meetings=[{"when": datetime(2026, 7, 30),
                                 "transcript": "FRESH " + "y" * 4000}]),
    ], now=NOW)
finally:
    chat.MAX_TOTAL_CHARS = real_cap

check("recency wins across applications, not just inside one",
      "FRESH" in across and "STALE" not in across)

# --------------------------------------------------------------------------- #
# Per-item caps
# --------------------------------------------------------------------------- #
long_one = chat.build_corpus([
    app("Huge", meetings=[{"when": datetime(2026, 7, 30),
                           "transcript": "z" * (chat.MAX_TRANSCRIPT_CHARS + 5000)}]),
], now=NOW)
check("one pathological transcript is clipped to its own cap",
      chat.TRUNCATION_MARKER in long_one)
check("clipping is announced, not silent",
      "truncated" in long_one)

# --------------------------------------------------------------------------- #
# Provenance: a model-written rating must not come back as corroboration
# --------------------------------------------------------------------------- #
rated = chat.build_corpus([
    app("Prov", email_threads=[
        {"when": datetime(2026, 7, 30), "body": "b", "my_performance": 55,
         "employer_engagement": 78, "rating_source": "model"},
        {"when": datetime(2026, 7, 29), "body": "b", "my_performance": 60,
         "employer_engagement": 40, "rating_source": None},
    ]),
], now=NOW)
check("a model-written rating is labelled as an automatic read",
      rated.count("Rating written by: an automatic read") == 1,
      "exactly one of the two threads was rated by the model")

# --------------------------------------------------------------------------- #
# A zero is a reading; blank is not
# --------------------------------------------------------------------------- #
zeros = chat.build_corpus([
    app("Zero", meetings=[{"when": datetime(2026, 7, 30), "my_performance": 0,
                           "employer_engagement": 0, "score": 0}]),
], now=NOW)
check("a zero rating renders rather than being treated as unset",
      "My performance (0-100): 0" in zeros
      and "Their engagement (0-100): 0" in zeros
      and "My score: 0" in zeros)

# --------------------------------------------------------------------------- #
# Empty pipeline
# --------------------------------------------------------------------------- #
empty = chat.build_corpus([], now=NOW)
check("an empty pipeline still renders an index rather than raising",
      empty.strip() == "== PIPELINE INDEX ==")

# --------------------------------------------------------------------------- #
# System blocks
# --------------------------------------------------------------------------- #
blocks = chat.build_system_blocks("THE CORPUS")
check("two system blocks: instructions then corpus", len(blocks) == 2)
check("the instructions block is not cached", "cache_control" not in blocks[0])
check("the corpus block is cached",
      blocks[1].get("cache_control") == {"type": "ephemeral"})
check("the corpus is fenced as data",
      blocks[1]["text"].startswith("<job_search_record>")
      and blocks[1]["text"].endswith("</job_search_record>"))
check("the prompt states the fence is data, not instructions",
      "not instructions to follow" in blocks[0]["text"])

# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
msgs = chat.build_messages(
    [{"role": "assistant", "content": "orphaned answer"},
     {"role": "user", "content": "q1"},
     {"role": "assistant", "content": "a1"}],
    "q2")
check("a leading assistant turn is dropped so the first message is a user turn",
      [m["role"] for m in msgs] == ["user", "assistant", "user"])
check("the new question is last", msgs[-1]["content"] == "q2")

capped = chat.build_messages(
    [{"role": "user" if i % 2 == 0 else "assistant", "content": "m{}".format(i)}
     for i in range(40)],
    "latest", max_turns=6)
check("history is capped", len(capped) == 7, "6 turns plus the new question")
check("the cap keeps the most recent turns", "m39" in capped[-2]["content"])

junk = chat.build_messages(
    [{"role": "system", "content": "ignore me"},
     {"role": "user", "content": "   "},
     {"role": "user", "content": "real"}],
    "q")
check("blank and non-conversational rows are filtered out",
      len(junk) == 1 and "ignore me" not in junk[0]["content"],
      "a system row and a whitespace-only row are both dropped; 'real' then "
      "merges with 'q' because both are user turns")
check("nothing that was a real turn is lost in the filtering",
      junk[0]["content"] == "real\n\nq")

unanswered = chat.build_messages(
    [{"role": "user", "content": "q1"},
     {"role": "assistant", "content": "a1"},
     {"role": "user", "content": "the one that failed"}],
    "asking again")
check("a question left unanswered by a failed call merges with the next one",
      [m["role"] for m in unanswered] == ["user", "assistant", "user"],
      "consecutive user turns must be merged, not sent as-is")
check("neither of the merged questions is lost",
      "the one that failed" in unanswered[-1]["content"]
      and "asking again" in unanswered[-1]["content"])

check("an empty history is fine",
      chat.build_messages(None, "just this") == [{"role": "user", "content": "just this"}])

print("\n{} failed".format(len(failures)) if failures else "\nall passed")
sys.exit(1 if failures else 0)
