from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import TypeVar

from ..config import Config
from ..model import Article
from ..progress import _current, emit
from . import crossref, exports, openalex, pubmed, zolit, zolit_sbs, zotero
from .common import deduplicate, extract_doi, extract_pmid, is_local_pdf, local_metadata_config

logger = logging.getLogger(__name__)

LOCAL_SEARCHERS = (zotero.search, zolit.search, zolit_sbs.search, exports.search)
LOCAL_DOI_LOOKUPS = (zotero.by_doi, zolit.by_doi, zolit_sbs.by_doi, exports.by_doi)
LOCAL_PMID_LOOKUPS = (zotero.by_pmid, zolit.by_pmid, zolit_sbs.by_pmid, exports.by_pmid)


def _local_search(query: str, config: Config) -> list[Article]:
    articles: list[Article] = []
    for searcher in LOCAL_SEARCHERS:
        emit("resolve", f"Searching {searcher.__module__.rsplit('.', 1)[-1]}")
        try:
            articles.extend(searcher(query, config))
        except Exception:
            logger.debug("Resolver %s failed", searcher.__module__, exc_info=True)
            continue
    result = deduplicate(articles)
    result.sort(key=lambda a: a.score or 0.0, reverse=True)
    return result[: config.max_candidates]


def _local_by_doi(doi: str, config: Config) -> list[Article]:
    articles = []
    for lookup in LOCAL_DOI_LOOKUPS:
        emit("resolve", f"Looking up DOI in {lookup.__module__.rsplit('.', 1)[-1]}")
        try:
            article = lookup(doi, config)
            if article:
                articles.append(article)
        except Exception:
            logger.debug("DOI resolver %s failed", lookup.__module__, exc_info=True)
            continue
    return deduplicate(articles)


def _local_by_pmid(pmid: str, config: Config) -> list[Article]:
    articles = []
    for lookup in LOCAL_PMID_LOOKUPS:
        emit("resolve", f"Looking up PMID in {lookup.__module__.rsplit('.', 1)[-1]}")
        try:
            article = lookup(pmid, config)
            if article:
                articles.append(article)
        except Exception:
            logger.debug("PMID resolver %s failed", lookup.__module__, exc_info=True)
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


_Result = TypeVar("_Result")


def _remote_providers(
    providers: Sequence[Callable[[str, Config], _Result]],
    query: str,
    config: Config,
    detail: str,
) -> list[_Result]:
    """Run independent providers together and collect in stable provider order."""

    def invoke(provider: Callable[[str, Config], _Result]) -> _Result:
        # Each worker inherits its own context, including the command HTTP session.
        # Reporter owns terminal state and must only be called by the main thread.
        token = _current.set(None)
        try:
            return provider(query, config)
        finally:
            _current.reset(token)

    results = []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="papr-metadata") as pool:
        futures = []
        for provider in providers:
            emit("resolve", f"{detail} {provider.__module__.rsplit('.', 1)[-1]}")
            futures.append(pool.submit(copy_context().run, invoke, provider))
        for provider, future in zip(providers, futures, strict=True):
            try:
                results.append(future.result())
            except Exception:
                logger.debug("Resolver %s failed", provider.__module__, exc_info=True)
    return results


def _remote_search(query: str, config: Config) -> list[Article]:
    groups = _remote_providers((crossref.search, openalex.search), query, config, "Searching")
    articles = [article for group in groups for article in group]
    ranked = sorted(deduplicate(articles), key=lambda a: a.score or 0.0, reverse=True)
    if not _strong_local(ranked, config):
        emit("resolve", "Searching PubMed for additional candidates")
        try:
            articles.extend(pubmed.search(query, config))
        except Exception:
            logger.debug("PubMed search failed", exc_info=True)
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
    enrich: bool = False,
) -> list[Article]:
    # A hosted Zotero API is an external fallback, after all offline libraries.
    offline_config = local_metadata_config(config)
    doi = extract_doi(query)
    if doi:
        local = _local_by_doi(doi, offline_config) if use_local and config.local_sources else []
        if (
            use_local
            and config.local_sources
            and use_remote
            and offline_config is not config
            and (enrich or not any(is_local_pdf(article.local_pdf) for article in local))
        ):
            local = deduplicate([*local, *_local_by_doi(doi, config)])
        if not use_remote:
            return local
        if not enrich and any(is_local_pdf(a.local_pdf) for a in local):
            return local
        remote = [
            article
            for article in _remote_providers(
                (crossref.by_doi, openalex.by_doi), doi, config, "Looking up DOI in"
            )
            if article is not None
        ]
        return deduplicate([*local, *remote])

    pmid = extract_pmid(query)
    if pmid:
        local = _local_by_pmid(pmid, config) if use_local and config.local_sources else []
        if not use_remote or (not enrich and any(is_local_pdf(a.local_pdf) for a in local)):
            return local
        emit("resolve", "Looking up PMID in PubMed")
        try:
            article = pubmed.by_pmid(pmid, config)
            if article and article.doi:
                emit("resolve", "Enriching PubMed metadata with OpenAlex")
                try:
                    oa = openalex.by_doi(article.doi, config)
                    if oa:
                        article.merge(oa)
                except Exception:
                    logger.debug("OpenAlex PMID enrichment failed", exc_info=True)
            return deduplicate([*local, *([article] if article else [])])
        except Exception:
            logger.debug("PubMed resolver failed", exc_info=True)
            return local

    local: list[Article] = []
    if use_local and config.local_sources:
        local = _local_search(query, offline_config)
        if (
            use_remote
            and offline_config is not config
            and (enrich or not (_strong_local(local, config) and is_local_pdf(local[0].local_pdf)))
        ):
            local = deduplicate([*local, *_local_search(query, config)])
            local.sort(key=lambda article: article.score or 0.0, reverse=True)
            local = local[: config.max_candidates]
        if not use_remote:
            return local
        if _strong_local(local, config):
            best = local[0]
            if not enrich and (best.doi or is_local_pdf(best.local_pdf)):
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
