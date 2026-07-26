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
  written to stage history.
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
- **Emails**: recruiter/hiring-manager email threads, tied to a Person and
  optionally to an Application (unset for pre-application outreach, e.g. a
  cold recruiter message). Paste the thread text in, or upload a file — a
  PDF export of the thread (Gmail's own "Print all") works especially well:
  its text is extracted the same way a resume upload is, and if it's
  shaped like a Gmail export, the subject, participants, and thread
  start/last-message dates are pulled out and pre-filled automatically
  (anything you type by hand always wins over the auto-filled value). An
  application's edit page shows an **Activity** related list that merges its
  meetings and email threads into one chronological timeline.
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
  clears the link on any email thread pointed at it); deleting a person
  cascades to their email threads. Deleting a posting, resume, or
  application-link on a person/thread just unlinks it from anything that
  referenced it (nothing else is deleted). Stage history has no delete of
  its own — it's an audit trail, cleaned up only as a side effect of
  deleting its parent application.

## Data & privacy

Your real job-search data lives only in `data/jobsearch.db`, which is
**gitignored** — it never leaves your machine. The repo ships with the schema and
code only, no personal data and no seed data. Your Firecrawl key lives in `.env`,
also gitignored.

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
