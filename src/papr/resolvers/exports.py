"""Resolve previously delivered PDFs through their recorded metadata and checksum."""

from __future__ import annotations

import logging
from pathlib import Path

from ..artifacts import cache_path, checksum, locate
from ..batch_catalog import known_manifest_paths
from ..batches import load_manifest
from ..config import Config
from ..model import Article, article_from_dict
from ..pdf import doi_tokens, validate_pdf
from .common import score_article
from .session import cached_library

logger = logging.getLogger(__name__)


def _doi(value: str | None) -> str | None:
    tokens = doi_tokens(value or "")
    return next(iter(tokens)) if len(tokens) == 1 else None


def _load(config: Config) -> list[Article]:
    try:
        paths = known_manifest_paths(config)
    except (OSError, ValueError):
        logger.debug("Papr export catalog unavailable", exc_info=True)
        return []
    articles = []
    validated: set[tuple[str, str, str, str]] = set()
    manifests = []
    for manifest in paths:
        try:
            data = load_manifest(manifest)
        except (OSError, ValueError):
            logger.debug("Papr export manifest unavailable: %s", manifest, exc_info=True)
            continue
        created = data.get("created_at")
        manifests.append((created if isinstance(created, str) else "", data))
    # A corrected/recovered original should supersede an earlier export of the
    # same work. Prefer the recorded delivery time, never filesystem timestamps.
    for _, data in sorted(manifests, key=lambda entry: entry[0], reverse=True):
        for row in data["items"]:
            # Pending outputs and previews were never published. An item may
            # still have delivered a PDF before a later converter/Zotero failure.
            if row["status"] not in {"ok", "error"} or "article" not in row:
                continue
            article = article_from_dict(row["article"])
            for artifact in row.get("artifacts", []):
                if not artifact["name"].lower().endswith(".pdf"):
                    continue
                key = (artifact["sha256"], article.doi or "", article.pmid or "", article.title)
                if key in validated:
                    continue
                try:
                    try:
                        pdf = locate(Path(artifact["path"]), artifact["sha256"], config)
                    except ValueError:
                        # An overwritten export must not hide a still verified
                        # cache copy. Reuse that copy without modifying the user's file.
                        pdf = cache_path(artifact["sha256"], config)
                        if not pdf.is_file() or checksum(pdf) != artifact["sha256"]:
                            continue
                    validate_pdf(pdf, article)
                except (OSError, ValueError):
                    logger.debug("Papr exported PDF unavailable: %s", artifact["path"],
                                 exc_info=True)
                    continue
                validated.add(key)
                article.local_pdf = str(pdf)
                article.source = "papr-exports"
                article.score = None
                articles.append(article)
                break
    # Keep complete identifiers: the general bibliographic normalizer accepts
    # DOI prefixes, which must not merge distinct delivered records here.
    unique: dict[tuple, Article] = {}
    for article in articles:
        key = (
            ("doi", _doi(article.doi)) if _doi(article.doi) else
            ("pmid", article.pmid) if article.pmid else
            ("citation", article.title.casefold(), article.year,
             tuple(person.family.casefold() for person in article.authors))
        )
        if key in unique:
            unique[key].merge(article)
        else:
            unique[key] = article
    return list(unique.values())


def all_articles(config: Config) -> list[Article]:
    """Read and verify delivered artifacts once during a command's resolution session."""
    key = ("papr-exports", str(config.data_dir), str(config.cache_dir), str(config.download_dir))
    return cached_library(key, lambda: _load(config))


def by_doi(doi: str, config: Config) -> Article | None:
    target = _doi(doi)
    if not target:
        return None
    for article in all_articles(config):
        if _doi(article.doi) == target:
            article.score = 1.0
            return article
    return None


def by_pmid(pmid: str, config: Config) -> Article | None:
    if not pmid or not pmid.isdigit():
        return None
    for article in all_articles(config):
        if article.pmid == pmid:
            article.score = 1.0
            return article
    return None


def search(query: str, config: Config) -> list[Article]:
    articles = all_articles(config)
    for article in articles:
        article.score = score_article(query, article)
    matches = [article for article in articles if (article.score or 0.0) >= 0.35]
    matches.sort(key=lambda article: article.score or 0.0, reverse=True)
    return matches[:config.max_candidates]
