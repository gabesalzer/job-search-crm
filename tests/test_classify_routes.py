"""Classification and enrichment against the real app.

The parsers are covered by `test_classify.py` with literals. What only running
can check is the wiring: that a human value is never overwritten, that the
automatic pass fires on a posting change and *not* on an ordinary save, and
that a failed lookup doesn't roll back the rest of your edit. Every one of the
four bugs found in the thread-read feature was of exactly that kind — an
interaction between individually-correct pieces.

Run: python3 tests/test_classify_routes.py
"""
import os
import pathlib
import sys
import tempfile
from urllib.parse import unquote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-classify-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP  fastapi is not installed; route tests cannot run here.")
    raise SystemExit(0)

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import ui  # noqa: E402

client = TestClient(app)

REPLY = {"text": "SENIORITY: Director+\nSPECIALITY: Systems\nREASON: owns the function."}
PAGE = {"value": {"url": "https://acme.test/about", "text": "We raised a Series B. A team of 60."}}
CALLS = {"llm": 0, "fetch": 0}


def _fake_generate(system, messages, **kwargs):
    CALLS["llm"] += 1
    if isinstance(REPLY["text"], Exception):
        raise REPLY["text"]
    return REPLY["text"], "test-model"


def _fake_scrape(url, **kwargs):
    CALLS["fetch"] += 1
    if isinstance(PAGE["value"], Exception):
        raise PAGE["value"]
    return PAGE["value"]


ui.llm.generate = _fake_generate
ui.scrape.scrape_page_text = _fake_scrape


def _reset(reply=None, page=None):
    if reply is not None:
        REPLY["text"] = reply
    if page is not None:
        PAGE["value"] = page
    CALLS["llm"] = CALLS["fetch"] = 0


def _seed():
    with SessionLocal() as db:
        if db.query(models.Company).filter_by(name="Acme Classify").first():
            return
        company = models.Company(name="Acme Classify", website="https://acme.test")
        db.add(company)
        db.flush()
        posting = models.JobPosting(
            company_id=company.id, title="VP Revenue Operations",
            jd_text="Own forecasting, territory design and the CRM estate.")
        other = models.JobPosting(
            company_id=company.id, title="RevOps Manager",
            jd_text="Run the Salesforce estate day to day.")
        db.add_all([posting, other])
        db.commit()


_seed()


def _ids():
    with SessionLocal() as db:
        company = db.query(models.Company).filter_by(name="Acme Classify").first()
        postings = db.query(models.JobPosting).filter_by(
            company_id=company.id).order_by(models.JobPosting.id).all()
        return company.id, postings[0].id, postings[1].id


COMPANY_ID, POSTING_A, POSTING_B = _ids()


def _new_application(posting_id=None, title="RevOps Lead"):
    with SessionLocal() as db:
        appn = models.JobApplication(company_id=COMPANY_ID, title=title,
                                     job_posting_id=posting_id)
        db.add(appn)
        db.commit()
        return appn.id


def _row(app_id):
    with SessionLocal() as db:
        return db.get(models.JobApplication, app_id)


