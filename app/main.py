"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI

from .database import Base, engine
from .routers import (
    analytics,
    applications,
    companies,
    people,
    postings,
    resumes,
    ui,
)

# Create tables on startup. For schema migrations later, swap in Alembic.
Base.metadata.create_all(bind=engine)

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
