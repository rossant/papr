from __future__ import annotations

from typing import Any

from ..config import Config
from ..http import client
from ..model import Article, Person
from .common import normalize_doi, score_article

BASE = "https://api.openalex.org/works"


def from_item(item: dict[str, Any]) -> Article:
    authors = []
    for authorship in item.get("authorships", []):
        name = authorship.get("author", {}).get("display_name", "").strip()
        parts = name.split()
        authors.append(Person(family=parts[-1] if parts else "", given=" ".join(parts[:-1])))
    ids = item.get("ids") or {}
    location = item.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = item.get("biblio") or {}
    best = item.get("best_oa_location") or {}
    return Article(
        title=item.get("title") or item.get("display_name") or "",
        authors=authors,
        year=item.get("publication_year"),
        journal=source.get("display_name"),
        volume=biblio.get("volume"),
        issue=biblio.get("issue"),
        pages=_pages(biblio),
        doi=normalize_doi(ids.get("doi") or item.get("doi")),
        pmid=_strip_id(ids.get("pmid"), "https://pubmed.ncbi.nlm.nih.gov/"),
        pmcid=_strip_id(ids.get("pmcid"), "https://pmc.ncbi.nlm.nih.gov/articles/"),
        url=location.get("landing_page_url") or item.get("id"),
        oa_pdf_url=best.get("pdf_url") or location.get("pdf_url"),
        source="openalex",
    )


def _strip_id(value: str | None, prefix: str) -> str | None:
    return value.removeprefix(prefix).strip("/") if value else None


def _pages(biblio: dict[str, Any]) -> str | None:
    first, last = biblio.get("first_page"), biblio.get("last_page")
    if first and last and first != last:
        return f"{first}-{last}"
    return first or last


def by_doi(doi: str, config: Config) -> Article | None:
    params: dict[str, str] = {"filter": f"doi:{doi}"}
    if config.openalex_api_key:
        params["api_key"] = config.openalex_api_key
    with client() as c:
        r = c.get(BASE, params=params)
        r.raise_for_status()
        items = r.json().get("results", [])
    return from_item(items[0]) if items else None


def search(query: str, config: Config) -> list[Article]:
    params: dict[str, str | int] = {"search": query, "per-page": config.max_candidates}
    if config.openalex_api_key:
        params["api_key"] = config.openalex_api_key
    with client() as c:
        r = c.get(BASE, params=params)
        r.raise_for_status()
        items = r.json().get("results", [])
    result = [from_item(item) for item in items]
    for article in result:
        article.score = score_article(query, article)
    return result