def _edit(app_id, **form):
    a = _row(app_id)
    payload = {
        "company_id": str(a.company_id), "title": a.title or "",
        "stage": a.stage.value,
        "job_posting_id": str(a.job_posting_id) if a.job_posting_id else "",
        "resume_id": "", "lost_reason": "", "lost_category": "",
        "applied_date": "", "created_at": "", "last_activity_date": "",
        "updated_at": "", "notes": a.notes or "", "context": "", "source": "",
        "manual_forecast": "", "champion": "",
        "seniority": a.seniority.value if a.seniority else "",
        "speciality": a.speciality.value if a.speciality else "",
    }
    payload.update(form)
    resp = client.post("/ui/applications/{}/edit".format(app_id),
                       data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def _message(resp):
    """The human-readable half of a redirect, decoded.

    Assertions read the decoded text rather than the raw query string, so they
    pin the sentence the user sees instead of an encoding detail.
    """
    return unquote(resp.headers["location"].replace("+", " "))


def _company(company_id=None):
    with SessionLocal() as db:
        return db.get(models.Company, company_id or COMPANY_ID)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_the_button_classifies_from_the_posting():
    _reset(reply="SENIORITY: Director+\nSPECIALITY: Systems\nREASON: owns it.")
    app_id = _new_application(POSTING_A)
    resp = client.post("/ui/applications/{}/classify".format(app_id),
                       follow_redirects=False)
    assert resp.status_code == 303
    a = _row(app_id)
    assert a.seniority == models.Seniority.DIRECTOR_PLUS
    assert a.speciality == models.Speciality.SYSTEMS
    assert a.classification_source == "model"
    assert a.classification_model == "test-model"
    assert a.classified_at is not None
    assert "owns it" in a.classification_note


def test_linking_a_posting_classifies_without_a_button():
    _reset(reply="SENIORITY: Manager\nSPECIALITY: Strategy\nREASON: runs a team.")
    app_id = _new_application(None)
    _edit(app_id, job_posting_id=str(POSTING_A))
    a = _row(app_id)
    assert a.seniority == models.Seniority.MANAGER
    assert CALLS["llm"] == 1


def test_an_ordinary_save_costs_nothing():
    _reset()
    app_id = _new_application(POSTING_A)
    client.post("/ui/applications/{}/classify".format(app_id), follow_redirects=False)
    before = CALLS["llm"]
    _edit(app_id, notes="just jotting something down")
    assert CALLS["llm"] == before, (
        "editing a note must not cost an API call; the previous answer is "
        "still correct for a JD nobody touched")


def test_changing_the_posting_reclassifies():
    _reset(reply="SENIORITY: Director+\nSPECIALITY: Systems\nREASON: x")
    app_id = _new_application(POSTING_A)
    client.post("/ui/applications/{}/classify".format(app_id), follow_redirects=False)
    _reset(reply="SENIORITY: Manager\nSPECIALITY: Strategy\nREASON: different role.")
    _edit(app_id, job_posting_id=str(POSTING_B))
    assert _row(app_id).seniority == models.Seniority.MANAGER
    assert CALLS["llm"] == 1


def test_a_value_you_typed_is_never_overwritten():
    _reset(reply="SENIORITY: Director+\nSPECIALITY: Systems\nREASON: x")
    app_id = _new_application(None)
    _edit(app_id, seniority="Manager")
    a = _row(app_id)
    assert a.seniority == models.Seniority.MANAGER
    assert a.classification_source is None, "typing takes ownership"
    assert CALLS["llm"] == 0

    # The button refuses too, rather than silently doing nothing.
    resp = client.post("/ui/applications/{}/classify".format(app_id),
                       follow_redirects=False)
    assert "already classified" in _message(resp)
    assert _row(app_id).seniority == models.Seniority.MANAGER


def test_clearing_a_value_while_relinking_does_not_refill_it():
    """The bug the thread read shipped once, guarded here before it could."""
    _reset(reply="SENIORITY: Director+\nSPECIALITY: Systems\nREASON: x")
    app_id = _new_application(POSTING_A)
    client.post("/ui/applications/{}/classify".format(app_id), follow_redirects=False)
    _reset()
    # Clear both fields *and* relink the posting in one submit.
    _edit(app_id, seniority="", speciality="", job_posting_id=str(POSTING_B))
    a = _row(app_id)
    assert (a.seniority, a.speciality) == (None, None), (
        "clearing is an edit, and an edit claims the field for the rest of "
        "the request")
    assert CALLS["llm"] == 0


def test_a_declined_classification_is_stored_as_a_completed_read():
    _reset(reply="SENIORITY: NONE\nSPECIALITY: NONE\nREASON: an IC analyst role.")
    app_id = _new_application(POSTING_A)
    client.post("/ui/applications/{}/classify".format(app_id), follow_redirects=False)
    a = _row(app_id)
    assert (a.seniority, a.speciality) == (None, None)
    assert a.classification_source == "model", (
        "blank-because-declined must be distinguishable from never-classified")
    assert "analyst" in a.classification_note


def test_an_unparseable_reply_writes_nothing():
    _reset(reply="I'd say it's fairly senior, systems-leaning.")
    app_id = _new_application(POSTING_A)
    resp = client.post("/ui/applications/{}/classify".format(app_id),
                       follow_redirects=False)
    a = _row(app_id)
    assert (a.seniority, a.speciality, a.classification_source) == (None, None, None)
    assert "could not be read" in _message(resp)


def test_an_api_failure_does_not_cost_the_rest_of_the_save():
    _reset(reply=ui.llm.LLMError("API returned 429: rate limited"))
    app_id = _new_application(None)
    _edit(app_id, notes="a note I typed", job_posting_id=str(POSTING_A))
    a = _row(app_id)
    assert a.notes == "a note I typed", "the edit survives a failed classification"
    assert a.job_posting_id == POSTING_A
    assert a.classification_source is None


def test_a_record_with_nothing_to_read_refuses_clearly():
    _reset()
    app_id = _new_application(None, title="")
    resp = client.post("/ui/applications/{}/classify".format(app_id),
                       follow_redirects=False)
    assert "no job description" in _message(resp)
    assert CALLS["llm"] == 0


# --------------------------------------------------------------------------- #
# Company enrichment
# --------------------------------------------------------------------------- #
def _fresh_company(name, website="https://acme.test"):
    with SessionLocal() as db:
        row = models.Company(name=name, website=website)
        db.add(row)
        db.commit()
        return row.id


def test_the_lookup_reads_the_site_and_records_where_from():
    _reset(reply="FUNDING: Series B\nEMPLOYEES: 51-200\nREASON: about page says both.",
           page={"url": "https://acme.test/about", "text": "We raised a Series B."})
    cid = _fresh_company("Acme Lookup")
    resp = client.post("/ui/companies/{}/enrich".format(cid), follow_redirects=False)
    assert resp.status_code == 303
    c = _company(cid)
    assert c.funding_stage == models.FundingStage.SERIES_B
    assert c.employee_band == models.EmployeeBand.B_51_200
    assert c.enrichment_source == "model"
    assert c.enrichment_url == "https://acme.test/about", (
        "the page it actually read, not the URL it was asked for -- a value is "
        "only as checkable as the page behind it")
    assert c.enriched_at is not None


def test_a_site_that_says_nothing_leaves_both_blank_and_says_so():
    _reset(reply="FUNDING: NONE\nEMPLOYEES: NONE\nREASON: marketing homepage only.",
           page={"url": "https://acme.test", "text": "We make software."})
    cid = _fresh_company("Acme Silent")
    resp = client.post("/ui/companies/{}/enrich".format(cid), follow_redirects=False)
    c = _company(cid)
    assert (c.funding_stage, c.employee_band) == (None, None)
    assert c.enrichment_source == "model", (
        "looked-and-found-nothing must not look like never-looked")
    assert "found nothing" in _message(resp)


def test_a_company_with_no_website_refuses_before_spending_anything():
    _reset()
    cid = _fresh_company("Acme No Site", website=None)
    resp = client.post("/ui/companies/{}/enrich".format(cid), follow_redirects=False)
    assert "No website" in _message(resp)
    assert CALLS["fetch"] == 0 and CALLS["llm"] == 0


def test_a_failed_fetch_writes_nothing():
    _reset(page=RuntimeError("connection refused"))
    cid = _fresh_company("Acme Unreachable")
    resp = client.post("/ui/companies/{}/enrich".format(cid), follow_redirects=False)
    c = _company(cid)
    assert c.enrichment_source is None and c.funding_stage is None
    assert "fetch" in _message(resp)
    assert CALLS["llm"] == 0, "a failed fetch must not still cost a model call"


def test_your_own_values_are_never_overwritten_by_a_lookup():
    _reset(reply="FUNDING: Series B\nEMPLOYEES: 51-200\nREASON: x",
           page={"url": "https://acme.test", "text": "Series B."})
    cid = _fresh_company("Acme Mine")
    client.post("/ui/companies/{}/edit".format(cid), data={
        "name": "Acme Mine", "company_type": "Employer",
        "website": "https://acme.test", "industry": "", "notes": "",
        "funding_stage": "Seed", "employee_band": "11-50",
    }, follow_redirects=False)
    c = _company(cid)
    assert c.funding_stage == models.FundingStage.SEED
    assert c.enrichment_source is None, "typing takes ownership"

    resp = client.post("/ui/companies/{}/enrich".format(cid), follow_redirects=False)
    assert "already filled" in _message(resp)
    assert _company(cid).funding_stage == models.FundingStage.SEED
    assert CALLS["fetch"] == 0


def test_editing_the_fields_drops_the_citation():
    _reset(reply="FUNDING: Series B\nEMPLOYEES: 51-200\nREASON: x",
           page={"url": "https://acme.test/about", "text": "Series B."})
    cid = _fresh_company("Acme Recite")
    client.post("/ui/companies/{}/enrich".format(cid), follow_redirects=False)
    assert _company(cid).enrichment_url is not None

    client.post("/ui/companies/{}/edit".format(cid), data={
        "name": "Acme Recite", "company_type": "Employer",
        "website": "https://acme.test", "industry": "", "notes": "",
        "funding_stage": "Series C", "employee_band": "51-200",
    }, follow_redirects=False)
    c = _company(cid)
    assert c.funding_stage == models.FundingStage.SERIES_C
    assert c.enrichment_source is None
    assert c.enrichment_url is None, (
        "a value you changed must not go on carrying a citation that no longer "
        "describes it")


def test_the_enums_and_the_classifier_vocabularies_cannot_drift():
    """The prompt is generated from classify.py; the column accepts models.py.

    If those two lists ever disagree, the model is asked for a value the column
    will refuse -- and the failure is a 500 on save, at the worst possible
    moment, for a value that was correct. Cheap to pin, so pin it.
    """
    from app import classify
    for enum_cls, values in (
        (models.Seniority, classify.SENIORITY_VALUES),
        (models.Speciality, classify.SPECIALITY_VALUES),
        (models.FundingStage, classify.FUNDING_STAGES),
        (models.EmployeeBand, classify.EMPLOYEE_BANDS),
    ):
        assert [m.value for m in enum_cls] == values, enum_cls.__name__


def test_the_new_fields_reach_the_chat_and_the_filters():
    """Otherwise they are write-only: stored, and invisible to everything."""
    from app import viewspec
    from app.routers.ui import _analytics_apps, _chat_corpus, _vocabulary

    for field in ("seniority", "speciality", "funding_stage", "employee_band"):
        assert field in viewspec.TEXT_FIELDS, field
        assert field in viewspec.COMPARE_FIELDS, field
        assert field in viewspec.PROMPT, (
            "a field the parser accepts but the prompt never mentions is dead")

    with SessionLocal() as db:
        rows = _analytics_apps(db)
        assert all(field in rows[0] for field in
                   ("seniority", "speciality", "funding_stage", "employee_band"))
        assert set(_vocabulary(rows)) >= {"seniority", "speciality",
                                          "funding_stage", "employee_band"}
        corpus = _chat_corpus(db)
    assert "Seniority:" in corpus or "Speciality:" in corpus, (
        "at least one seeded application is classified, so the corpus must "
        "carry it")


def test_the_edit_pages_render_every_state():
    for app_id in (_new_application(POSTING_A), _new_application(None)):
        assert client.get("/applications/{}/edit".format(app_id)).status_code == 200
    assert client.get("/companies/{}/edit".format(COMPANY_ID)).status_code == 200


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("  PASS  {}".format(t.__name__))
        passed += 1
    print("\n{}/{} classification route assertions passed.".format(passed, len(tests)))
