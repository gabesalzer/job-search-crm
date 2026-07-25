"""Standalone Jinja2 render smoke-test for the new/edited templates.

Doesn't need FastAPI/SQLAlchemy/the app package — just renders each template
with lightweight mock objects that have the same attributes the real
SQLAlchemy models expose, to catch Jinja syntax errors and typo'd attribute
references before deploying.
"""
import pathlib
import sys
from datetime import datetime
from types import SimpleNamespace

import jinja2

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)))


class Enum(SimpleNamespace):
    pass


def enum(value):
    return Enum(value=value)


company = SimpleNamespace(
    id=1, name="Plaid", company_type=enum("Employer"), website="https://plaid.com",
    industry="Fintech", notes="Great culture", applications=[], postings=[], people=[],
)

posting = SimpleNamespace(
    id=2, title="GTM Strategy & Ops", location="Remote", url="https://plaid.com/careers/2",
    jd_text="Do great things", comp_min=150000, comp_max=180000, company=company,
    my_rating=enum("Up"), rating_reason="Good fit",
)

resume = SimpleNamespace(
    id=3, label="RevOps v3", content="Experienced RevOps leader...", source_link=None,
    filename="resume.pdf", notes="metrics-forward", applications=[],
)

app_obj = SimpleNamespace(
    id=4, title="Sr. RevOps Manager", company_id=1, company=company, stage=enum("Applied"),
    lost_reason=None, resume_id=3, resume=resume, job_posting_id=2,
    applied_date=datetime(2026, 7, 1, 10, 0), notes="Referred by Jane",
)

meeting = SimpleNamespace(
    id=5, title="Interview with Plaid", meeting_type=enum("Hiring Manager"),
    meeting_date=datetime(2026, 7, 10, 15, 0), summary="Went well", transcript="Me: Hi\nThem: Hi",
    notes="", granola_note_id="abc123", granola_link="https://granola.ai/notes/abc123",
    application=app_obj,
)

cases = [
    ("company_edit.html", {"active": "companies", "company": company, "company_types": ["Employer", "Agency", "Both"]}),
    ("posting_edit.html", {"active": "postings", "posting": posting}),
    ("resume_edit.html", {"active": "resumes", "resume": resume}),
    ("application_edit.html", {
        "active": "board", "app_obj": app_obj, "stages": ["Saved", "Applied", "Closed Lost"],
        "lost_reasons": ["Ghosted", "Other"], "companies": [company], "resumes": [resume],
        "postings": [posting],
    }),
    ("meeting_edit.html", {
        "active": "meetings", "meeting": meeting, "applications": [app_obj],
        "meeting_types": ["Hiring Manager", "Technical"], "granola_enabled": True,
    }),
    ("meeting_edit.html (granola disabled)", {
        "active": "meetings", "meeting": meeting, "applications": [app_obj],
        "meeting_types": ["Hiring Manager", "Technical"], "granola_enabled": False,
    }),
    ("companies.html", {"active": "companies", "companies": [company], "company_types": ["Employer"]}),
    ("postings.html", {"active": "postings", "postings": [posting], "companies": [company]}),
    ("resumes.html", {"active": "resumes", "resumes": [resume]}),
    ("meetings.html", {
        "active": "meetings", "meetings": [meeting], "applications": [app_obj],
        "meeting_types": ["Hiring Manager"], "granola_enabled": True,
    }),
    ("board.html", {
        "active": "board", "stages": ["Saved", "Applied"],
        "grouped": {"Saved": [], "Applied": [app_obj]}, "companies": [company],
        "resumes": [resume], "postings": [posting],
    }),
]

failures = 0
for name, ctx in cases:
    template_name = name.split(" ")[0]
    try:
        html = env.get_template(template_name).render(**ctx)
        assert "{%" not in html and "{{" not in html, "unrendered Jinja syntax leaked into output"
        print(f"OK   {name}  ({len(html)} chars)")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")

sys.exit(1 if failures else 0)
