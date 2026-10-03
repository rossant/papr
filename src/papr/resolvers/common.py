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
WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    match = DOI_RE.search(value.strip())
    if not match:
        return None
    doi = match.group(1).rstrip(".,;")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1].rstrip(".,;")
    return doi.lower()


def extract_doi(query: str) -> str | None:
    return normalize_doi(query)


def extract_pmid(query: str) -> str | None:
    q = query.strip()
    if q.lower().startswith("pmid:"):
        match = PMID_RE.search(q)
        return match.group(1) if match else None
    return None


def extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = YEAR_RE.search(value)
    return int(match.group(0)) if match else None


def score_article(query: str, article: Article) -> float:
    q = query.casefold()
    words = [w.casefold() for w in WORD_RE.findall(query)]
    years = [int(m.group(0)) for m in YEAR_RE.finditer(query)]
    non_year_words = [w for w in words if not w.isdigit()]

    title_words = [w.casefold() for w in WORD_RE.findall(article.title)]
    if title_words and (
        words == title_words
        or (
            years
            and article.year in years
            and non_year_words == [w for w in title_words if not w.isdigit()]
        )
    ):
        return 0.95

    title_score = token_set_ratio(q, article.title.casefold()) / 100.0 if article.title else 0.0
    author_score = 0.0
    if article.authors and non_year_words:
        author_score = max(
            (
                ratio(word, author.family.casefold()) / 100.0
                for word in non_year_words
                for author in article.authors[:3]
                if author.family
            ),
            default=0.0,
        )
    year_score = 1.0 if article.year and article.year in years else (0.45 if not years else 0.0)
    doi_bonus = 1.0 if article.doi and article.doi.casefold() in q else 0.0

    if years and len(non_year_words) <= 3:
        score = 0.64 * author_score + 0.30 * year_score + 0.06 * title_score
    else:
        score = 0.58 * title_score + 0.20 * author_score + 0.17 * year_score
        score += 0.05 * doi_bonus
    return min(1.0, score)


def deduplicate(articles: Iterable[Article]) -> list[Article]:
    out: list[Article] = []
    for article in articles:
        article.doi = normalize_doi(article.doi) or article.doi
        title = " ".join(WORD_RE.findall(article.title.casefold()))
        duplicate = next(
            (
                existing
                for existing in out
                if (article.doi and existing.doi == article.doi)
                or (
                    title
                    and title == " ".join(WORD_RE.findall(existing.title.casefold()))
                    and article.year == existing.year
                    and not (article.doi and existing.doi and article.doi != existing.doi)
                )
            ),
            None,
        )
        if duplicate is not None:
            duplicate.merge(article)
            if (article.score or 0) > (duplicate.score or 0):
                duplicate.score = article.score
            continue
        out.append(article)
    return out
