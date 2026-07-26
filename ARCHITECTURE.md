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
 ├── (lookup, nullable) ──> Job Application (the application this person is tied to)
 └── (master-detail) ──> Email Thread       (conversations with this person)

Email Thread
 └── (lookup, nullable) ──> Job Application (unset until the thread is about a role)
```

### Master-detail vs. lookup — what it means here

In Salesforce, **master-detail** means the child cannot exist without the parent:
a required parent, cascade delete, and the parent can roll up aggregates of its
children. **Lookup** means an optional pointer: nullable, no forced cascade, no
automatic rollups.

Because this app is plain SQLAlchemy/SQLite rather than the Salesforce platform,
those concepts map directly to SQL:

- **Master-detail** → `NOT NULL` foreign key with `ON DELETE CASCADE`.
- **Lookup** → nullable foreign key, `ON DELETE SET NULL`.

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
- **Analysis questions the model supports:** which resume versions progress; which
  JD/company types you gain traction with; whether a particular resume moves you
  forward; whether you have a "champion"; and, because Person's company is
  independent, "which recruiters (regardless of agency) get me furthest" as its
  own question separate from "which employers."

## Stages

Ordered pipeline (Opportunity-style), stored as an enum:

`Qualification → Discovery → Takehome → Executive Signoff → Negotiation → Closed Won / Closed Lost`

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
  the specific artifact rather than a generic "Technical," since RevOps
  hiring loops reliably include some form of takehome exercise even when
  they're not coding-specific.
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

## Meetings and Email Threads

Both are activity records, but deliberately **separate objects**, not one
merged "Interaction" table:

- **Meeting** — master-detail to Job Application. A meeting exists because
  you're pursuing a specific role; through the application it also reaches
  the Posting (JD) and Resume, which is what makes "questions asked, by JD /
  by resume" analysis possible. Shape: a single dated event (`meeting_date`)
  with a summary and transcript.
- **Email Thread** — master-detail to **Person**, with only an optional
  lookup to Job Application. A thread is fundamentally a conversation with
  someone, and — like a Person's own company — that conversation can predate
  any application: a cold recruiter email lands in your inbox before you've
  decided to pursue anything. Forcing every thread to have an application
  would make cold outreach impossible to log until after the fact. Shape:
  a subject, a body, and a message span (`started_at` → `last_message_at`),
  not a single instant.

Different masters, different required fields, different natural lifetimes —
merging them would mean either making Application nullable on both (losing
the not-null guarantee Meeting relies on) or forcing every early-stage email
to fabricate an application it doesn't have yet. Two objects, cleanly typed,
avoids both.

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

### Auto-creating People from a thread's senders

`EmailThread.person_id` is required — a thread is always *with* someone —
but you don't have to create that Person by hand first. The parser also
extracts every real message **sender** (not just any email mentioned in the
text) from the Gmail-shaped headers, and identifies which one is *you* from
the account-owner banner line Gmail always prints at the top of an export.
Whatever's left — the other side of the conversation — gets found-or-created
as a Person automatically.

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
Person — useful on its own, since your People list fills in as a side
effect of just logging threads — but the thread's own `person_id` only ever
points at the *first* sender, in message order, since the schema is one
thread → one person. An explicit pick in the Person dropdown always
overrides auto-detection, same override rule as every other auto-filled
field on this form. If the text isn't Gmail-shaped and you didn't pick a
Person by hand, thread creation fails with a clear error rather than
guessing — see `_resolve_thread_person()` in `app/routers/ui.py`. This whole
path is proven against representative cases (case-insensitive matching, a
brand-new email, a second person at a known company, two threads landing on
two different companies) in `tests/test_person_from_email.py`.

### One timeline, two objects

An Application's edit page shows an **Activity** related list that merges
its Meetings and Email Threads into one chronologically-sorted view, rather
than two separate lists you'd have to mentally interleave. This is a
display-layer merge only — no new table, no schema change: the route
handler (`_activity_timeline()` in `app/routers/ui.py`) fetches both
relationships, normalizes each row into a common `{type, when, title, sub,
url}` shape, and sorts by whichever timestamp best represents "most recent
activity" for that row type (`meeting_date` for a Meeting, `last_message_at`
for a thread, so a thread with a fresh reply surfaces near the top rather
than staying pinned at when it started). Salesforce's own Activity Timeline
works the same way under the hood — Tasks and Events are different objects
with different fields, merged and sorted at the UI layer, not in the schema.

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
