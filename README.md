# Job Search CRM

A personal CRM for running a job search like a revenue pipeline. It ingests job
postings, lets you rate them so the system learns what you're looking for, and
tracks each application through stages the way a CRM tracks an opportunity — so
you can see *where* and *why* you fall off, which resume versions get traction,
and who your champions are.

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
- **Firecrawl** for pulling job postings from the web.
- Server-rendered pages (Jinja2) plus a JSON API.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 for the app and http://127.0.0.1:8000/docs for
the interactive API.

A Firecrawl key is only needed for job-posting ingestion (not yet built), so you
can run the app without it. When you're ready, add your key to `.env`.

## Data & privacy

Your real job-search data lives only in `data/jobsearch.db`, which is
**gitignored** — it never leaves your machine. The repo ships with the schema and
code only, no personal data and no seed data. Your Firecrawl key lives in `.env`,
also gitignored.

## Status

Early build. Implemented: the full object model, stage-history tracking, and
CRUD/rating/stage endpoints. Planned next: Firecrawl ingestion with the
keyword filter, the three-layer dedup pipeline, and the funnel/analytics views.
See `ARCHITECTURE.md` for the roadmap.
