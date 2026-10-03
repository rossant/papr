from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from .config import Config
from .model import Article, Person
from .resolvers import resolve
from .resolvers.common import extract_doi


def article_from_pdf(path: Path, config: Config) -> Article:
    reader = PdfReader(str(path))
    meta = reader.metadata or {}
    chunks = []
    for page in reader.pages[:2]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            pass
    text = "\n".join(chunks)
    doi = extract_doi(text)
    if doi:
        resolved = resolve(doi, config)
        if resolved:
            return resolved[0]
    title = str(meta.get("/Title") or "").strip()
    author = str(meta.get("/Author") or "").strip()
    if title:
        query = f"{author} {title}".strip()
        resolved = resolve(query, config)
        if resolved and (resolved[0].score or 0) >= 0.65:
            return resolved[0]
    year = None
    creation = str(meta.get("/CreationDate") or "")
    m = re.search(r"(?:19|20)\d{2}", creation)
    if m:
        year = int(m.group())
    authors = []
    if author:
        parts = author.split()
        authors = [Person(family=parts[-1], given=" ".join(parts[:-1]))]
    return Article(title=title or path.stem, authors=authors, year=year, source="pdf")
