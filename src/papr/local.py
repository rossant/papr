from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfReader

from .config import Config
from .model import Article, Person
from .resolvers import resolve
from .resolvers.common import extract_doi, extract_year
from .selection import select_candidate


def article_from_pdf(
    path: Path, config: Config, *, use_local: bool = True, use_remote: bool = True
) -> Article:
    reader = PdfReader(str(path))
    meta = reader.metadata or {}
    chunks = []
    for page in reader.pages[:2]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            logging.getLogger(__name__).debug(
                "Text extraction failed for a page in %s", path, exc_info=True
            )
    text = "\n".join(chunks)
    doi = extract_doi(text) or next(
        (
            value
            for key in ("/DOI", "/doi", "/Subject", "/Keywords", "/Title")
            if (value := extract_doi(str(meta.get(key) or "")))
        ),
        None,
    )
    if doi:
        resolved = resolve(doi, config, use_local=use_local, use_remote=use_remote)
        if resolved:
            try:
                return select_candidate(doi, resolved, config)
            except LookupError:
                pass
    title = str(meta.get("/Title") or "").strip()
    author = str(meta.get("/Author") or "").strip()
    queries = [f"{author} {title}".strip()] if title else []
    filename_query = path.stem.replace("_", " ").strip()
    if filename_query and filename_query not in queries:
        queries.append(filename_query)
    for query in queries:
        resolved = resolve(query, config, use_local=use_local, use_remote=use_remote)
        if resolved:
            try:
                return select_candidate(query, resolved, config)
            except LookupError:
                pass
    # A PDF's creation date describes the file, not the publication.
    year = extract_year(filename_query)
    authors = []
    if author:
        parts = author.split()
        authors = [Person(family=parts[-1], given=" ".join(parts[:-1]))]
    return Article(title=title or path.stem, authors=authors, year=year, doi=doi, source="pdf")
