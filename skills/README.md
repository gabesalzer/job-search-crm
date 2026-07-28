# Skills

Claude Skills that work alongside this app but are deliberately **not part of
it**. Nothing here is imported by the server, listed in `requirements.txt`, or
reachable from a route. They're folders of instructions you invoke in a chat
session; the app keeps zero LLM dependencies.

| Skill | What it does |
| --- | --- |
| `application-viability` | Reads an interview transcript, an email thread, or an application's context and returns an independent 0–100 win-likelihood score plus a one-line reason — in the exact shape the app's `score` / `score_reason` fields expect, so you can transcribe it in. |
| `candidacy-check` | Reads the whole arc of one company's process across every Granola meeting tied to it, calibrated against that application's **Source** and **Context** from the app. Returns a qualitative read — Strong / Encouraging / Mixed / Cooling / Unclear — not a number. |

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

## Using `candidacy-check`

Name a company and it finds that company's meetings itself. It will then ask
once for the application record — paste the edit page or screenshot it, and
give it **Source**, **Context**, **Stage**, and the **Stage history** rows
together. It works without them and says so, but Source in particular changes
the conclusion often enough to be worth the paste.

Source is doing more work than it looks like. It sets the baseline the meeting
signals are read against, because the same signal means different things
depending on how you got in the door:

- **Referral** — you arrived with borrowed credibility, so warmth is expected
  and carries little information, and a *generic* close counts against you: the
  referral should have bought better than boilerplate.
- **Recruiter Inbound** — enthusiasm is partly their job, so early warmth is
  discounted heavily; cooling is what's informative, since they had a specific
  reason to come looking.
- **Outbound** — silence is the default and means little, but any specificity
  counts for more than the same signal would from a referral.

Context is read for two things a pile of meeting notes can't tell you: which of
the criteria you said would decide this are *still open* several meetings in,
and whether you'd actually accept. A process moving briskly toward a role you've
already recorded reasons to decline doesn't get called Strong.

It reads Context for what you observed, not what you concluded — a read that has
already absorbed your own optimism isn't a second look at anything.
