"""The server-rendered UI (Jinja2), split by domain.

A thin presentation layer over the exact same models and database the JSON API
uses. Form posts here just create or update rows and redirect back to the page;
the drag-to-change-stage on the board calls the JSON API directly.

This package replaces a single 3,278-line `ui.py`. Every task -- however small -- used to
begin by reading all of it, which is a cost in a model's context window and in
yours; this is the change that makes a cheaper model viable on this codebase.

The split has to be invisible from outside. `app/main.py` imports `ui.router`,
`app/routers/log.py` imports `_propose_from_note`, and the tests reach for
`_chat_corpus`, `_criteria`, `_forecast_for` and others straight off this
package. All of that still works, and the test suite is what proves the move
changed no behaviour.

Every path registered below is a distinct literal or prefix, so sub-router
order is for readability rather than routing.
"""
from fastapi import APIRouter

from . import (  # noqa: F401
    applications,
    companies,
    insights,
    log,
    meetings,
    people,
    postings,
    resumes,
    settings,
    shared,
    threads,
)

# Re-export every submodule's namespace onto the package, so this package *is*
# the old module's namespace.
#
# The alternative was a hand-written list of the forty-odd names other modules
# and tests import from here, which would go stale the first time a helper
# moved between domains -- and go stale silently, since the failure is an
# ImportError in a test file nobody edited. Copying references keeps the
# contract exact and self-maintaining.
#
# Safe because nothing rebinds a name on this package: the tests stub
# `ui.llm.generate`, which mutates the shared `llm` module object itself and is
# therefore seen by every submodule regardless of how it was imported.
_MODULES = (shared, applications, postings, companies, resumes, meetings,
            people, threads, insights, settings, log)
for _m in _MODULES:
    for _name, _value in vars(_m).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

# Deliberately last: each submodule declares a `router` of its own, and the one
# `app/main.py` mounts is the aggregate, not whichever submodule was copied in
# first.
router = APIRouter()
for _module in _MODULES[1:]:
    router.include_router(_module.router)
