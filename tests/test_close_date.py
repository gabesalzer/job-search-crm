"""The expected close date, its slip log, and what past-due means.

Two things are worth pinning here and neither is arithmetic.

The first is that the history is written by an *attribute listener*, not by the
route. That is what makes the log complete: the date can be set from the edit
form, from an approved voice note or from a script, and none of them has to
remember to append a row. A test that only drove the form would pass just as
well against route code that logged it by hand, and would go quiet the day a
second writer appeared — which is exactly what the Log feature is.

The second is that `overdue` is a claim about an *open* pursuit. A Closed Lost
application that ran past its date is not something to chase, and colouring it
red would be nagging about the past.

Run: python3 tests/test_close_date.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-close-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


def _seed():
    with SessionLocal() as db:
        if db.query(models.JobApplication).first():
            return
        co = models.Company(name="Condor Close")
        db.add(co)
        db.flush()
        for title in ("Head of RevOps", "RevOps Manager", "Old role"):
            db.add(models.JobApplication(company_id=co.id, title=title))
        db.commit()


_seed()

with SessionLocal() as db:
    IDS = [a.id for a in db.query(models.JobApplication)
           .order_by(models.JobApplication.id).all()]
MOVER, PLAIN, CLOSED = IDS[0], IDS[1], IDS[2]


def _set(app_id, when):
    """Set the date directly on the ORM, bypassing the route entirely."""
    with SessionLocal() as db:
        obj = db.get(models.JobApplication, app_id)
        obj.expected_close_date = when
        db.commit()


def _history(app_id):
    with SessionLocal() as db:
        obj = db.get(models.JobApplication, app_id)
        return [(h.from_date, h.to_date) for h in obj.close_date_history]


def _date(app_id):
    with SessionLocal() as db:
        return db.get(models.JobApplication, app_id).expected_close_date


# --------------------------------------------------------------------------- #
# Blank is the normal state, and it is not overdue
# --------------------------------------------------------------------------- #
check("a new application has no expected close date", _date(PLAIN) is None)
check("...and no history to show for it", _history(PLAIN) == [])

page = client.get("/applications/{}/edit".format(PLAIN))
check("the edit page renders with no date set", page.status_code == 200)
check("...and offers the field", 'name="expected_close_date"' in page.text)
check("...and says nothing about being due",
      "Past due" not in page.text and "days out" not in page.text)


# --------------------------------------------------------------------------- #
# The listener logs every move, whoever makes it
# --------------------------------------------------------------------------- #
_set(MOVER, datetime(2026, 6, 30))
check("the first date ever set is logged", len(_history(MOVER)) == 1)
check("...with no from_date, since there was nothing before",
      _history(MOVER)[0][0] is None)

_set(MOVER, datetime(2026, 7, 31))
check("a move is logged", len(_history(MOVER)) == 2)
check("...carrying both ends",
      _history(MOVER)[1] == (datetime(2026, 6, 30), datetime(2026, 7, 31)))

_set(MOVER, datetime(2026, 7, 31))
check("re-setting the same date is not a move", len(_history(MOVER)) == 2)

_set(MOVER, None)
check("clearing the date is logged too", len(_history(MOVER)) == 3)
check("...as a to_date of NULL, which StageHistory could never express",
      _history(MOVER)[2][1] is None)

_set(MOVER, None)
check("clearing an already-blank date is not a move", len(_history(MOVER)) == 3)

_set(MOVER, datetime(2026, 9, 30))
check("setting it again after a clear is logged", len(_history(MOVER)) == 4)
check("...with no from_date, because there genuinely was none",
      _history(MOVER)[3][0] is None)


# --------------------------------------------------------------------------- #
# The form writes through the same listener
# --------------------------------------------------------------------------- #
def _edit(app_id, **overrides):
    with SessionLocal() as db:
        obj = db.get(models.JobApplication, app_id)
        data = {"company_id": obj.company_id, "title": obj.title or "",
                "stage": obj.stage.value, "applied_date": "",
                "expected_close_date": "", "notes": "", "context": ""}
    data.update(overrides)
    return client.post("/ui/applications/{}/edit".format(app_id), data=data,
                       follow_redirects=False)


before = len(_history(MOVER))
_edit(MOVER, expected_close_date="2026-11-15T00:00")
check("a date set through the edit form is logged by the same listener",
      len(_history(MOVER)) == before + 1)
check("...and the column holds it",
      _date(MOVER) == datetime(2026, 11, 15, 0, 0))

before = len(_history(MOVER))
_edit(MOVER, expected_close_date="2026-11-15T00:00")
check("saving the form without touching the date logs nothing",
      len(_history(MOVER)) == before)


# --------------------------------------------------------------------------- #
# Past due is about open pursuits only
# --------------------------------------------------------------------------- #
now = datetime.utcnow()
_set(PLAIN, now - timedelta(days=9))
page = client.get("/applications/{}/edit".format(PLAIN))
check("an open application past its date says so", "Past due by 9 days" in page.text)

_set(CLOSED, now - timedelta(days=9))
with SessionLocal() as db:
    obj = db.get(models.JobApplication, CLOSED)
    obj.stage = models.Stage.CLOSED_LOST
    db.commit()
page = client.get("/applications/{}/edit".format(CLOSED))
check("a closed application past its date is not nagged about",
      "Past due" not in page.text)
check("...but the date is still shown, because the gap is worth seeing",
      "Closed. Expected" in page.text)

_set(PLAIN, now + timedelta(days=5))
page = client.get("/applications/{}/edit".format(PLAIN))
check("a date still ahead reads as days out", "days out" in page.text)

_set(PLAIN, now)
page = client.get("/applications/{}/edit".format(PLAIN))
check("a date landing today says so", "Due today" in page.text)


# --------------------------------------------------------------------------- #
# The board
# --------------------------------------------------------------------------- #
_set(PLAIN, now - timedelta(days=3))
board = client.get("/board")
check("an overdue card carries the overrun in the score row",
      "3d over" in board.text)

check("...under the overdue marker, not some other span",
      'title="Expected to close' in board.text)

_set(PLAIN, now + timedelta(days=20))
board = client.get("/board")
check("a card whose date is still ahead shows the plain date instead",
      'class="tag due"' in board.text
      and 'title="Expected to close' not in board.text)

_set(MOVER, None)
_set(PLAIN, None)
_set(CLOSED, None)
board = client.get("/board")
# Deliberately matched on the markup rather than on the words: the forecast
# tooltip already contains "scored over 35 of 100 points", so a substring
# search for "d over" passes and fails for reasons that have nothing to do
# with this feature.
check("with no dates set, the board says nothing about them",
      'title="Expected to close' not in board.text
      and 'class="tag due"' not in board.text)


# --------------------------------------------------------------------------- #
# The slip log on the page
# --------------------------------------------------------------------------- #
_set(MOVER, datetime(2026, 6, 30))
_set(MOVER, datetime(2026, 7, 31))
_set(MOVER, datetime(2026, 9, 30))
page = client.get("/applications/{}/edit".format(MOVER))
check("the slip log renders once a date has moved",
      "Where this date has been" in page.text)
check("...counting the moves rather than the rows",
      "Moved " in page.text)
check("...and showing how far each move went", "+31d" in page.text)

with SessionLocal() as db:
    obj = db.get(models.JobApplication, MOVER)
    obj.expected_close_date = None
    db.commit()
    db.delete(obj)
    db.commit()
    left = db.query(models.CloseDateHistory).filter(
        models.CloseDateHistory.application_id == MOVER).count()
check("deleting an application takes its slip log with it", left == 0)


print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
