# Architecture & Data Model

This document captures the data model for the Job Search CRM and, just as
importantly, the *reasoning* behind each decision. The app treats a job search
like a sales pipeline, so the model is designed around a Salesforce (SFDC)
analogy.

## The core analogy

| This app          | Salesforce object          | Why                                                                 |
| ----------------- | -------------------------- | ------------------------------------------------------------------- |
| **Company**       | Account                    | The organization a role sits at (or a staffing agency).             |
| **Job Posting**   | Product                    | Catalog data: a role that exists in the world whether or not I act on it. |
| **Job Application** | Opportunity              | Pipeline data: exists *because* I decided to pursue a posting.      |
| **Person**        | Contact                    | A recruiter, hiring manager, interviewer, or referral.              |
| **Stage History** | OpportunityFieldHistory    | An append-only log of stage changes, so the funnel is measurable.   |
| **Resume**        | (a versioned asset)        | Which resume version was attached to an application.                |
| **Email Thread**  | Email/Task on a Contact    | A recruiter or hiring-manager email exchange.                       |

The Posting → Product mapping is the one that isn't obvious. It's the right fit
because a posting, like a product in a catalog, has its own lifecycle
independent of whether I ever act on it, whereas an application (like an
opportunity) only exists because I chose to pursue one. That difference in
lifecycle and volume is exactly why they are two objects, not one (see below).

## Relationships

```
Company (Account)
 ├── (master-detail) ──> Job Posting      (Product)
 ├── (master-detail) ──> Job Application  (Opportunity)
 └── (master-detail) ──> Person           (Contact)

Job Application
 ├── (lookup, nullable) ──> Job Posting   (the posting I applied to, if any)
 ├── (lookup, nullable) ──> Resume        (the resume version I used)
 └── (master-detail) <── Stage History    (one row per stage change)

Person
 ├── (lookup, nullable) ──> Job Application  (the application this person is tied to)
 └── (many-to-many) <──> Email Thread        (conversations involving this person)

Email Thread
 └── (lookup, nullable) ──> Job Application  (unset until the thread is about a role)
```

### Master-detail vs. lookup vs. many-to-many — what it means here

In Salesforce, **master-detail** means the child cannot exist without the parent:
a required parent, cascade delete, and the parent can roll up aggregates of its
children. **Lookup** means an optional pointer: nullable, no forced cascade, no
automatic rollups. Neither fits a relationship where either side can have many
of the other — that's **many-to-many**, via a join table.

Because this app is plain SQLAlchemy/SQLite rather than the Salesforce platform,
those concepts map directly to SQL:

- **Master-detail** → `NOT NULL` foreign key with `ON DELETE CASCADE`.
- **Lookup** → nullable foreign key, `ON DELETE SET NULL`.
- **Many-to-many** → a join table with `ON DELETE CASCADE` on both foreign
  keys, so deleting either side removes the *association*, never the other
  side's actual row.

### Why Company has a *direct* master-detail to Job Application

A Job Application links to a Company two ways: directly, and indirectly through
its Job Posting. The direct link is intentional and mirrors how an Opportunity
carries its own Account lookup rather than inheriting Account only through a
Product line item. It matters the first time you log a cold-outreach or referral
application that has **no formal posting attached** — the application still needs
a company.

### Why Person's company is independent of the Application's company

