from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from ..config import Config
from ..http import client
from ..model import Article, Person
from .common import normalize_doi, score_article

BASE = "https://api.crossref.org/works"


def _publisher_url(item: dict[str, Any]) -> str | None:
    primary = (item.get("resource") or {}).get("primary") or {}
    url = primary.get("URL") or item.get("URL")
    if url:
        parsed = urlparse(url)
        if parsed.hostname == "linkinghub.elsevier.com" and parsed.path.startswith(
            "/retrieve/pii/"
        ):
            pii = parsed.path.removeprefix("/retrieve/pii/")
            if pii.startswith("S") and pii[1:].isdigit():
                return f"https://www.sciencedirect.com/science/article/pii/{pii}"
    return url


def _year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = item.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                pass
    return None


def from_item(item: dict[str, Any]) -> Article:
    authors = [
        Person(
            family=a.get("family", ""),
            given=a.get("given", ""),
            orcid=(a.get("ORCID") or "").removeprefix("https://orcid.org/") or None,
        )
        for a in item.get("author", [])
    ]
    links = item.get("link", []) or []
    pdf = next(
        (x.get("URL") for x in links if "pdf" in (x.get("content-type") or "").lower()),
        None,
    )
    return Article(
        title=(item.get("title") or [""])[0],
        authors=authors,
        year=_year(item),
        journal=(item.get("container-title") or [None])[0],
        volume=item.get("volume"),
        issue=item.get("issue"),
        pages=item.get("page") or item.get("article-number"),
        doi=normalize_doi(item.get("DOI")),
        url=_publisher_url(item),
        abstract=item.get("abstract"),
        item_type=item.get("type") or "article-journal",
        oa_pdf_url=pdf,
        source="crossref",
    )


def by_doi(doi: str, config: Config) -> Article | None:
    params = {"mailto": config.email} if config.email else {}
    with client() as c:
        r = c.get(f"{BASE}/{quote(doi, safe='')}", params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return from_item(r.json()["message"])


def search(query: str, config: Config) -> list[Article]:
    params: dict[str, str | int] = {
        "query.bibliographic": query,
        "rows": config.max_candidates,
    }
    if config.email:
        params["mailto"] = config.email
    with client() as c:
        r = c.get(BASE, params=params)
        r.raise_for_status()
        items = r.json()["message"]["items"]
    result = [from_item(item) for item in items]
    for article in result:
        article.score = score_article(query, article)
    return result
