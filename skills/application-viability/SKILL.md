---
name: application-viability
description: Produce an independent, deliberately un-flattering second-opinion score (0-100) on how likely a job application is to end in an accepted offer, from an interview transcript, an email thread, or an application's standing context. Use this whenever the user shares an interview transcript or recruiter email and asks how it went, whether a role is still alive, whether to keep investing effort, what to score it, or wants a read on a job application's viability — including when they only say "here's the transcript" or "what do you think of this one" without using the word "score". Also use it when they want a check against their own gut read of a pursuit.
---

# Application viability: a second opinion

Produce a number the user did not produce, from evidence they may have read
past. The value of this assessment is entirely in its independence — an
assessment that agrees with the user's instinct by default is worth nothing to
them. They already have their own read; they built this to get a different one.

This runs in a chat session, deliberately outside their Job Search CRM app.
The app's `score` field is filled in by hand. Nothing here writes to it — the
output is shaped to be *transcribed* into it.

## The question being scored

**How likely is this application to end in an offer the user accepts?**

That is the app's `Closed Won` stage, and it is a joint event: the employer
has to want them *and* they have to take it. A role that would clearly be
declined — bad comp, wrong level, a team they'd hate — is not a likely Closed
Won no matter how much the interviewer liked them. Score the whole path, not
just the hiring side. If the two halves diverge sharply, say so explicitly;
that split is often the most useful thing in the assessment.

Scale: `0` = dead. `100` = signed. Blank is a legitimate answer and means "no
basis to form a judgment" — it is *not* the same as a low score, and the app
keeps them apart on purpose. Recommend blank when the input genuinely doesn't
support a reading, rather than manufacturing a number.

## Do not anchor on their score

Read the input for evidence, not for the user's conclusion.

If their own `score`, `score_reason`, or evaluative notes appear in what they
pasted — from the app's edit page, from their own commentary — **skip past
them and do not read them as input**. Commit to a number from the primary
material first. Say in the output that their score was visible and was set
aside.

If they ask afterward how the two compare, or volunteer their number, *then*
engage with it: where the gap is, which piece of evidence each read is leaning
on, and what would settle it. Do not revise the original number quietly —
state it, then state the revision and why, so the disagreement stays legible.

## Workflow

1. **Take stock of the input.** Identify which of these are present: the
   interview transcript or meeting summary, the email thread body, the
   application's Context field, current Stage, Source, the job description,
   the resume used, and prior activity on the same application. Note what's
   missing — thin input caps how confident the output can be.

2. **Set the base rate from the stage.** Start at the prior for where the
   application sits, before reading a word of the transcript. See
   `references/scoring-calibration.md`. Most bad assessments come from
   scoring the *conversation* instead of the *pursuit*: a warm chat at
   Qualification is still an early-stage pursuit.

3. **Extract signals, quoting evidence.** Work through the material for the
   things that actually move outcomes — not tone. `references/reading-
   signals.md` catalogues what predicts advancement, what predicts a stall,
   and the courtesies that reliably read as positive but carry no
   information. Every signal claimed in the output must be traceable to
   something in the input; quote it.

4. **Move off the base rate, and account for the move.** Adjust up or down
   from the prior, with each adjustment tied to a quoted signal. If the
   final number is far from the base rate, the evidence for that distance
   has to be correspondingly strong.

5. **Look for the disconfirming read.** Before writing anything: what's the
   most credible case that this is going worse than it looks? Name it, even
   when rejecting it. An assessment that never considered the pessimistic
   reading is an echo, not a second opinion.

6. **Write the output** in the format below.

## Output format

Lead with the two paste-ready lines, so the user can drop them straight into
the app's score fields:

```
Score: 62
Reason: HM engaged on scope and named a specific problem, but no timeline and comp band still undiscussed.
```

`Reason` must fit on one line — target under 140 characters — because it goes
into a single-line field next to the number. It should name the actual driver,
not summarize the meeting.

Then, in prose (no bullet lists unless the user asks for them):

- **Where the number came from.** The base rate for the stage, and each
  adjustment made to it, with the quoted evidence behind each.
- **What's pulling it down.** The strongest case against this pursuit. Always
  present. If there's genuinely nothing, say that explicitly — it's a strong
  claim and should read like one.
- **What isn't known.** The specific unknowns that would move the number most,
  and roughly how far each would move it.
- **One thing to actually do.** A single concrete next action that would
  resolve the largest unknown — a question to ask, a follow-up to send, a
  thing to confirm. Not general advice; something they could do this week.

Keep the whole thing tight. This is a second opinion, not a report — if it
takes longer to read than the transcript, it has failed.

## Calibration honesty

Say the uncomfortable number. A user who only ever hears 70s has a decorative
tool. If the evidence supports 25, write 25 and defend it.

Equally: don't be contrarian for its own sake. If the pursuit really is
strong, a high score with clear evidence is the correct output. Independence
means the number comes from the evidence, not that it disagrees on principle.

Do not soften the number in the prose. A 35 explained in encouraging language
is a 35 the user will read as a 60.

## Relationship to `candidacy-check`

If a `candidacy-check` skill is also installed, they do different jobs and
shouldn't be confused. That one sweeps *every* Granola meeting tied to a
company and narrates how the process as a whole is going. This one produces a
single number and one-line rationale for *one* interaction or one
application's standing, in the shape the CRM's `score` / `score_reason` fields
expect. Use that one to understand a process; use this one to record a
reading. Running that one first and this one after is a reasonable sequence —
but if its narrative conclusion is in hand, treat it the same as the user's
own score: set it aside until a number has been committed to independently.

## References

- `references/scoring-calibration.md` — stage base rates, what each band of
  the 0–100 scale is claiming, and common miscalibrations. Read this before
  committing to a number.
- `references/reading-signals.md` — what actually predicts advancement in
  interview transcripts and recruiter email threads, and the polite noise
  that doesn't. Read this while working through the material.

## A note on what's being processed

Interview transcripts contain other people who did not agree to have their
words analyzed. That's the reason this lives here, in a chat session the user
invokes deliberately, rather than wired into the app where every imported
Granola note would be shipped off automatically. Don't quote third parties
more than the assessment requires, don't retain or restate their personal
details beyond what's needed, and keep the focus on the pursuit rather than on
characterizing the individuals in the room.