Person has a master-detail to Company (the person's *own* employer) and a
separate nullable lookup to Job Application. These two companies are deliberately
**allowed to differ**, because a person may be an external recruiter at a
staffing **agency** working a role on behalf of a different hiring company. An
early instinct was to add a validation rule forcing them to match — that would
have been a bug, because it would break exactly the agency-recruiter case we want
to capture.

### `company_type`

Because Company now covers both real employers and staffing agencies, it carries
a `company_type` field (`Employer` / `Agency` / `Both`). Without it, agencies and
employers blend together in any company-level rollup. With it, you can ask two
different questions cleanly: "which **employers** am I making progress with?" and
"which **agencies / recruiters** get me traction?"

## Why Posting and Application are two objects (not merged)

A tempting simplification is to merge Job Posting and Job Application into one
object and use a stage like `Opened` → `Closed` to tell "jobs I'm looking at"
apart from "jobs I applied to." We deliberately **did not** do this:

1. **Volume mismatch.** Firecrawl may surface 20–50 postings for every one you
   actually apply to. Merging would turn every scraped listing into a full
   pipeline record the moment it's ingested — hundreds of rows you never pursued.
2. **The funnel would be polluted.** Stage History exists to answer "where do I
   fall off." Merging mixes two different processes into one stage field: *my*
   triage decision (do I bother applying) and the *recruiter's* pipeline decision
   (do they move me forward). Those have different owners and meanings, and you'd
   have to filter one out every time you computed a real conversion rate.
3. **Re-application is a real case.** Applying to the same posting later (new
   resume, or the role is reposted months on) is clean with two objects — a new
   Application pointing at the same Posting — but awkward if they're merged.

This is the same reason Salesforce keeps Product and Opportunity separate.

**The friction fix:** we keep them separate but do **not** require an Application
until you actually apply. Rating a posting (thumbs up/down/neutral + reason) is a
lightweight action entirely on the Posting. Only when you decide to apply does a
real Application row get created, pointing back at that Posting. So the
Application count only ever reflects postings you actually pursued, and Stage
History only ever reflects real pipeline movement.

## Feedback loops the model is built to drive

- **Top level — sourcing.** The app shares postings; you give a thumbs
  up/down/neutral and a reason. Over time this hones what gets surfaced. Stored as
  `my_rating`, `rating_reason`, `rated_at` directly on Job Posting (kept simple
  for v1; can graduate to its own event-log object later for re-rating / ML
  features).
- **Middle level — pipeline.** Track which applications are at which stage. Because
  Stage History is append-only, this becomes a measurable funnel rather than a
  snapshot, which lets you compute stage-to-stage conversion (e.g. "Discovery →
  Takehome"). Pair `stage` with a `lost_reason` picklist (ghosted, rejected
  after screen, rejected after onsite, declined by me, …) — that's the field that
  answers *where and why* you fall off.
- **Per-interaction — calibration.** Every Meeting and Email Thread can carry a
  0–100 win-likelihood score with a reason and a timestamp. Because each one is
  attached to an application that eventually reaches a terminal stage, these
  become judgments you can check against outcomes — "was I overconfident after
  recruiter screens?" is answerable, not a feeling. See "Win-likelihood
  scoring" below.
- **Analysis questions the model supports:** which resume versions progress; which
  JD/company types you gain traction with; whether a particular resume moves you
  forward; whether you have a "champion"; which origin (`source`: referral vs.
  recruiter inbound vs. outbound) actually converts, and how much cheaper the
  ones that do are to run; and, because Person's company is
  independent, "which recruiters (regardless of agency) get me furthest" as its
  own question separate from "which employers."

## Stages

Ordered pipeline (Opportunity-style), stored as an enum:

`Staging ⇢ Qualification → Discovery → Takehome → Executive Signoff → Negotiation → Closed Won / Closed Lost`

The dashed arrow is deliberate. `Staging` and `Closed Lost` are both real
stages you can park a record in, but neither is a rung of the funnel — see
*The `Staging` stage* below, and the note above `STAGE_ORDER` in
`app/models.py`.

Every time `stage` changes, a Stage History row is written automatically
(`from_stage`, `to_stage`, `changed_at`) via a SQLAlchemy attribute event
listener, so the funnel is captured without relying on the caller to remember.

**Why macro, decision-oriented stages instead of literal interview-call
names** (the original v1 model was `Saved → Applied → Recruiter Screen →
Hiring Manager Screen → Onsite / Technical → Offer`): a stage model built
around *which specific call this is* doesn't generalize, because every
employer structures their loop differently — some skip a recruiter screen,
some run five rounds, some replace the onsite with a takehome. Sales
opportunity stages don't work that way either: they're built around the
*seller's own* qualification and pursuit logic (would this deal close if we
invested further?), not a mirror of the buyer's internal process, precisely
because every buyer's process differs too. The current stages follow that
same discipline — each one is a question you can answer about your own
pursuit of the role, not a specific call type:

- **Qualification** — is this worth pursuing at all (folds what used to be
  Saved + Applied — both describe "not yet meaningfully engaged").
- **Discovery** — how strong a fit, in both directions (folds Recruiter
  Screen + Hiring Manager Screen — both are mutual fact-finding, just at
  different depths).
- **Takehome** — can you actually do the work, proven concretely. Named for
  the specific artifact rather than a generic "Technical," since plenty of
  non-engineering loops include some form of takehome exercise even when
  it isn't coding-specific.
- **Executive Signoff** — final internal approval, whether or not you're
  ever in the room for it — the candidate-side equivalent of a deal needing
  an economic buyer's blessing.
- **Negotiation** — an offer is on the table.

A **Go/No-Go** stage (used in the original sales version to confirm a
completed trial had a real path to close) was deliberately left out: a
seller can gather real signals for that call — confirmed budget, an
identified economic buyer, competitive standing — and has the leverage to
decline running a costly POC without them. A candidate has neither the
visibility nor the leverage to make that judgment about an employer before
an onsite/takehome loop, so forcing the stage would mean pretending to know
something you can't actually know. Applications aren't required to touch
every stage in order — a given company might skip Takehome entirely, or
fold Executive Signoff into the same conversation as Negotiation.

An application migrates through `data/jobsearch.db` automatically on
deploy (see `migrate_stage_names()` in `app/database.py`) — existing
applications and their stage history are remapped to the new names rather
than reset, and now-redundant history rows (e.g. a logged Recruiter Screen →
Hiring Manager Screen move, which collapses into a Discovery → Discovery
no-op once both fold into one stage) are cleaned up rather than left as
misleading duplicate entries.

### The `Staging` stage

`Staging` is where a role sits *before* you've applied to it. You found the
posting, you want it, and the work in front of you isn't submitting an
application — it's working the angle in. Jellyfish is the canonical case: the
VP of Marketing there is a former colleague, so the right first move is
reaching out to her, not dropping a resume into a form.

