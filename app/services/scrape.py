"""Job-posting scraper.

Extraction strategy, in order:

1. **Known ATS APIs** (Greenhouse, Lever). These boards render their content with
   JavaScript, so a plain fetch of the page only sees bare metadata. But each has
   a public JSON API that returns the *complete* posting — title, location, and
   full description — reliably and with no API key. We detect the board from the
   URL and call it directly.
2. **schema.org JobPosting JSON-LD** embedded in the page (many company career
   pages include it for SEO).
3. **Firecrawl**, if ``FIRECRAWL_API_KEY`` is set — renders JavaScript, so it
   handles anything the above miss (LinkedIn, Indeed, Workday).
4. **Open Graph / <meta>** tags as a last resort.

Compensation is additionally sniffed out of the description text (e.g.
"$180,000 to $250,000"), since most postings state pay in prose rather than a
structured field.

Pure standard library plus httpx — nothing extra to install.
"""
from __future__ import annotations

import json
import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import httpx

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

_EMPTY = {
    "title": None,
    "company_name": None,
    "location": None,
    "comp_min": None,
    "comp_max": None,
    "comp_currency": None,
    "jd_text": None,
}


def scrape_job(url: str) -> dict:
    """Fetch ``url`` and return the JobPosting fields we can extract."""
    fields = _try_ats(url)
    if not fields:
        html = _fetch_html(url)
        job = _find_jsonld_job(html)
        fields = _from_jsonld(job) if job else _from_meta(html)

    # If pay wasn't a structured field, try to read it out of the description.
    if fields.get("comp_min") is None and fields.get("jd_text"):
        lo, hi = _salary_from_text(fields["jd_text"])
        if lo is not None:
            fields["comp_min"], fields["comp_max"] = lo, hi
            fields["comp_currency"] = fields.get("comp_currency") or "USD"

    fields["source_url"] = url
    fields["used_firecrawl"] = bool(FIRECRAWL_API_KEY)
    return fields


# --------------------------------------------------------------------------- #
# Known ATS APIs
# --------------------------------------------------------------------------- #
def _try_ats(url: str) -> Optional[dict]:
    parsed = urlparse(url if re.match(r"^https?://", url, re.I) else "https://" + url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    try:
        if "greenhouse.io" in host and "jobs" in parts:
            i = parts.index("jobs")
            return _greenhouse(parts[i - 1], parts[i + 1])
        if "lever.co" in host and len(parts) >= 2:
            return _lever(parts[0], parts[1])
    except Exception:
        return None
    return None


def _greenhouse(token: str, job_id: str) -> dict:
    api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?content=true"
    data = httpx.get(api, headers={"User-Agent": _UA}, timeout=30)
    data.raise_for_status()
    d = data.json()
    fields = dict(_EMPTY)
    fields["title"] = _clean(d.get("title"))
    # content is HTML with entities escaped — unescape once, then strip tags.
    fields["jd_text"] = _strip_html(unescape(d.get("content") or ""))
    loc = d.get("location") or {}
    fields["location"] = _clean(loc.get("name") if isinstance(loc, dict) else loc)
    fields["company_name"] = _greenhouse_company(token)
    return fields


def _greenhouse_company(token: str) -> str:
    try:
        r = httpx.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}",
            headers={"User-Agent": _UA},
            timeout=15,
        )
        r.raise_for_status()
        name = (r.json() or {}).get("name")
        if name:
            return _clean(name)
    except Exception:
        pass
    return token.replace("-", " ").title()


