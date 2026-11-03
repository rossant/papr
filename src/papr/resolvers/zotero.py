"""Resolve references from offline Zotero libraries, then the configured API."""

from __future__ import annotations

import logging
import sqlite3

import httpx

from ..config import Config
from ..model import Article
from ..progress import emit
from . import zotero_api, zotero_sqlite
from .common import deduplicate, is_local_pdf, normalize_doi, score_article
from .zotero_metadata import from_api_item as from_api_item

logger = logging.getLogger(__name__)


def search_api(query: str, config: Config) -> list[Article]:
    articles = zotero_api.articles(query, config, "titleCreatorYear")
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def search_sqlite(query: str, config: Config) -> list[Article]:
    articles = zotero_sqlite.all_articles(config)
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def _sqlite_matches(config: Config, field: str, target: str) -> list[Article]:
    try:
        matches = [
            article for article in zotero_sqlite.all_articles(config)
            if getattr(article, field) == target
        ]
    except (sqlite3.Error, OSError):
        emit("resolve", "Zotero database unavailable; continuing with other sources")
        logger.debug("Zotero SQLite lookup failed", exc_info=True)
        return []
    for article in matches:
        article.score = 1.0
    return matches


def by_doi(doi: str, config: Config) -> Article | None:
    target = normalize_doi(doi)
    if not target:
        return None
    local = _sqlite_matches(config, "doi", target)
    attached = next((article for article in local if is_local_pdf(article.local_pdf)), None)
    if attached:
        return attached
    try:
        remote = zotero_api.articles(target, config, "everything")
        matches = deduplicate([*remote, *local])
        if matches:
            return matches[0]
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("Zotero API unavailable; using SQLite", exc_info=True)
    return local[0] if local else None


def by_pmid(pmid: str, config: Config) -> Article | None:
    matches = _sqlite_matches(config, "pmid", pmid)
    return next(
        (article for article in matches if is_local_pdf(article.local_pdf)),
        matches[0] if matches else None,
    )


def search(query: str, config: Config) -> list[Article]:
    try:
        local = search_sqlite(query, config)
    except (sqlite3.Error, OSError):
        emit("resolve", "Zotero database unavailable; continuing with other sources")
        logger.debug("Zotero SQLite search failed", exc_info=True)
        local = []
    # Keep all candidates for the normal ambiguity policy in the resolver/chooser.
    if any(is_local_pdf(article.local_pdf) for article in local):
        return local
    try:
        result = deduplicate([*search_api(query, config), *local])
        result.sort(key=lambda article: article.score or 0.0, reverse=True)
        return result[: config.max_candidates]
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("Zotero API unavailable; using SQLite", exc_info=True)
    return local