Read this section next to the one below it, because the two decisions look
contradictory and aren't. **`Applied` is an event; `Staging` is a state.**
Applying happens on a date and is then over — there's nothing to occupy, so it
belongs in a nullable `applied_date` column. Staging is somewhere a pursuit
*sits*, accumulating meetings and notes, for as long as it takes to land the
intro. States are what stages are for. That's the whole test: if you can be
"in" it for a week, it's a stage; if it's a thing that happened, it's a date.

Staging also disambiguates something the date column can't. A blank
`applied_date` currently means two unrelated things — *haven't applied yet*
and *never will apply, they came to me* — and both kinds of record sat at
Qualification, indistinguishable from a role you did apply to and never heard
back on. Staging pulls the first meaning out into the stage, where it's
visible on the board.

**It is not in `STAGE_ORDER`, and that's load-bearing.** `stage` defaults to
`Qualification`, so every application is *born* there; combined with the
prefix-crediting described below, whichever stage is the default is reached by
100% of applications by construction and is worthless as a denominator.
Putting Staging at the front of `STAGE_ORDER` and making it the new default
would move that dead denominator onto Staging rather than removing it — which
is precisely the objection that keeps `Applied` from being a stage at all.
There's precedent either way: `STAGE_ORDER` has always been a *subset* of
`Stage`, because `Closed Lost` is a terminal exit rather than a depth. Staging
is the same shape at the other end of the pipeline.

Leaving it out costs nothing. An application at Staging hasn't entered the
pipeline and correctly counts toward no stage, so staged roles can pile up
without diluting conversion. When one converts, the move writes a real
`Staging → Qualification` Stage History row — and *that row* is the honest
cohort filter for the metric Staging enables: **of the roles I staged, how
many turned into a live pursuit, and how long did the angle take?** Same shape
as the `applied_date` cohort in `/api/analytics/applied-conversion`: real
dated transitions, never credit implied by position in a list. A
recruiter-inbound role never passes through Staging and is therefore neither a
staged success nor a staged failure — it's simply outside that measurement,
which is correct. The endpoint isn't built yet;
`tests/test_staging_stage.py` proves out its shape, including that leaving
Staging for `Closed Lost` counts as the angle failing rather than converting.

Adding the enum value required no migration. There's no `create_constraint`
anywhere in `models.py`, so SQLAlchemy 2.0's default `create_constraint=False`
applies and the existing SQLite table has no CHECK constraint to fight.

### Why there's no `Applied` stage

The obvious gap in the stage list is that nothing marks *"I submitted this."*
The tempting fix is a new `Applied` stage in front of Qualification. It
doesn't work, and the reason is worth writing down because it will look like
an omission again later.

Every application is **born** at Qualification — `stage` carries
`default=Stage.QUALIFICATION`, so the value is a starting state rather than
something a pursuit ever *reaches*. That makes Qualification useless as a
funnel denominator: 100% of applications reach it by construction, and the
`changed_at` on its Stage History row records when the record was typed in,
not when anything happened in the world. Putting `Applied` in front of it
would relocate the problem, not solve it — records would simply be born at
`Applied` and *that* would become the meaningless stage.

A nullable `applied_date` column on Job Application is strictly better. It's a
fact about the world rather than a workflow position: a submission either
happened on a date or it didn't. It is absent exactly when it should be — a
recruiter-inbound role you never applied to has no applied date and correctly
drops out of any application-cohort math, where an `Applied` stage would have
forced you to either lie or leave a hole in the ordering. And it can be
backfilled months later for a pursuit whose early stages were never logged,
which a stage transition can't be without fabricating history.

`GET /api/analytics/applied-conversion` is what that column buys. The cohort
is "applications I actually submitted," and the first stage that means
anything is **Discovery** — the first one you have to be *let into*. Each
stage reports how many of the cohort reached it, conversion off the applied
count, and median days from submission. Timing is measured only off real Stage
History transitions, so an application credited with a stage by implication
(reaching a later stage implies passing through the earlier ones) counts
toward `reached` but contributes nothing to the median.

### Reaching a stage credits every stage before it

Both the funnel and the applied-conversion endpoint compute "how many
applications ever reached stage X" by crediting the whole prefix of
`STAGE_ORDER` up to and including the stage observed, from Stage History rows
*and* from the application's current stage.

Crediting only the observed stage seems more literal and is wrong, because
applications routinely lack a history row for a stage they demonstrably passed
through. An application created directly at Discovery has an opening row of
`None → Discovery` and no Qualification row at all. Stages can also be skipped
outright — the model says so explicitly, since plenty of loops have no
Takehome. Under literal crediting, a *later* stage can then report more
applications than an *earlier* one, which isn't a funnel: it produced
Qualification → Discovery conversion of 100% on a dataset where the true
figure was 50%, and can exceed 100% outright.

The invariant to preserve is monotonicity — the counts down `STAGE_ORDER`
never increase — and it's asserted directly in `tests/test_applied_analytics.py`
rather than left as a comment. Note this affects *depth* only: the closed
stages aren't in `STAGE_ORDER`, so a Closed Lost application keeps whatever
depth its history earned it and gains nothing from closing.

### Every date on an Application is hand-correctable

