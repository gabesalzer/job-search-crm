"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI

from .database import Base, engine, ensure_schema
from .routers import (
    analytics,
    applications,
    companies,
    people,
    postings,
    resumes,
    ui,
)

# Create any missing tables, then add any missing columns to existing tables
# (a lightweight auto-migration so schema changes don't drop your data).
Base.metadata.create_all(bind=engine)
ensure_schema()

app = FastAPI(
    title="Job Search CRM",
    description="Run a job search like a revenue pipeline. See ARCHITECTURE.md.",
    version="0.1.0",
)

# JSON API routers (mounted under /api/*)
app.include_router(companies.router)
app.include_router(postings.router)
app.include_router(applications.router)
app.include_router(people.router)
app.include_router(resumes.router)
app.include_router(analytics.router)

# Server-rendered UI (/, /board, /postings, /companies, and /ui/* form handlers)
app.include_router(ui.router)


@app.get("/health")
def health():
    return {"status": "ok"}
