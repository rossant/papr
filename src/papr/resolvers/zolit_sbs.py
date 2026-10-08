from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import Config
from ..formats import csl
from ..model import Article, Person
from .common import extract_year, normalize_doi, score_article

TITLE_RE = re.compile(r"\x60([^\x60]+)\x60")


def find_domain(config: Config) -> Path | None:
    candidates: list[Path] = []
    if config.zolit_sbs_repo:
        candidates.append(config.zolit_sbs_repo / "domains" / "sbs")
    if config.zolit_repo:
        candidates.append(config.zolit_repo.parent / "zolit-sbs" / "domains" / "sbs")
    candidates.extend(
        [
            Path.home() / ".zolit" / "domains" / "sbs",
            Path.cwd().parent / "zolit-sbs" / "domains" / "sbs",
        ]
    )
    return next(
        (
            p
            for p in candidates
            if any((p / name).is_file() for name in ("references.csl.json", "key-publications.md"))
        ),
        None,
    )


def load_references(domain: Path) -> list[Article]:
    path = domain / "references.csl.json"
    if not path.exists():
        return parse_key_publications((domain / "key-publications.md").read_text(encoding="utf-8"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("references.csl.json must contain a list of CSL objects")
    articles = []
    for item in data:
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ValueError("Each structured reference requires a title")
        if item.get("papr-status") not in {None, "candidate", "verified"}:
            raise ValueError("papr-status must be candidate or verified")
        article = csl.decode(item)
        article.doi = normalize_doi(article.doi)
        article.source = "zolit-sbs"
        article.source_key = item.get("id")
        articles.append(article)
    return articles


def parse_key_publications(text: str) -> list[Article]:
    articles = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        title_match = TITLE_RE.search(stripped)
        year = extract_year(stripped)
        if not title_match or not year:
            continue
        prefix = stripped[2 : title_match.start()].strip(" ,")
        authors = [
            Person(family=name.strip())
            for name in prefix.split(",")
            if name.strip() and len(name.strip().split()) <= 4
        ]
        if not authors:
            continue
        articles.append(
            Article(
                title=title_match.group(1).strip(),
                authors=authors,
                year=year,
                item_type="document",
                source="zolit-sbs",
            )
        )
    return articles


def search(query: str, config: Config) -> list[Article]:
    domain = find_domain(config)
    if not domain:
        return []
    try:
        articles = load_references(domain)
    except (OSError, ValueError, TypeError):
        return []
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def by_doi(doi: str, config: Config) -> Article | None:
    target = normalize_doi(doi)
    domain = find_domain(config)
    if not target or not domain:
        return None
    try:
        articles = load_references(domain)
    except (OSError, ValueError, TypeError):
        return None
    for article in articles:
        if article.doi == target:
            article.score = 1.0
            return article
    return None