`applied_date`, `created_at`, `last_activity_date` and `updated_at` all render
as editable inputs on the Application edit page, including when they're null —
a never-set date shows as an empty field marked "not set" rather than
vanishing. The reason is the same one behind the editable Stage History rows:
the date something was *logged here* is routinely later than the date it
happened. You log Monday's rejection on Thursday. A tracker whose dates you
can't correct ends up measuring your data-entry habits instead of your job
search, and every conversion-velocity number computed off it inherits that
error.

`updated_at` needs care, because it carries `onupdate=_utcnow`. SQLAlchemy
fires `onupdate` only when the column is absent from the UPDATE's SET clause,
so an explicit assignment does win — but the form round-trips the current
value on every save, and blindly assigning it back would freeze "last
modified" at whatever was in the box. So the handler only overrides when the
submitted value actually *differs* from what's stored, and the comparison
truncates both sides to the minute: `<input type="datetime-local">` can't
express seconds, so raw datetime equality would report a spurious edit on
every single save.

A blank date field means "leave it alone" for `created_at` and
`last_activity_date` — clearing them by accident would scramble ordering. The
exception is `applied_date`, where blank is a real answer meaning "I never
applied to this," which is why it's the one date the form can clear.

## Meetings and Email Threads

Both are activity records, but deliberately **separate objects**, not one
merged "Interaction" table:

- **Meeting** — master-detail to Job Application. A meeting exists because
  you're pursuing a specific role; through the application it also reaches
  the Posting (JD) and Resume, which is what makes "questions asked, by JD /
  by resume" analysis possible. Shape: a single dated event (`meeting_date`)
  with a summary and transcript.
- **Email Thread** — many-to-many with **Person** (via the `email_thread_people`
  join table), with only an optional lookup to Job Application. A thread is
  fundamentally a conversation with one or more people, and — like a Person's
  own company — that conversation can predate any application: a cold recruiter
  email lands in your inbox before you've decided to pursue anything. Forcing
  every thread to have an application would make cold outreach impossible to
  log until after the fact. Shape: a subject, a body, and a message span
  (`started_at` → `last_message_at`), not a single instant.

Meeting keeps its master-detail to Application because a meeting can't exist
without one — you don't sit down for an interview about nothing. Email Thread
deliberately doesn't mirror that: it has neither a required master (no forced
Application) nor a single required party (many-to-many Person, not a required
FK) — see "Why Email Thread has no distinguished 'primary' person" below for
why the latter was a later correction, not the original design.

Different required fields, different natural lifetimes — merging Meeting and
Email Thread into one object would mean either making Application nullable on
both (losing the not-null guarantee Meeting relies on) or forcing every
early-stage email to fabricate an application it doesn't have yet. Two
objects, cleanly typed, avoids both.

### Ingestion: why manual paste/upload, not a live Gmail sync

Three ways to get email threads in were considered: paste the text by hand,
have an assistant pull threads on demand via an MCP-connected Gmail session
and push them in, or build a full Gmail OAuth2 integration into the app
itself (the "GRANOLA_API_KEY pattern" — the app polls on its own).

We started with the first, for a reason worth stating plainly: an MCP Gmail
connector grants whatever session it's attached to broad read access to the
*whole* inbox, not just job-search threads — that's a real access decision,
independent of how the app itself is built, and shouldn't be an incidental
side effect of picking the easy ingestion path. A live OAuth2 integration
would actually be *more* scoped in principle (read-only, filterable by
label/sender) — but is meaningfully heavier to build and isn't justified
until the manual flow proves the data model earns its keep. So v1 optimizes
for the cheapest, most self-contained path: paste text directly, or **upload
a file** — a PDF export of the thread works well and is extracted with the
same `pypdf`/`python-docx` pipeline Resume upload already uses (see
`app/services/resume_extract.py`). No external credential, no chat-session
dependency, same trust boundary as uploading a resume.

### Auto-parsing a Gmail export

Gmail's own "Print all" export (the printer icon on an open thread) has a
very regular, machine-readable shape: every message header reads
`<Sender> <email> <Weekday>, <Month> <Day>, <Year> at <H:MM> <AM/PM>`, and
the subject sits on its own line right before the first one. That's regular
enough to lift structured fields out of unstructured text —
`app/services/email_parse.py` parses subject, participants (every email
address found), and the thread's first/last message timestamps directly
from the extracted text. Verified against a real Gmail export PDF, not
guessed at (see `tests/test_email_parse.py`). Parsed values only ever fill
in a field you left blank; anything typed by hand always wins, the same
override principle used everywhere else auto-extraction touches a form
(resume text, Granola-imported meeting fields).

### Why Email Thread has no distinguished "primary" person

The original design gave `EmailThread` a required `person_id` — a thread was
always *with* one person. That broke on a real 3-way thread (a recruiter
looping in a hiring manager): only the first sender got linked, and the
other two people on the conversation were invisible from the thread and from
their own Person record. Picking *which* sender should be "the" person is
also inherently arbitrary — an intro thread doesn't have a primary party any
more than a group email does.

