"""FastAPI application entry point."""
from __future__ import annotations

import base64
import os
import secrets

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .database import Base, engine, ensure_schema
from .routers import (
    analytics,
    applications,
    companies,
    granola,
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


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Password-gate the whole app when APP_PASSWORD is set.

    Uses HTTP Basic Auth (safe over the HTTPS your host provides). If
    APP_PASSWORD is empty — e.g. running locally — auth is disabled so you don't
    need a password on your own machine. In the cloud, set APP_USERNAME and
    APP_PASSWORD as environment variables and every route requires them.
    """

    # Paths reachable without a password: the health check the host pings, and
    # the JSON health endpoint. Everything else is gated.
    OPEN_PATHS = {"/health"}

    async def dispatch(self, request: Request, call_next):
        password = os.getenv("APP_PASSWORD", "")
        if not password:
            return await call_next(request)  # auth disabled (local dev)
        if request.url.path in self.OPEN_PATHS:
            return await call_next(request)  # e.g. Render's /health probe
        username = os.getenv("APP_USERNAME", "gabe")
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pw = decoded.partition(":")
                if secrets.compare_digest(user, username) and secrets.compare_digest(pw, password):
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            "Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Job Search CRM"'},
        )


app.add_middleware(BasicAuthMiddleware)

# JSON API routers (mounted under /api/*)
app.include_router(companies.router)
app.include_router(postings.router)
app.include_router(applications.router)
app.include_router(people.router)
app.include_router(resumes.router)
app.include_router(analytics.router)
app.include_router(granola.router)

# Server-rendered UI (/, /board, /postings, /companies, and /ui/* form handlers)
app.include_router(ui.router)


@app.get("/health")
def health():
    return {"status": "ok"}
