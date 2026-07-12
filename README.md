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

## Data & privacy

Your real job-search data lives only in `data/jobsearch.db`, which is
**gitignored** — it never leaves your machine. The repo ships with the schema and
code only, no personal data and no seed data. Your Firecrawl key lives in `.env`,
also gitignored.

## Status

Implemented: the full object model, stage-history tracking, the web UI, the JSON
API, and the URL scraper (Greenhouse/Lever APIs, JSON-LD, optional Firecrawl),
plus posting-first company creation and application↔posting links.

Planned next: a Firecrawl-powered bulk *search* for postings, the three-layer
dedup pipeline, richer company enrichment, and the funnel / resume-traction
analytics views. See `ARCHITECTURE.md` for the roadmap and reasoning.
