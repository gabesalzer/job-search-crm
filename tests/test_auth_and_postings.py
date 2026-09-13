"""Two bugs that hid each other, and the tests that would have caught them.

Posting creation raised `NameError: _ATS_DOMAINS` -- the ui split moved
`_company_website` into shared.py and left the constant it reads behind. The
gate missed it because nothing ever created a posting *with a URL*, which is
the only path that touches the website inference.

It presented as an authentication failure, because `await call_next(request)`
sat inside the credential check's `try`, so `except Exception: pass` swallowed
whatever the route raised and fell through to a 401. Re-entering correct
credentials just re-ran the failing route, which is why it looked like the
password was being rejected.

That second bug is the more dangerous one: it disguised *every* server error in
the app as a login prompt, and it would have hidden the next one too. So the
first assertions here are about the middleware itself.

Run: python3 tests/test_auth_and_postings.py
"""
import base64
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-auth-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ["APP_PASSWORD"] = "secret"
os.environ["APP_USERNAME"] = "gabe"
os.environ.pop("ANTHROPIC_API_KEY", None)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from fastapi import HTTPException  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"gabe:secret").decode()}

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #
check("no credentials is refused", client.get("/board").status_code == 401)
check("a wrong password is refused", client.get("/board", headers={
    "Authorization": "Basic " + base64.b64encode(b"gabe:wrong").decode()
}).status_code == 401)
check("a wrong username is refused", client.get("/board", headers={
    "Authorization": "Basic " + base64.b64encode(b"nobody:secret").decode()
}).status_code == 401)
check("a malformed header is a failed login, not a crash", client.get(
    "/board", headers={"Authorization": "Basic !!!not-base64!!!"}
).status_code == 401)
check("a non-Basic scheme is refused", client.get(
    "/board", headers={"Authorization": "Bearer whatever"}).status_code == 401)
check("the right credentials get in",
      client.get("/board", headers=AUTH).status_code == 200)
check("the health probe stays open, so Render can reach it",
      client.get("/health").status_code == 200)


# --------------------------------------------------------------------------- #
# A crash must look like a crash
# --------------------------------------------------------------------------- #
# The load-bearing assertion. A route that raises must not come back as 401 --
# that is what turned a NameError into "your password is wrong" and cost real
# time to diagnose.
@app.get("/_test_explodes")
def _explodes():
    raise RuntimeError("boom")


@app.get("/_test_404s")
def _404s():
    raise HTTPException(404, "nope")


check("a route that raises returns a server error, not a login prompt",
      client.get("/_test_explodes", headers=AUTH).status_code == 500)
check("...and an HTTPException still becomes its own status",
      client.get("/_test_404s", headers=AUTH).status_code == 404)
check("a raising route with NO credentials is still a 401 -- the gate comes "
      "first", client.get("/_test_explodes").status_code == 401)


# --------------------------------------------------------------------------- #
# Creating a posting, which is where the NameError lived
# --------------------------------------------------------------------------- #
def post(**over):
    data = {"company_name": "Vercel", "title": "GTM Systems Lead",
            "location": "Remote", "url": "", "jd_text": "", "comp_min": "",
            "comp_max": ""}
    data.update(over)
    return client.post("/ui/postings", headers=AUTH, data=data,
                       follow_redirects=False)


r = post()
check("a posting with no URL is created", r.status_code == 303)

# The path that was broken. A company URL on the employer's own domain means
# the domain IS the company site, so this branch reads _ATS_DOMAINS.
r = post(company_name="Plaid", url="https://plaid.com/careers/123")
check("a posting WITH a url is created -- the path that raised NameError",
      r.status_code == 303, "got {}".format(r.status_code))
with SessionLocal() as db:
    plaid = db.query(models.Company).filter(models.Company.name == "Plaid").first()
check("...and the company website is inferred from it",
      plaid is not None and plaid.website == "https://plaid.com")

r = post(company_name="Acme", url="https://boards.greenhouse.io/acme/jobs/1")
check("a posting from an ATS board is created", r.status_code == 303)
with SessionLocal() as db:
    acme = db.query(models.Company).filter(models.Company.name == "Acme").first()
check("...and no website is inferred, because the domain is the board rather "
      "than the employer", acme is not None and acme.website is None)

r = post(company_name="Vercel", url="https://vercel.com/careers/9",
         comp_min="150000", comp_max="180000", jd_text="Do great things")
check("comp and a JD come through", r.status_code == 303)
with SessionLocal() as db:
    p = (db.query(models.JobPosting)
         .filter(models.JobPosting.url == "https://vercel.com/careers/9").first())
check("...and are stored", p is not None and p.comp_min == 150000.0
      and p.jd_text == "Do great things")
check("an existing company is reused rather than duplicated",
      db.query(models.Company).filter(models.Company.name == "Vercel").count() == 1)

check("the postings page renders the result",
      "GTM Systems Lead" in client.get("/postings", headers=AUTH).text)


print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
