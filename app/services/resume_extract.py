"""Extract plain text from an uploaded resume (PDF / DOCX / TXT).

The extracted text is what the resume analysis runs on, so it matters more than
keeping the original file. PDF uses pypdf; DOCX uses python-docx; plain text is
decoded directly. Imports are done lazily so the app still starts if a parser
isn't installed — the caller surfaces a helpful message instead.
"""
from __future__ import annotations

import io
import re


def extract_text(filename: str, data: bytes) -> str:
    """Return plain text extracted from ``data`` based on the file extension."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        text = _from_pdf(data)
    elif name.endswith(".docx"):
        text = _from_docx(data)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", "ignore")
    else:
        # Unknown extension — try PDF, then DOCX, then treat as text.
        text = ""
        for fn in (_from_pdf, _from_docx):
            try:
                text = fn(data)
                if text:
                    break
            except Exception:
                continue
        if not text:
            text = data.decode("utf-8", "ignore")
    return _tidy(text)


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PDF support needs the 'pypdf' package — run: pip install -r requirements.txt"
        ) from exc
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _from_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "DOCX support needs the 'python-docx' package — run: pip install -r requirements.txt"
        ) from exc
    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)


def _tidy(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
