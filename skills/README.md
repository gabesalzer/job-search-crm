# Skills

Claude Skills that work alongside this app but are deliberately **not part of
it**. Nothing here is imported by the server, listed in `requirements.txt`, or
reachable from a route. They're folders of instructions you invoke in a chat
session; the app keeps zero LLM dependencies.

| Skill | What it does |
| --- | --- |
| `application-viability` | Reads an interview transcript, an email thread, or an application's context and returns an independent 0–100 win-likelihood score plus a one-line reason — in the exact shape the app's `score` / `score_reason` fields expect, so you can transcribe it in. |

## Why they live outside the app

The scoring fields on Meeting and Email Thread are filled in by hand. That's a
choice, not a gap: automating it inside the app would mean every imported
Granola note gets shipped to a third-party API as a side effect of ordinary
use, and those transcripts contain other people. Doing the same work in a
session you start, on material you hand over deliberately, keeps that from
being invisible or automatic. `ARCHITECTURE.md` has the longer version.

## Installing

Copy (or symlink) a skill folder into your Claude skills directory:

```bash
cp -r skills/application-viability ~/.claude/skills/
```

A symlink keeps it in sync with the repo as you edit it:

```bash
ln -s "$(pwd)/skills/application-viability" ~/.claude/skills/application-viability
```

Then start a new session and it'll trigger on its own when you paste in a
transcript or ask how an application is looking — or call it by name.

## Using `application-viability`

Paste in as much of the picture as you have and ask for a read. The more of
these it gets, the better the number:

- the meeting transcript or summary, or the email thread body
- the application's **Context** field and current **Stage**
- the **Source** (Referral / Recruiter Inbound / Outbound)
- the job description, and which resume you used

It returns two paste-ready lines followed by a short written assessment:

```
Score: 62
Reason: HM engaged on scope and named a specific problem, but no timeline and comp band still undiscussed.
```

Drop those into the score fields on that meeting or thread, and the
application's activity timeline picks up the trend automatically.

Two behaviours worth knowing about. It will ignore any score you've already
recorded if it appears in what you paste — the point is a view that isn't
yours, and one that's read yours first isn't independent. Tell it your number
*afterward* and it'll engage with the gap directly. And it will recommend
leaving the score blank when the material genuinely doesn't support a
judgment, rather than inventing a low number: blank and `0` are different
claims, and the app stores them differently.
