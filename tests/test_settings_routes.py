"""The Settings page: the override lifecycle, and the fold of Looking For.

The interesting assertions are the ones about the *edge* of the override
mechanism, because "store the text you typed" is not where this goes wrong.
Clearing a box, retyping the shipped wording by hand, and editing a field the
Log cannot write all have to do something defensible, and each of them is a
way the page could quietly stop matching what the model is actually told.

Run: python3 tests/test_settings_routes.py
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-settings-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from app import fields, logspec, models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import ui as ui_module  # noqa: E402

client = TestClient(app)

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


def _overrides():
    with SessionLocal() as db:
        return {r.field: r.definition
                for r in db.query(models.FieldDefinition).all()}


def _save(field, text):
    return client.post("/ui/settings/definitions/{}".format(field),
                       data={"definition": text}, follow_redirects=False)


def _prompt_now():
    """The prompt the Log would send right now, overrides and all."""
    with SessionLocal() as db:
        return logspec.system_prompt(
            stages=["Discovery"], categories=["Other"], today="2026-09-06",
            definitions=ui_module._definition_overrides(db))


# --------------------------------------------------------------------------- #
# The page, and both sections on it
# --------------------------------------------------------------------------- #
page = client.get("/settings")
check("the settings page renders", page.status_code == 200)
check("it has a Field definitions section", "Field definitions" in page.text)
check("it has a What I'm looking for section",
      "looking for" in page.text.lower())
check("both sections are anchorable",
      'id="definitions"' in page.text and 'id="looking-for"' in page.text)
check("every catalogued field appears",
      all(row["label"] in page.text for row in fields.CATALOGUE))
check("the fields the model actually reads are marked as such",
      "read by the Log" in page.text)
check("a field the Log cannot write is still listed, because why it is "
      "excluded is the interesting half", "Champion inside" in page.text)
check("the folded page brought its axes with it", "The axes" in page.text)
check("...and its ranking table", "How the pipeline scores" in page.text)
check("Settings is in the nav", 'href="/settings"' in client.get("/board").text)
check("Looking for is gone from the nav",
      'href="/looking-for"' not in client.get("/board").text)

r = client.get("/looking-for", follow_redirects=False)
check("the old URL redirects rather than 404s", r.status_code == 301)
check("...to the right section", r.headers["location"] == "/settings#looking-for")


# --------------------------------------------------------------------------- #
# The override lifecycle
# --------------------------------------------------------------------------- #
check("nothing is stored before you edit anything", _overrides() == {})

_save("pain", "Only what the employer is trying to fix.")
check("an edit is stored",
      _overrides().get("pain") == "Only what the employer is trying to fix.")

page = client.get("/settings")
check("the page shows your wording",
      "Only what the employer is trying to fix." in page.text)
check("...marked as yours", "yours" in page.text)
check("...alongside what it replaced, so you can see what you changed from",
      "Shipped wording:" in page.text)

# The whole point of the feature. A settings page whose edits do not reach the
# model is worse than none: it invites you to tune something that is not
# listening, and you would only find out by noticing nothing ever changed.
check("an edited definition reaches the prompt the Log actually sends",
      "Only what the employer is trying to fix." in _prompt_now())

client.post("/ui/settings/definitions/pain/reset", follow_redirects=False)
check("reset removes the row rather than storing the default back",
      "pain" not in _overrides())
check("...and the shipped wording comes back on the page",
      fields.BY_FIELD["pain"]["definition"][:40] in client.get("/settings").text)
check("...and in the prompt",
      fields.BY_FIELD["pain"]["definition"][:40] in _prompt_now())

_save("risks", "  ")
check("saving a blank box clears the override instead of storing an empty one",
      "risks" not in _overrides())
check("...so a field's meaning cannot be deleted by emptying a textarea",
      fields.BY_FIELD["risks"]["definition"][:40] in _prompt_now())

_save("risks", fields.BY_FIELD["risks"]["definition"])
check("retyping the shipped wording exactly is treated as a reset, so a row "
      "cannot silently pin today's wording and stop tracking a better one",
      "risks" not in _overrides())

_save("notes", "Mine.")
_save("notes", "Mine, revised.")
check("editing twice updates one row rather than adding another",
      _overrides() == {"notes": "Mine, revised."})
client.post("/ui/settings/definitions/notes/reset", follow_redirects=False)

# A field the Log cannot write is editable as documentation, and the page has
# already said the edit is inert for the model. Storing it is right; letting it
# into the prompt is not.
_save("champion", "My own note about champions.")
check("a non-writable field's definition can still be edited",
      _overrides().get("champion") == "My own note about champions.")
check("...but it never reaches the prompt",
      "My own note about champions." not in _prompt_now())
client.post("/ui/settings/definitions/champion/reset", follow_redirects=False)

check("an unknown field is refused rather than stored",
      _save("not_a_field", "x").status_code == 404)
check("...and resetting one is too",
      client.post("/ui/settings/definitions/not_a_field/reset",
                  follow_redirects=False).status_code == 404)
check("nothing was stored for it", _overrides() == {})


# --------------------------------------------------------------------------- #
# The folded Looking For half still works
# --------------------------------------------------------------------------- #
r = client.post("/ui/looking-for",
                data={"statement": "Systems and strategy, Series B or later.",
                      "dq_threshold": "5"}, follow_redirects=False)
check("saving the statement lands back on Settings",
      r.headers["location"] == "/settings#looking-for")
page = client.get("/settings")
check("...and the statement is on the page",
      "Systems and strategy, Series B or later." in page.text)

# Deliberately not "Manager quality": that phrase is the placeholder text in
# the add-an-axis form, so a substring search for it passes whether or not the
# axis exists. Assertions on rendered pages need a needle the page cannot
# supply by itself.
AXIS = "Commute tolerance"

r = client.post("/ui/looking-for/criteria",
                data={"name": AXIS, "description": "A 10 is..."},
                follow_redirects=False)
check("adding an axis lands back on Settings",
      r.headers["location"] == "/settings#looking-for")
check("...and the axis is on the page", AXIS in client.get("/settings").text)

with SessionLocal() as db:
    cid = (db.query(models.Criterion)
           .filter(models.Criterion.name == AXIS).first().id)
client.post("/ui/looking-for/criteria/{}/delete".format(cid),
            follow_redirects=False)
with SessionLocal() as db:
    check("deleting an axis removes the row", db.get(models.Criterion, cid) is None)
check("...and it leaves the page", AXIS not in client.get("/settings").text)


print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
