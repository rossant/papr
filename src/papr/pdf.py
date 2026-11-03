"""PDF signature sniffing and full-file validation before cache publication."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from pypdf import PdfReader

if TYPE_CHECKING:
    from .model import Article


class PdfValidationError(ValueError):
    """A downloaded file is damaged, unreadable, or demonstrably another paper."""


def is_pdf(content: bytes, content_type: str = "") -> bool:
    """Check the PDF signature; publisher content types are not authoritative."""
    return content.startswith(b"%PDF-")


def is_local_pdf(value: str | Path | None) -> bool:
    """Cheap signature check, suitable for discovery and streaming (not validation)."""
    if not value:
        return False
    path = Path(value).expanduser()
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return is_pdf(stream.read(5))
    except OSError:
        return False


def doi_tokens(text: str) -> set[str]:
    """Read complete DOI tokens without accepting a requested DOI's prefix."""
    identifiers = set()
    for token in re.findall(r"(?<!\w)10\.\d{4,9}/\S+", text, re.I):
        token = token.rstrip(".,;\"'")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
            while token.endswith(closing) and token.count(closing) > token.count(opening):
                token = token[:-1].rstrip(".,;")
        identifiers.add(token.casefold())
    return identifiers


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _verify_identity(reader: PdfReader, article: Article, first_text: str) -> None:
    """Reject positive identity conflicts; absent text/identifiers remain acceptable.

    Reference-list DOIs do not identify a paper. Only explicitly labelled DOI
    lines near the start or DOI metadata are considered contradictory evidence.
    A matching title in the first-page text takes precedence over such lines.
    """
    expected = _words(article.title)
    actual_text = " ".join(_words(first_text))
    title_matches = bool(expected) and " ".join(expected) in actual_text
    metadata = reader.metadata or {}
    if article.doi and not title_matches:
        expected_doi = article.doi.casefold().removeprefix("https://doi.org/")
        labelled = []
        for line in first_text[:1500].splitlines():
            if re.match(r"^\s*(?:doi\s*:|https?://(?:dx\.)?doi\.org/)", line, re.I):
                labelled.append(line)
        labelled.extend(str(metadata.get(key, "")) for key in ("/DOI", "/doi"))
        printed = doi_tokens("\n".join(labelled))
        if printed and expected_doi not in printed:
            raise PdfValidationError("PDF identifies a different DOI than the requested paper")
    # Metadata alone is often a template name. Treat a conflicting title as
    # identity evidence only when it is substantive and also printed on page 1.
    title = str(metadata.get("/Title", ""))
    actual = _words(title)
    if (
        len(expected) >= 4 and len(actual) >= 4 and not title_matches
        and " ".join(actual) in actual_text
        and len(set(actual) & set(expected)) / len(set(expected)) < 0.25
    ):
        raise PdfValidationError("PDF title identifies a different paper than requested")


def validate_pdf(path: Path, article: Article | None = None) -> None:
    """Require a complete, readable PDF with pages and usable content streams.

    Scans and papers without extractable identifiers pass structural validation.
    Identity checking is conservative and never requires OCR or title presence.
    """
    try:
        with Path(path).open("rb") as stream:
            if not is_pdf(stream.read(5)):
                raise PdfValidationError("Downloaded file does not have a PDF signature")
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 1024))
            if b"%%EOF" not in stream.read():
                raise PdfValidationError("PDF is truncated: end-of-file marker is missing")
            stream.seek(0)
            reader = PdfReader(stream, strict=False)
            if reader.is_encrypted:
                raise PdfValidationError("PDF is encrypted and cannot be verified")
            if not len(reader.pages):
                raise PdfValidationError("PDF contains no pages")
            first_text = ""
            for index, page in enumerate(reader.pages):
                if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                    raise PdfValidationError("PDF contains an invalid page")
                content = page.get_contents()
                if content is not None:
                    content.get_data()
                if article is not None and index == 0:
                    # Unsupported fonts must not prevent retrieval of a valid scan
                    # or paper; structural validation above remains mandatory.
                    try:
                        first_text = page.extract_text() or ""
                    except Exception:
                        first_text = ""
            if article is not None:
                _verify_identity(reader, article, first_text)
    except PdfValidationError:
        raise
    except Exception as exc:
        raise PdfValidationError("PDF is damaged or cannot be read") from exc
