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
 └── (lookup, nullable) ──> Job Application (the application this person is tied to)
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
  snapshot, which lets you compute stage-to-stage conversion (e.g. "recruiter
  screen → onsite"). Pair `stage` with a `lost_reason` picklist (ghosted, rejected
  after screen, rejected after onsite, declined by me, …) — that's the field that
  answers *where and why* you fall off.
- **Analysis questions the model supports:** which resume versions progress; which
  JD/company types you gain traction with; whether a particular resume moves you
  forward; whether you have a "champion"; and, because Person's company is
  independent, "which recruiters (regardless of agency) get me furthest" as its
  own question separate from "which employers."

## Stages

Ordered pipeline (Opportunity-style), stored as an enum:

`Saved → Applied → Recruiter Screen → Hiring Manager Screen → Onsite / Technical → Offer → Closed Won / Closed Lost`

Every time `stage` changes, a Stage History row is written automatically
(`from_stage`, `to_stage`, `changed_at`) via a SQLAlchemy attribute event
listener, so the funnel is captured without relying on the caller to remember.

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
