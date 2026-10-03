from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..model import Article, Person
from .common import extract_year, score_article

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
    return next((p for p in candidates if (p / "key-publications.md").exists()), None)


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
                source="zolit-sbs",
            )
        )
    return articles


def search(query: str, config: Config) -> list[Article]:
    domain = find_domain(config)
    if not domain:
        return []
    try:
        articles = parse_key_publications(
            (domain / "key-publications.md").read_text(encoding="utf-8")
        )
    except OSError:
        return []
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]
