from __future__ import annotations

from .config import Config
from .model import Article
from .resolvers.common import extract_doi, extract_pmid, normalize_doi


def verified_identifier(query: str, article: Article) -> bool:
    doi = extract_doi(query)
    if doi:
        return normalize_doi(article.doi) == doi
    pmid = extract_pmid(query)
    return bool(pmid and article.pmid and str(article.pmid) == pmid)


def select_candidate(query: str, candidates: list[Article], config: Config) -> Article:
    """Accept verified identifiers or sufficiently confident, unambiguous searches."""
    if not candidates:
        raise LookupError(f"No bibliographic match for: {query}")
    for candidate in candidates:
        if verified_identifier(query, candidate):
            return candidate
    # Identifier lookups must actually return the requested identifier.
    if extract_doi(query) or extract_pmid(query):
        raise LookupError(f"No verified identifier match for: {query}")
    first = candidates[0].score or 0.0
    second = (candidates[1].score or 0.0) if len(candidates) > 1 else 0.0
    if first >= config.auto_accept_score and (
        len(candidates) == 1 or first - second >= config.auto_accept_margin
    ):
        return candidates[0]
    raise LookupError(f"Ambiguous or low-confidence reference: {query}")