The fix was to drop the single required FK entirely in favor of a genuine
many-to-many (`email_thread_people` join table, `ON DELETE CASCADE` on both
sides — see "Master-detail vs. lookup vs. many-to-many" above). The
deliberate trade-off: a thread can now end up with **zero** linked people
(everyone unchecked, or the last linked person deleted) and that's treated
as a valid state, not an error — an orphaned thread just sits there until
you either relink it or delete it by hand. What master-detail would have
given up for free — cascading the thread away when its "owner" is deleted —
isn't worth reintroducing a fake distinguished owner to get back. See
`test_deleting_only_linked_person_unlinks_but_thread_survives` and
`test_thread_can_have_multiple_people_linked_at_once` in
`tests/test_cascade_design.py`, and `migrate_email_thread_people()` in
`app/database.py` for how existing single-person threads were carried over
to the join table without losing data.

### Auto-creating People from a thread's senders

You don't have to create a thread's People by hand before logging it. The
parser extracts every real message **sender** (not just any email mentioned
in the text) from the Gmail-shaped headers, and identifies which one is
*you* from the account-owner banner line Gmail always prints at the top of
an export. Whatever's left — everyone else on the conversation — gets
found-or-created as a Person automatically, and **all** of them are linked to
the thread, not just the first one.

**Email address is the dedup key**, compared case-insensitively: the same
address always resolves to the same Person, whether it shows up in this
thread, a later one, or one you upload for a completely different
application. An existing match is returned as-is and never overwritten, so a
later, blanker upload can't clobber a name, role, or notes you've since
filled in by hand.

