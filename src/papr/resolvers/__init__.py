from __future__ import annotations

from ..config import Config
from ..model import Article
from . import crossref, openalex, pubmed
from .common import deduplicate, extract_doi, extract_pmid


def resolve(query: str, config: Config) -> list[Article]:
    doi = extract_doi(query)
    if doi:
        articles = []
        for fn in (crossref.by_doi, openalex.by_doi):
            try:
                item = fn(doi, config)
                if item:
                    articles.append(item)
            except Exception:
                continue
        return deduplicate(articles)

    pmid = extract_pmid(query)
    if pmid:
        try:
            article = pubmed.by_pmid(pmid, config)
            if article and article.doi:
                try:
                    oa = openalex.by_doi(article.doi, config)
                    if oa:
                        article.merge(oa)
                except Exception:
                    pass
            return [article] if article else []
        except Exception:
            return []

    articles: list[Article] = []
    for fn in (crossref.search, openalex.search):
        try:
            articles.extend(fn(query, config))
        except Exception:
            continue
    result = deduplicate(articles)
    result.sort(key=lambda a: a.score or 0.0, reverse=True)
    return result[: config.max_candidates]
