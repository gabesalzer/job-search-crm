"""The headers every model call sends, and the one that is conditional.

Small file, narrow subject, and it exists because of a real outage. The Brief
worked for weeks, then returned a 400 with no deploy in between: a replacement
`ANTHROPIC_API_KEY` was a personal key rather than the legacy workspace key it
replaced, and the API refuses an identity-linked key that can reach more than
one workspace until the request names which one.

That failure is invisible to every other test in this suite, because they all
stub `llm.generate` and never build a header. So it gets its own.

Run: python3 tests/test_llm_client.py
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from app.services import llm
except ImportError:  # pragma: no cover
    print("SKIP  httpx is not installed; the client cannot be imported here.")
    raise SystemExit(0)

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


def _with(value):
    if value is None:
        os.environ.pop("ANTHROPIC_WORKSPACE_ID", None)
    else:
        os.environ["ANTHROPIC_WORKSPACE_ID"] = value
    return llm._headers()


os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"

base = _with(None)
check("the three required headers are always sent",
      set(base) == {"x-api-key", "anthropic-version", "content-type"})
check("the API version is pinned", base["anthropic-version"] == llm.API_VERSION,
      "it is a dated contract, and it is what guarantees the response shape "
      "the parser expects stays the shape that arrives")
check("no workspace header when none is configured",
      "anthropic-workspace-id" not in base,
      "a legacy workspace key carries its workspace implicitly and sending an "
      "empty one would break a setup that currently works")

withws = _with("wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ")
check("the workspace header is sent when one is configured",
      withws["anthropic-workspace-id"] == "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ")
check("and the other three are untouched",
      all(withws[k] == base[k] for k in base))

check("a blank value is treated as unset, not sent empty",
      "anthropic-workspace-id" not in _with("   "),
      "an env var set to whitespace is the shape a half-finished dashboard "
      "edit leaves behind, and an empty header is worse than no header")

check("the value is trimmed",
      _with("  wrkspc_abc  ")["anthropic-workspace-id"] == "wrkspc_abc")

check("it is read at call time, not captured at import",
      _with("wrkspc_second")["anthropic-workspace-id"] == "wrkspc_second",
      "so setting the variable and restarting is enough")

_with(None)
check("enabled() still keys off the API key alone",
      llm.enabled() is True,
      "the workspace id is optional; requiring it would switch the feature "
      "off for every legacy key that works fine")

print("\n{} failed".format(len(failures)) if failures else "\nall passed")
sys.exit(1 if failures else 0)
