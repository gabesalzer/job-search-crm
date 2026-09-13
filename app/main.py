"""FastAPI application entry point."""
from __future__ import annotations

import base64
import os
import secrets

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .database import (
    Base,
    engine,
    ensure_schema,
    migrate_email_thread_people,
    migrate_lost_reason,
    migrate_stage_names,
)
from .routers import (
    analytics,
    applications,
    companies,
    granola,
    log,
    people,
    postings,
    resumes,
    ui,
)

# Create any missing tables, then add any missing columns to existing tables
# (a lightweight auto-migration so schema changes don't drop your data).
Base.metadata.create_all(bind=engine)
ensure_schema()
migrate_stage_names()  # one-time remap to the July 2026 macro stage model
migrate_email_thread_people()  # one-time move off EmailThread's old single person_id
migrate_lost_reason()  # one-time move off the retired LostReason enum

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
        if not self._authorised(request, username, password):
            return Response(
                "Authentication required.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Job Search CRM"'},
            )
        # Deliberately outside the credential check's try/except. It used to be
        # inside it, which meant `except Exception: pass` swallowed anything the
        # route raised and fell through to the 401 below -- so every server
        # error in the app presented as a login prompt that then "rejected"
        # correct credentials, because re-submitting them just re-ran the
        # failing route. It hid a NameError in posting creation for days and
        # would have hidden the next one too. A crash must look like a crash.
        return await call_next(request)

    @staticmethod
    def _authorised(request: Request, username: str, password: str) -> bool:
        """Whether this request carries the right Basic credentials.

        The broad `except` belongs here and only here: a malformed or
        non-UTF-8 Authorization header is a failed login, not a server error.
        Keeping it wrapped around nothing else is what stops it catching
        exceptions it has no business catching.
        """
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001 -- a bad header is a failed login
            return False
        user, _, pw = decoded.partition(":")
        return (secrets.compare_digest(user, username)
                and secrets.compare_digest(pw, password))


app.add_middleware(BasicAuthMiddleware)

# JSON API routers (mounted under /api/*)
app.include_router(companies.router)
app.include_router(postings.router)
app.include_router(applications.router)
app.include_router(people.router)
app.include_router(resumes.router)
app.include_router(analytics.router)
app.include_router(granola.router)
app.include_router(log.router)

# Server-rendered UI (/, /board, /postings, /companies, and /ui/* form handlers)
app.include_router(ui.router)


@app.get("/health")
def health():
    return {"status": "ok"}