When a brand-new email needs a brand-new Person, it also needs a Company
(Person's own company is `NOT NULL`) — inferred from the email's domain.
That lookup checks existing companies by their **website's domain** first
(so a second person at an already-known company, e.g. a hiring manager at a
company you already have a recruiter contact for, reuses that company
instead of creating a duplicate), and only creates a new one, named from a
rough cleanup of the domain (`condor-software.com` → "Condor Software"), if
nothing matches. That guess is a starting point, not a final answer — like
every other auto-created record in this app, it's fully editable afterward.
An auto-created Person's role defaults to `Other` for the same reason: better
an honest unknown than a wrong guess at Recruiter vs. Hiring Manager vs.
Interviewer.

If a thread has more than one non-you sender (e.g. a recruiter loops in a
hiring manager mid-thread), every one of them gets found-or-created as a
Person **and** linked to the thread — useful on its own, since your People
list fills in as a side effect of just logging threads, and matches what the
thread actually was: a conversation with more than one person at once. An
explicit selection in the People checkbox list always overrides
auto-detection entirely (any box checked means "use exactly this set," not
"add to the detected set"), same override rule as every other auto-filled
field on this form. On create, if the text isn't Gmail-shaped and nothing
was checked by hand, thread creation fails with a clear error rather than
guessing; on edit, leaving everyone unchecked with nothing auto-detectable
is allowed and just unlinks the thread from everyone (see "Why Email Thread
has no distinguished 'primary' person" above) — see
`_resolve_thread_people()` in `app/routers/ui.py`. This whole path is proven
against representative cases (case-insensitive matching, a brand-new email,
a second person at a known company, two threads landing on two different
companies) in `tests/test_person_from_email.py`.

### One timeline, two objects

An Application's edit page shows an **Activity** related list that merges
its Meetings and Email Threads into one chronologically-sorted view, rather
than two separate lists you'd have to mentally interleave. This is a
display-layer merge only — no new table, no schema change: the route
handler (`_activity_timeline()` in `app/routers/ui.py`) fetches both
relationships, normalizes each row into a common `{type, when, title, sub,
url, score}` shape, and sorts by whichever timestamp best represents "most recent
activity" for that row type (`meeting_date` for a Meeting, `last_message_at`
for a thread, so a thread with a fresh reply surfaces near the top rather
than staying pinned at when it started). Salesforce's own Activity Timeline
works the same way under the hood — Tasks and Events are different objects
with different fields, merged and sorted at the UI layer, not in the schema.

## Win-likelihood scoring

Meeting and Email Thread each carry the same three nullable columns: `score`
(0–100), `score_reason`, and `scored_at`. The score answers one question —
*given what just happened in this interaction, how likely is this application
to end up Closed Won?*

### Why the score lives on the activity, not the Application

The obvious place to put a win-likelihood number is on the Application, next
to `stage`. That was rejected on purpose. A single field on the Application
can only ever tell you where you stand **right now**; the same judgments
recorded against each interaction turn into a time series, and the series is
where the information actually is. A 55 on its own says almost nothing. A 55
that was 80 last week says the pursuit is cooling and you should do something
about it this week — and that's visible only if the earlier reading survived
instead of being overwritten.

This is the same reason Stage History exists as an append-only child rather
than as a "previous stage" column on the Application. Both are the choice to
keep the history rather than the latest value.

The Application's own number is therefore **derived at display time**
(`_score_rollup()` in `app/routers/ui.py`) rather than stored: latest reading,
the change from the one before it, how many readings back it, and how old the
latest one is. Nothing to keep in sync, no backfill when you rescore
something, and no possibility of a stored rollup quietly disagreeing with the
rows it summarizes.

Note what's deliberately *not* there: an average. Averaging a 22 from a screen
that went badly with an 80 from two weeks earlier produces a 51 describing a
moment that never existed. The latest reading is your current position; the
delta is the part carrying information.

### Where the rollup surfaces, and why it carries an age

The rollup renders in two places: the Application edit page, above the
activity timeline, and — since it's what makes a pipeline scannable — on each
board card. A pipeline view whose whole purpose is comparing pursuits at a
glance is the wrong place to hide the one number that ranks them.

Both are wrapped in a truth condition rather than a placeholder: the rollup
returns `None` when nothing on that application has been scored, and the
widget disappears entirely instead of rendering an empty box or a zero. Blank
and `0` are different claims here, the same way they are on the score field
itself.

`stale_days` exists because a number with no age on it lies by omission. An 80
from six weeks ago and an 80 from yesterday are the same digits describing
completely different situations, and on a board — where a column of them gets
scanned at once — the confident-looking old one is exactly the card that
misleads. Past two weeks the age renders in the warning colour. It follows the
sort key, so it measures from the event rather than from the keystroke:
"nothing has happened here in 31 days" is the thing worth acting on, and it
stays true no matter when you got around to typing the number in. An activity
with no usable date on any field reports `None` rather than an age — *unknown
when* is a different claim from *very old*.

### Why `scored_at` is separate from the activity's own date

You often score a meeting days after it happened — after the follow-up email
lands, or doesn't. `meeting_date` records when you talked; `scored_at` records
when you formed the judgment.

Keeping the two apart makes calibration analysis possible later. "Was I
overconfident at the recruiter-screen stage?" needs to know what you believed
**at the time you believed it**, which a single conflated date can't tell you.

It is *not*, however, what the rollup orders on, and getting that wrong was a
real bug worth recording. The rollup originally sorted by `scored_at`, falling
back to the activity's own date — but `_apply_score()` stamps `scored_at` every
time it writes a number and is the only writer of `score` anywhere in the app,
so every scored row has one and the fallback could never fire. The series was
therefore ordered entirely by data entry. Log two meetings in one sitting,
oldest typed in last, and the older conversation became the "latest" reading —
while the activity list rendered directly beneath it on the same page sorted by
the real dates and visibly disagreed with it.

It now orders on the date the activity *happened* (`meeting_date`, or
`last_message_at` then `started_at` for a thread), falling back to `scored_at`
only for an activity carrying no date of its own — a fallback that can actually
fire, since those fields are nullable. A score is a judgment *about* an event
and belongs in the sequence where the event sits; when you formed it is a fact
about your evening, not about the pursuit.

### Nothing writes the score automatically — yet

Today you enter the number by hand. That's a deliberate stopping point, not an
unfinished feature: the alternative is shipping interview transcripts to a
third-party API, and those transcripts contain other people who never agreed
to that. Same reasoning as the manual-paste choice for email ingestion.

The three columns are shaped so an automated scorer could populate exactly
these fields later with no schema change and no backfill — `score_reason` is
where a model's rationale would go, the same place yours does now. And because
every scored activity hangs off an application that eventually reaches a
terminal stage, the readings accumulate into labeled training data as a side
effect of ordinary use: a judgment, its rationale, when it was made, and how
it actually turned out.

### The second opinion lives outside the app, on purpose

`skills/application-viability/` is a Claude Skill — a folder of instructions,
not code, and not wired into anything the server runs. Invoked in a chat
session, it reads a transcript, an email thread, or an application's context
and returns an independent score plus a one-line rationale, shaped to be
transcribed into the same two fields you'd fill in by hand.

That boundary is the whole design. The privacy objection above is about the
*app* silently shipping every imported Granola note to a third-party API as a
routine side effect of ordinary use. It is not an objection to ever forming a
model-assisted view of a conversation. Moving that step into a session the
user explicitly starts, with material they explicitly hand over, keeps the
deployed product free of any LLM dependency — there is still no `anthropic` or
`openai` package in `requirements.txt` — while leaving the useful part
available. The app stays a system of record; the judgment happens somewhere
you can see it happening.

The skill is also written to be *blinded* to whatever score is already on the
record. A second opinion that has read your first one isn't one.

### Where the 0–100 range is actually enforced

At the application layer only — `_parse_score()` clamps to 0–100 and treats
non-numeric input as unscored. There's no CHECK constraint behind the column,
because `ensure_schema()` only ever issues `ALTER TABLE ADD COLUMN` and has no
way to add a constraint to an existing table (SQLite would need a full table
rebuild). Guarding at the single door that writes the column keeps the
invariant real without inventing a migration path this project doesn't have.
`tests/test_scoring.py` is what holds that guarantee down.

One distinction the code takes care with throughout: **blank is not zero.**
Blank means no judgment has been formed; `0` means you think it's dead. Both
are falsy in Python, so every check uses `is not None` rather than
truthiness — otherwise a "this is over" score would silently disappear from
the timeline and the rollup.

## Forecast

Two numbers now sit on every Application, and they are allowed to disagree.

**Manual Forecast** is a picklist — `Pipeline`, `Best Case`, `Commit`,
`Closed` — that only you write. Nothing in the app touches that column.
**Automated Forecast** is derived on every page render from four inputs and
stored nowhere at all. Where they diverge is the whole point of having both;
the board draws a small `≠ Best Case` badge next to any card where your call
and the arithmetic part ways, because that gap is a question worth a minute of
your attention and a matching pair is not.

### Why the automated one is never stored

Same argument as the score rollup, one step further along. A stored forecast
is a snapshot of a moment, and the moment moves — a new meeting lands, a stage
advances, a resume gets swapped — while the stored value sits there looking
exactly as authoritative as it did the day it was written. A stale forecast
and a fresh one are visually identical, which makes the stored version worse
than no version: it is confidently wrong at no cost to itself.

Recomputing costs four small reads that the board is already doing anyway
(`selectinload` on meetings, threads, resume, and posting), and buys the
guarantee that what you're looking at describes the record as it stands right
now. The one thing this design gives up is history — you cannot ask "what did
the model think three weeks ago?" If that question ever matters, the answer is
a separate append-only snapshot table, not a mutable column on the
Application.

### The four inputs and their weights

`W_STAGE = 30`, `W_MEETINGS = 35`, `W_FIT = 20`, `W_SOURCE = 15`. They sum to
100 deliberately, so the total reads as a rough percentage and your own
definition — "Commit: more likely than not, 75% or higher" — means what you
said it means when it becomes `COMMIT_AT = 75`. `BEST_CASE_AT = 40` is where
"possible if a few things break our way" stops being defensible and the honest
answer becomes "I don't know yet."

Meetings carry the most weight because they are the only input that is
actually about *this* pursuit going well, rather than about the conditions it
started under. Source carries the least because it is fixed at birth and never
learns anything.

### Stage is an input, not a cap

You chose this explicitly, and it has a consequence worth naming: a
Discovery-stage referral with excellent meetings and strong fit reaches 82 and
reads **Commit**, while a Negotiation-stage outbound application with no
meeting ratings reads **Pipeline**. Late stage does not floor the forecast and
early stage does not ceiling it.

The counter-argument is real. "More likely than not to end in an accepted
offer" is a claim about the entire remaining path, and no amount of first-call
warmth should let a Qualification-stage record make it. That objection is
answered by making `STAGE_POINTS` rise steeply rather than linearly —
Qualification is worth 4 of 30, Discovery 12, Takehome 19 — so an early
pursuit has to be genuinely excellent on everything else to reach Commit,
rather than merely good.

### What the fit index actually measures, and what it doesn't

`fit_index()` is a bag-of-words cosine between the resume text and the job
description: tokenize, drop stopwords, compare the two term-frequency vectors.
It measures whether the two documents talk about the same things. It does not
measure whether you can do the job, and it is trivially gamed by pasting the
JD into your resume.

That honesty is why it carries the smallest of the four weights, why the raw
number is never displayed, and why `fit_band()` collapses it to `Low` /
`Moderate` / `Strong`. A user reading "0.27" next to the word "fit" will read
it as 27% fit, which is not what it means and not a claim this arithmetic can
support. Three buckets is about as much resolution as word overlap honestly
carries.

The floor and ceiling (`FIT_FLOOR = 0.08`, `FIT_CEILING = 0.30`) exist because
the raw cosine's useful range is narrow and low. Two unrelated professional
documents still share enough English to score above zero, and two documents in
the same field rarely exceed 0.35. Points scale linearly between those bounds
and clamp at both ends.

### Two meeting columns, not one quality number

`my_performance` and `employer_engagement` are separate 0–100 fields on
`Meeting` because they answer different questions and diverge in the way that
matters most. A strong performance met with flat engagement usually means the
role is going somewhere else regardless of how you did; a weak performance met
with high engagement means you have room you didn't think you had. Blending
them at input time would erase exactly that signal.

They are blended at *read* time, weighted `ENGAGEMENT_WEIGHT = 0.6` against
`PERFORMANCE_WEIGHT = 0.4`, because their interest in you predicts the outcome
better than your opinion of yourself does. Quality is read off the most recent
scored meeting only — an early bad call that has since been superseded is
history, not forecast — with a small depth bonus (`DEPTH_POINTS_PER_MEETING =
2`, capped at `MAX_DEPTH_POINTS = 7`) for having got several meetings deep at
all.

These two fields deliberately carry no `*_at` stamp, unlike `score`.
`scored_at` exists because the rollup has to order readings against each other
over time; these are read only off the latest meeting, which is already
ordered by `meeting_date`. A timestamp nobody reads is a column that can only
rot.

The blank-is-not-zero rule applies here as it does to `score`, and applies
harder: a call where you didn't rate your own performance and a call you rated
0 are opposite claims, and the model reads them as such.

### Confidence rides alongside the category

`confidence` counts how many of the four components had real data behind them
and is reported *separately* from the band, never folded into it. A Pipeline
that means "I have nothing to go on" and a Pipeline that means "I have plenty
to go on and it's bad" are the same category and completely different
situations. Values are `none` (nothing), `thin` (one or two components), and
`ok` (three or four); `high` is reserved for the two closed short-circuits,
where there is nothing left to be uncertain about.

Stage counts toward confidence only once the pursuit is past Qualification.
Every application is *born* at Qualification (`models.DEFAULT_STAGE`) — that
is a fact about a column default, not something anybody learned — and counting
it would let a brand-new empty record claim it had evidence.

### Closed stages short-circuit

`Closed Won` returns Commit at 100 and `Closed Lost` returns Pipeline at 0,
both without running the arithmetic. A won application is not forecast, it is
finished, and scoring it would produce the absurdity of a signed offer reading
Best Case because nobody got around to filling in the meeting fields.

### Why Manual Forecast defaults to Pipeline

Pipeline literally means "no signal," so a record born there is making a true
statement about itself rather than a placeholder one. The cost is that
"deliberately judged Pipeline" and "never touched" look identical. If that
distinction starts mattering, the fix is a `manual_forecast_set_at` column,
not a nullable default — but it hasn't mattered yet and a column added
speculatively is a column that gets ignored.

Clearing the field in the UI writes NULL rather than snapping back to
Pipeline. New rows are born Pipeline because that's honest; writing Pipeline
back over a field you just deliberately emptied would be the app overruling
you, which is precisely what a hand-maintained field must never do.

One deployment note: `ensure_schema()` adds `manual_forecast` to the live
database with `ALTER TABLE ADD COLUMN`, and SQLAlchemy's `default=` is a
Python-side INSERT default, not DDL. Every application already in the database
therefore gets NULL, not Pipeline, on the first deploy. Both templates handle
that branch and `tests/render_check.py` covers it explicitly, because the
first page load after a migration is exactly where a missing null check turns
into a 500.

### `app/forecast.py` imports nothing from the app

It is stdlib-only on purpose — no SQLAlchemy, nothing from `app.models`. The
routers translate ORM objects into plain dicts and strings at the boundary and
hand those in.

The payoff is `tests/test_forecast.py`, which is the first test file in this
project that imports and exercises the **real** code rather than a
hand-maintained mirror of it. Every other test file in `tests/` reimplements
the logic it checks, which means it can pass while the app is broken; this one
cannot. The one thing that arrangement gives up is compile-time agreement
between `forecast.py`'s category and stage literals and the enums in
`models.py` — bought back by three tests that read `app/models.py` as *text*
and regex out the enum members.

## Context vs. notes on an Application

`JobApplication` carries two free-text fields, and the split is intentional:

- **`context`** is durable input you'd re-read when judging whether this one
  is worth continuing to spend effort on — why the role is interesting, what
  you know about the team, comp, and timeline, what would make you walk.
- **`notes`** stays a running scratchpad of whatever happened lately.

Merging them would be simpler, and worse. The point of `context` is that a
viability assessment (yours, or an automated one later) has a stable thing to
read instead of having to sift a chronological log for the handful of durable
facts buried in it. Chronological notes are append-mostly and grow without
bound; context is edited in place and stays roughly the same size.

## Where an application came from

`source` is a nullable picklist — **Referral**, **Recruiter Inbound**, or
**Outbound** — about the *origin* of the application, not about who a person
is. A referral from a friend and an inbound note from that friend's in-house
recruiter are different sources even when both eventually route through the
same recruiter, because they convert differently and you can act on them
differently.

It's nullable and defaults to nothing, deliberately. Applications created
before the field existed genuinely don't know their own answer, and defaulting
them to "Outbound" would put fabricated data into the exact analysis the field
exists to support ("which source actually converts?"). An honest blank is
worth more than a confident guess.

## Ingestion & dedup (roadmap)

Job postings come from **Firecrawl** (search + scrape + schema-based structured
extraction). Firecrawl does **not** filter or dedup for us, so we own both:

**Keyword filtering, two layers**
1. *At the source* — targeted Firecrawl `search` queries (role keywords, and
   site-scoped queries for the ATS boards you care about) to cut volume before
   anything is scraped.
2. *After extraction, in our code* — an include/exclude keyword ruleset we own,
   applied before a posting is written. This is also the natural seam where the
   thumbs up/down feedback later upgrades a static filter into a learned one.

**Deduplication, three fallback layers (cheapest first)**
1. *Canonical ID from the URL.* Most ATS platforms (Greenhouse, Lever, Ashby,
   Workday) embed a stable job ID in the posting URL. Parse it into `dedup_key`
   and index it — an exact match is the same posting.
2. *Fuzzy fallback* when there's no parseable ID: same company + normalized title
   + similar location within a rolling window.
3. *Semantic fallback:* embed the JD text and compare against recent postings from
   the same company; near-duplicates merge instead of creating a new row. These
   same embeddings double as the feature representation for the "pattern-match on
   what I'm looking for" goal — so this layer isn't wasted even for non-duplicates.

On a match we don't create a new row; we bump `last_seen_at` on the existing
posting and append the new URL to `source_urls`. Phase 1 ships layers 1–2
(deterministic, no ML dependency); the embedding layer is added once there's real
rating data to make it worthwhile.

## Portfolio note

This project is public as an example of AI-native product thinking: the object
model above was designed collaboratively and deliberately, trading off
normalization, reporting needs, and real-world edge cases (agency recruiters,
cold outreach, reposted roles) before any code was written.
