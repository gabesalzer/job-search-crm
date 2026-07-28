# Job Search CRM

A personal CRM for running a job search like a revenue pipeline. It captures job
postings (including a paste-a-URL scraper), lets you rate them so the system
learns what you're looking for, and tracks each application through stages the
way a CRM tracks an opportunity — so you can see *where* and *why* you fall off,
which resume versions get traction, and who your champions are.

The data model is deliberately built on a Salesforce analogy:

| This app        | Salesforce analogy |
| --------------- | ------------------ |
| Company         | Account            |
| Job Application | Opportunity        |
| Person          | Contact            |
| Job Posting     | Product            |
| Stage History   | Opportunity field history |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full data model and the design
reasoning behind every relationship.

## Stack

- **FastAPI** + **SQLAlchemy** + **SQLite** — a real relational backend, single-user,
  zero external database to run.
- Server-rendered UI (Jinja2): a kanban **Pipeline** board, a **Postings** triage
  page, and a **Companies** view — plus a JSON API under `/api` and interactive
  docs at `/docs`.
- A job-posting **scraper**: paste a URL and it fills the fields. Greenhouse and
  Lever are read from their public APIs; other pages via schema.org JobPosting
  data; JS-heavy sites (LinkedIn/Indeed/Workday) work if a Firecrawl key is set.

## Quickstart

First-time setup, from the project folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 for the app and http://127.0.0.1:8000/docs for
the interactive API.

### Running it after setup

The steps above are one-time. After that, restarting is just two lines from the
project folder (wherever you keep it) — the app uses relative paths, so its
location doesn't matter:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

Press `Ctrl+C` in that terminal to stop the server.

### Optional: Firecrawl

A Firecrawl key is only needed to scrape JavaScript-heavy boards (LinkedIn,
Indeed, Workday). Company career pages and Greenhouse/Lever/Ashby work without
one. To enable it, put `FIRECRAWL_API_KEY=fc-...` in `.env` and restart.

## Usage

- **Postings** → *New posting*: paste a job URL and hit *Fetch details*, or fill
  it in by hand. The company (Account) is created automatically if it doesn't
  exist yet. Rate postings ▲ / — / ▼ to record what you're looking for.
- **Pipeline**: *New application* → optionally pick a posting to link it and
  auto-fill company + title. Drag cards between stage columns; every move is
  written to stage history. Each application also carries a **Source**
  (Referral / Recruiter Inbound / Outbound — how it originated) and a
  **Context** field: standing notes on why the role is worth pursuing, what
  you know about team, comp, and timeline, and what would make you walk.
  Context is deliberately separate from Notes — Context is durable input
  you'd re-read when judging whether to keep spending effort here, while
  Notes stays a running log of what happened lately.
- **Companies**: employers and staffing agencies, typed so you can later report
  on which employers vs. which agencies get you traction.
- **Meetings**: interviews and calls, attached to an application. Capture the
  summary and transcript by hand, or (with `GRANOLA_API_KEY` set) load and
  import a note from Granola. Every application's edit page shows its
  meetings as a related list, and a meeting's edit page shows which
  application (and, through it, which JD and resume) it belongs to.
- **People**: recruiters, hiring managers, interviewers, referrals. A
  person's company is their *own* employer — independent of whichever
  application they're optionally tied to, so an agency recruiter's company
  is the agency, not the employer you're interviewing at. Mark someone a
  *Champion* to flag they're rooting for you.