def _lever(company: str, job_id: str) -> dict:
    api = f"https://api.lever.co/v0/postings/{company}/{job_id}?mode=json"
    r = httpx.get(api, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    d = r.json()
    cats = d.get("categories") or {}
    fields = dict(_EMPTY)
    fields["title"] = _clean(d.get("text"))
    fields["company_name"] = company.replace("-", " ").title()
    fields["location"] = _clean(cats.get("location"))
    fields["jd_text"] = _clean_multiline(
        d.get("descriptionPlain") or _strip_html(d.get("description") or "")
    )
    return fields


# --------------------------------------------------------------------------- #
# Fetching (for JSON-LD / meta / Firecrawl paths)
# --------------------------------------------------------------------------- #
def _fetch_html(url: str) -> str:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    if FIRECRAWL_API_KEY:
        try:
            resp = httpx.post(
                FIRECRAWL_ENDPOINT,
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
                json={"url": url, "formats": ["rawHtml"]},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
            html = (payload.get("data") or {}).get("rawHtml") or ""
            if html.strip():
                return html
        except Exception:
            pass

    resp = httpx.get(url, headers={"User-Agent": _UA}, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------- #
# schema.org JobPosting JSON-LD
# --------------------------------------------------------------------------- #
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _iter_jsonld(html: str):
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1).strip())
        except Exception:
            continue
        yield from _flatten(data)


def _flatten(data):
    if isinstance(data, list):
        for item in data:
            yield from _flatten(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _flatten(item)
        yield data


def _is_job(obj: dict) -> bool:
    t = obj.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() == "jobposting" for x in types)


def _find_jsonld_job(html: str) -> Optional[dict]:
    for obj in _iter_jsonld(html):
        if isinstance(obj, dict) and _is_job(obj):
            return obj
    return None


def _from_jsonld(job: dict) -> dict:
    company = job.get("hiringOrganization")
    if isinstance(company, dict):
        company = company.get("name")
    comp_min, comp_max, currency = _parse_salary(job.get("baseSalary"))
    fields = dict(_EMPTY)
    fields.update(
        title=_clean(job.get("title")),
        company_name=_clean(company) if isinstance(company, str) else None,
        location=_location(job.get("jobLocation"), job),
        comp_min=comp_min,
        comp_max=comp_max,
        comp_currency=currency,
        jd_text=_strip_html(job.get("description")),
    )
    return fields


def _location(job_location, job) -> Optional[str]:
    def one(loc):
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                country = addr.get("addressCountry")
                if isinstance(country, dict):
                    country = country.get("name")
                parts = [
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                    country if isinstance(country, str) else None,
                ]
                parts = [p for p in parts if p]
                if parts:
                    return ", ".join(parts)
            if isinstance(addr, str):
                return addr
        elif isinstance(loc, str):
            return loc
        return None

    if isinstance(job_location, list):
        labels = [lbl for lbl in (one(loc) for loc in job_location) if lbl]
        label = "; ".join(dict.fromkeys(labels)) if labels else None
    else:
        label = one(job_location)

    remote = job.get("jobLocationType")
    if remote and str(remote).lower().startswith("tele"):
        label = "Remote" if not label else f"Remote · {label}"
    return label


def _parse_salary(base):
    if not isinstance(base, dict):
        return None, None, None
    currency = base.get("currency") or base.get("salaryCurrency")
    value = base.get("value")
    if isinstance(value, dict):
        lo = _num(value.get("minValue"))
        hi = _num(value.get("maxValue"))
        if lo is None and hi is None:
            lo = hi = _num(value.get("value"))
    else:
        lo = hi = _num(value)
    return lo, hi, (currency if isinstance(currency, str) else None)


def _salary_from_text(text: str):
    """Best-effort pay range out of prose, e.g. '$180,000 to $250,000'."""
    m = re.search(
        r"\$\s?([\d,]+(?:\.\d+)?)\s?([kK])?\s*(?:-|–|—|to)\s*\$?\s?([\d,]+(?:\.\d+)?)\s?([kK])?",
        text,
    )
    if not m:
        return None, None
    lo = _num(m.group(1))
    hi = _num(m.group(3))
    if lo is not None and m.group(2):
        lo *= 1000
    if hi is not None and m.group(4):
        hi *= 1000
    # sanity: ignore tiny/again-nonsensical ranges
    if lo is not None and lo < 1000:
        return None, None
    return lo, hi


def _num(x):
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Open Graph / meta fallback
# --------------------------------------------------------------------------- #
def _meta(html, *names):
    for name in names:
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']'
            + re.escape(name)
            + r'["\'][^>]*content=["\'](.*?)["\']',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            return _clean(unescape(m.group(1)))
    return None


def _from_meta(html: str) -> dict:
    title = _meta(html, "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = _clean(unescape(m.group(1))) if m else None
    fields = dict(_EMPTY)
    fields.update(
        title=title,
        company_name=_meta(html, "og:site_name"),
        jd_text=_meta(html, "og:description", "description", "twitter:description"),
    )
    return fields


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "li", "div", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")


def _strip_html(s):
    if not s or not isinstance(s, str):
        return None
    if "<" in s and ">" in s:
        parser = _TextExtractor()
        try:
            parser.feed(s)
            s = "".join(parser.parts)
        except Exception:
            s = re.sub(r"<[^>]+>", " ", s)
    return _clean_multiline(unescape(s))


def _clean_multiline(s):
    if not s or not isinstance(s, str):
        return None
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() or None


def _clean(s):
    if not s or not isinstance(s, str):
        return None
    return re.sub(r"\s+", " ", s).strip() or None


# --------------------------------------------------------------------------- #
# Generic page text, for company enrichment
# --------------------------------------------------------------------------- #
# `scrape_job` above is shaped entirely around job postings: it tries ATS APIs
# first, looks for schema.org JobPosting, and sniffs salary out of prose. None
# of that helps read a company's own homepage, so this is a separate, much
# simpler door: fetch the page, return its readable text.

# A homepage often says nothing useful while its about or careers page does, so
# a small number of conventional paths are tried in turn. Kept short and
# conventional on purpose -- each one is a fetch, and a long list would turn one
# button press into a dozen Firecrawl credits.
_COMPANY_SUBPAGES = ["", "/about", "/about-us", "/company", "/careers"]

# Enough text to carry an about page, far short of what a marketing site can
# weigh. The classifier caps again on its own side; this cap is about not
# holding a 5MB string in memory per press.
MAX_PAGE_CHARS = 60_000


def scrape_page_text(url: str, *, try_subpages: bool = True) -> dict:
    """Fetch ``url`` and return ``{"url": ..., "text": ...}``.

    Returns the first page that yields a usable amount of text, and reports
    **which** URL that was rather than the one it was asked for. That
    distinction is the whole reason this returns a dict: a funding stage read
    off `/about` should be recorded as having come from `/about`, because the
    value is only as checkable as the page behind it.

    Raises the underlying httpx error if nothing can be fetched at all. The
    caller decides what a failure means; this function does not invent an empty
    page, because "the site returned nothing" and "the site said nothing
    useful" are different answers and only one of them is worth storing.
    """
    if not url or not url.strip():
        raise ValueError("No website is recorded for this company.")
    base = url.strip().rstrip("/")
    if not re.match(r"^https?://", base, re.IGNORECASE):
        base = "https://" + base

    paths = _COMPANY_SUBPAGES if try_subpages else [""]
    first_error: Optional[Exception] = None
    best = {"url": base, "text": ""}

    for path in paths:
        candidate = base + path
        try:
            html = _fetch_html(candidate)
        except Exception as exc:            # noqa: BLE001 - any fetch failure
            first_error = first_error or exc
            continue
        text = _strip_html(html) or ""
        text = text[:MAX_PAGE_CHARS]
        # Keep the longest page seen, so a thin homepage loses to a real about
        # page rather than winning by being first.
        if len(text) > len(best["text"]):
            best = {"url": candidate, "text": text}
        # A page with real prose on it is enough; stop paying for more.
        if len(text) > 1200:
            break

    if not best["text"].strip():
        if first_error is not None:
            raise first_error
        raise ValueError("Fetched the site but found no readable text on it.")
    return best
