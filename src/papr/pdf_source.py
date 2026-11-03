"""Import verified original journal pages from an explicitly selected PDF source."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from .artifacts import atomic_json, checksum
from .config import Config
from .fetchers import _cache_path
from .locking import state_lock
from .resolvers import resolve
from .resolvers.common import normalize_doi
from .selection import select_candidate


def _normalized(text: str) -> str:
    return re.sub(r"[^\w]", "", text.casefold())


def import_pdf(
    doi: str, pdf: Path, config: Config, *, pages: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Verify identity before populating the normal original-PDF cache.

    The user selects an archive; this never guesses a thesis page range or
    reconstructs a document. The source file is left untouched.
    """
    identifier = normalize_doi(doi)
    if not identifier:
        raise ValueError("Cache import requires a DOI")
    source = pdf.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"PDF source not found: {source}")
    selected = None
    if pages:
        match = re.fullmatch(r"([1-9]\d*)-([1-9]\d*)", pages)
        if not match or int(match[1]) > int(match[2]):
            raise ValueError("--pages must be an inclusive PDF page range, e.g. 99-103")
        selected = [int(match[1]), int(match[2])]
    public_url = None
    if source_url:
        parsed = urlsplit(source_url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
        ):
            raise ValueError("--source-url must be a public HTTP(S) archive record URL")
        public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    article = select_candidate(identifier, resolve(identifier, config), config)
    source_digest = checksum(source)
    try:
        reader = PdfReader(source)
    except PdfReadError as exc:
        raise ValueError("Source is not a readable PDF") from exc
    if reader.is_encrypted:
        raise ValueError("Cannot verify an encrypted PDF source")
    start, end = selected or [1, len(reader.pages)]
    if not (1 <= start <= end <= len(reader.pages)):
        raise ValueError("Selected pages are outside the PDF source")
    first = reader.pages[start - 1].extract_text() or ""
    if identifier not in first.casefold() or (
        not _normalized(article.title) or _normalized(article.title) not in _normalized(first)
    ):
        raise ValueError(
            "The first selected page must contain the requested DOI and article title. "
            "Check the source and page range; scanned pages require separate verification."
        )
    extent = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", article.pages or "")
    if extent and end - start + 1 != int(extent[2]) - int(extent[1]) + 1:
        raise ValueError("Selected page count does not match the article's journal page range")
    with state_lock(config):
        cache = _cache_path(article, config)
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache.parent, prefix="import-") as directory:
            prepared = Path(directory) / "verified.pdf"
            if selected:
                writer = PdfWriter()
                for page in reader.pages[start - 1:end]:
                    writer.add_page(page)
                writer.add_metadata({"/Title": article.title})
                writer.write(prepared)
            else:
                shutil.copy2(source, prepared)
            if checksum(source) != source_digest:
                raise ValueError("PDF source changed during verification; retry the import")
            record = {
                "doi": identifier, "title": article.title, "source_file": str(source),
                "source_sha256": source_digest, "source_url": public_url,
                "pdf_pages": [start, end], "output_pages": end - start + 1,
                "cache_path": str(cache), "sha256": checksum(prepared),
                "method": "original-pages" if selected else "original-pdf",
            }
            atomic_json(config.data_dir / "pdf-sources" / f"{cache.stem}.json", record)
            prepared.replace(cache)
    return record
