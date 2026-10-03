from __future__ import annotations

import re
from collections.abc import Iterable

from rapidfuzz.fuzz import ratio, token_set_ratio

from ..model import Article

DOI_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.I,
)
PMID_RE = re.compile(r"(?:pmid:\s*)?(\d{5,10})$", re.I)
YEAR_RE = re.compile(r"\b(18|19|20|21)\d{2}\b")


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    match = DOI_RE.search(value.strip())
    return match.group(1).rstrip(".,;)]").lower() if match else None


def extract_doi(query: str) -> str | None:
    return normalize_doi(query)


def extract_pmid(query: str) -> str | None:
    q = query.strip()
    if q.lower().startswith("pmid:"):
        match = PMID_RE.search(q)
        return match.group(1) if match else None
    return None


def score_article(query: str, article: Article) -> float:
    q = query.casefold()
    title_score = token_set_ratio(q, article.title.casefold()) / 100.0 if article.title else 0.0
    author_score = 0.0
    if article.authors:
        author_score = max(
            ratio(q.split()[0] if q.split() else "", a.family.casefold()) / 100.0
            for a in article.authors[:3]
        )
    years = [int(m.group(0)) for m in YEAR_RE.finditer(query)]
    year_score = 1.0 if article.year and article.year in years else (0.45 if not years else 0.0)
    doi_bonus = 1.0 if article.doi and article.doi.casefold() in q else 0.0
    return min(1.0, 0.60 * title_score + 0.20 * author_score + 0.15 * year_score + 0.05 * doi_bonus)


def deduplicate(articles: Iterable[Article]) -> list[Article]:
    out: list[Article] = []
    by_key: dict[str, Article] = {}
    for article in articles:
        key = article.doi or f"{article.title.casefold()}|{article.year or ''}"
        if key in by_key:
            by_key[key].merge(article)
            if (article.score or 0) > (by_key[key].score or 0):
                by_key[key].score = article.score
            continue
        by_key[key] = article
        out.append(article)
    return out