- **Emails**: recruiter/hiring-manager email threads, tied to any number of
  People (a thread with a recruiter and a looped-in hiring manager links
  both) and optionally to an Application (unset for pre-application
  outreach, e.g. a cold recruiter message). Paste the thread text in, or
  upload a file — a PDF export of the thread (Gmail's own "Print all") works
  especially well: its text is extracted the same way a resume upload is,
  and if it's shaped like a Gmail export, the subject, participants, and
  thread start/last-message dates are pulled out and pre-filled
  automatically (anything you type by hand always wins over the auto-filled
  value). You don't need to create the People first, either — leave the
  checklist blank and everyone on the other side of the conversation is
  found-or-created by email address (the dedup key: the same address always
  resolves to the same Person, and an auto-created one also gets a Company
  inferred from their email domain, reusing an existing company if one
  already matches). An application's edit page shows an **Activity** related
  list that merges its meetings and email threads into one chronological
  timeline.
- **Scoring**: every meeting and email thread can carry a **win-likelihood
  score** — 0–100, your best guess at how likely that application is to end
  up Closed Won given what just happened — plus a one-line reason. The score
  lives on the interaction rather than on the application on purpose: a
  single number on the application would only ever tell you where you stand
  now, while a reading per interaction turns the same judgments into a trend
  you can watch (80 → 55 → 30 after an interview that went badly). The
  application's edit page rolls these up at the top of its Activity list as
  a current number plus the change from the previous reading, and each row
  in the timeline shows its own score. Leave it blank when you haven't
  formed a view — blank means unscored, and `0` means you think it's dead;
  they're different answers, and the app keeps them apart. Nothing fills
  these in automatically today: you enter the number, and the fields are
  shaped so an automated scorer could populate exactly the same ones later.
- **Forecast**: every application carries two forecasts, side by side and
  deliberately independent. **Manual Forecast** is a picklist — Pipeline,
  Best Case, Commit, Closed — that only you write; nothing in the app ever
  overwrites it. **Automated Forecast** derives the same three categories
  from four inputs: how far the pursuit has got (stage), the quality of the
  most recent scored meeting, how closely the resume and the job description
  overlap, and where the application came from (a referral is worth a great
  deal more than an outbound application). The weights sum to 100, so the
  total reads as a rough percentage and *Commit* means what it says: more
  likely than not, 75 or above. Meetings feed this through two new 0–100
  fields — **my performance** and **their engagement** — kept separate
  because a strong performance met with flat engagement means something very
  different from the reverse. The automated read is computed fresh on every
  page load and stored nowhere, so it can never be quietly stale, and it
  reports its own confidence alongside the category, because "Pipeline, I
  have nothing to go on" and "Pipeline, I have plenty to go on and it's bad"
  are the same word and different situations. The board flags any card where
  your call and the arithmetic disagree; the edit page shows the full
  four-part breakdown behind the number.
- **A second opinion**: `skills/application-viability/` is a Claude Skill —
  instructions, not code — that reads a transcript, an email thread, or an
  application's context and returns its own 0–100 score with a one-line
  reason, in the shape the score fields expect. It's deliberately *not* part
  of the app: it runs in a chat session you start, on material you hand it,
  so the deployed server keeps zero LLM dependencies and no transcript goes
  anywhere as a side effect of ordinary use. It's also written to ignore any
  score you've already recorded — a second opinion that's read your first one
  isn't one. See [`skills/README.md`](./skills/README.md) to install it.
- **Editing**: every record type (companies, postings, resumes, applications,
  meetings, people, email threads) has an *Edit* link that opens a form
  pre-filled with its current values — available both from each list/board
  view and from the edit page itself. For meetings, the edit page also
  carries the Granola import controls, so you can re-pull or switch a note's
  transcript onto an existing meeting instead of deleting and recreating it.
  For postings, the URL field auto-fetches a new listing's details as soon as
  you change it.
- **Deleting**: every record type also has a *Delete* button, guarded by a
  confirm dialog, available from both the list view and the edit page.
  Deleting a company cascades to its postings, applications, and people;
  deleting an application cascades to its stage history and meetings (and
  clears the link on any email thread pointed at it). Deleting a person just
  unlinks them from any email threads they were on — the threads themselves
  survive, even if that leaves one with no one linked (an orphaned thread is
  a valid state you can clean up by hand, not something the app deletes for
  you). Deleting a posting, resume, or application-link on a person/thread
  just unlinks it from anything that referenced it (nothing else is
  deleted). Stage history has no delete of its own — it's an audit trail,
  cleaned up only as a side effect of deleting its parent application.

## Data & privacy

Your real job-search data lives only in `data/jobsearch.db`, which is
**gitignored** — it never leaves your machine. The repo ships with the schema and
code only, no personal data and no seed data. Your Firecrawl key lives in `.env`,
also gitignored.

If you deploy this (e.g. via `render.yaml`), it's reachable on the open
internet by default, so the app password-gates itself: set `APP_PASSWORD` (and
`APP_USERNAME`) as environment variables and every route requires HTTP Basic
Auth before it'll serve a page. Locally, leave `APP_PASSWORD` unset and there's
no login prompt, since only you can reach `localhost`. Basic Auth with one
shared password is minimal protection — enough to keep a deployed personal
tool off of casual/automated access, not a substitute for a real auth system
if this ever needs to hold more than one person's data.

## Status

Implemented: the full object model, stage-history tracking, the web UI, the JSON
API, and the URL scraper (Greenhouse/Lever APIs, JSON-LD, optional Firecrawl),
posting-first company creation, application↔posting links, resume upload with
text extraction, Meetings with optional Granola import, Email Threads with
Gmail-export auto-parsing, a combined Meetings+Emails activity timeline on
each application, and edit forms for every record type.

Planned next: a Firecrawl-powered bulk *search* for postings, the three-layer
dedup pipeline, richer company enrichment, and the funnel / resume-traction
analytics views. See `ARCHITECTURE.md` for the roadmap and reasoning.
