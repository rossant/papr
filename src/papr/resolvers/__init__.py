from __future__ import annotations

from ..config import Config
from ..model import Article
from . import crossref, openalex, pubmed, zolit, zolit_sbs, zotero
from .common import deduplicate, extract_doi, extract_pmid

LOCAL_SEARCHERS = (zotero.search, zolit.search, zolit_sbs.search)
LOCAL_DOI_LOOKUPS = (zotero.by_doi, zolit.by_doi)


def _local_search(query: str, config: Config) -> list[Article]:
    articles: list[Article] = []
    for searcher in LOCAL_SEARCHERS:
        try:
            articles.extend(searcher(query, config))
        except Exception:
            continue
    result = deduplicate(articles)
    result.sort(key=lambda a: a.score or 0.0, reverse=True)
    return result[: config.max_candidates]


def _local_by_doi(doi: str, config: Config) -> list[Article]:
    articles = []
    for lookup in LOCAL_DOI_LOOKUPS:
        try:
            article = lookup(doi, config)
            if article:
                articles.append(article)
        except Exception:
            continue
    return deduplicate(articles)


def _strong_local(candidates: list[Article], config: Config) -> bool:
    if not candidates:
        return False
    first = candidates[0].score or 0.0
    if first < config.auto_accept_score:
        return False
    if len(candidates) == 1:
        return True
    second = candidates[1].score or 0.0
    return first - second >= config.auto_accept_margin


def _remote_search(query: str, config: Config) -> list[Article]:
    articles: list[Article] = []
    for searcher in (crossref.search, openalex.search):
        try:
            articles.extend(searcher(query, config))
        except Exception:
            continue
    return articles


def _expanded_query(article: Article) -> str:
    parts = [article.first_creator]
    if article.year:
        parts.append(str(article.year))
    parts.append(article.title)
    return " ".join(parts)


def resolve(
    query: str,
    config: Config,
    *,
    use_local: bool = True,
    use_remote: bool = True,
) -> list[Article]:
    doi = extract_doi(query)
    if doi:
        local = _local_by_doi(doi, config) if use_local and config.local_sources else []
        if not use_remote:
            return local
        remote = []
        for fn in (crossref.by_doi, openalex.by_doi):
            try:
                item = fn(doi, config)
                if item:
                    remote.append(item)
            except Exception:
                continue
        return deduplicate([*local, *remote])

    pmid = extract_pmid(query)
    if pmid:
        if not use_remote:
            return []
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

    local: list[Article] = []
    if use_local and config.local_sources:
        local = _local_search(query, config)
        if not use_remote:
            return local
        if _strong_local(local, config):
            best = local[0]
            if best.doi or best.local_pdf:
                return local
            remote = _remote_search(_expanded_query(best), config)
            result = deduplicate([*local, *remote])
            result.sort(key=lambda a: a.score or 0.0, reverse=True)
            return result[: config.max_candidates]

    if not use_remote:
        return local
    remote = _remote_search(query, config)
    result = deduplicate([*local, *remote])
    result.sort(key=lambda a: a.score or 0.0, reverse=True)
    return result[: config.max_candidates]
