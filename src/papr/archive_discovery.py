"""Opt-in discovery of public archive records related to a journal article.

Suggestions from this module are leads for a person to inspect. They are never
identity verification and never cause a PDF to be downloaded or cached.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Config
from .http import client

BASE = "https://api.openalex.org/works"
_DOI_INPUT = re.compile(
    r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s?#]+)$", re.I
)


def _doi(value: str) -> str:
    match = _DOI_INPUT.fullmatch(value.strip())
    if not match:
        raise ValueError("Discovery requires one valid DOI")
    return match.group(1).lower()


def _record_doi(record: dict[str, Any]) -> str | None:
    ids = record.get("ids")
    ids = ids if isinstance(ids, dict) else {}
    value = ids.get("doi") or record.get("doi")
    if not isinstance(value, str):
        return None
    match = _DOI_INPUT.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def _public_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password
        ):
            return None
        # Query strings and fragments can contain access tokens or session IDs.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return None


def _record_url(record: dict[str, Any]) -> str | None:
    location = record.get("primary_location")
    location = location if isinstance(location, dict) else {}
    return _public_url(location.get("landing_page_url") or record.get("id"))


def _candidate(record: dict[str, Any], *, reason: str) -> dict[str, Any] | None:
    url = _record_url(record)
    if not url:
        return None
    return {
        "url": url,
        "type": record.get("type") or "record",
        "title": record.get("title") or record.get("display_name") or "(untitled record)",
        "reason": reason,
        "verification": "unverified",
    }


def _authorships(record: dict[str, Any]) -> list[dict[str, Any]]:
    values = record.get("authorships")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _author_ids(record: dict[str, Any]) -> list[str]:
    output = []
    for authorship in _authorships(record):
        author = authorship.get("author")
        author_id = author.get("id") if isinstance(author, dict) else None
        if isinstance(author_id, str) and re.fullmatch(r"https://openalex\.org/A\d+", author_id):
            output.append(author_id.rsplit("/", 1)[-1])
    return list(dict.fromkeys(output))


def discover(doi: str, config: Config) -> list[dict[str, Any]]:
    """Find possible public records for an exact DOI and related theses."""
    identifier = _doi(doi)  # Validate before opening a client or making a request.
    params: dict[str, str | int] = {"filter": f"doi:{identifier}", "per-page": 5}
    if config.openalex_api_key:
        params["api_key"] = config.openalex_api_key
    with client() as http:
        response = http.get(BASE, params=params)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("Archive discovery returned invalid metadata") from None
        if not isinstance(payload, dict):
            raise ValueError("Archive discovery returned invalid metadata")
        records = payload.get("results", [])

    records = records if isinstance(records, list) else []
    matching = [r for r in records if isinstance(r, dict) and _record_doi(r) == identifier]
    if not matching:
        return []
    article = matching[0]
    title = article.get("title") or article.get("display_name") or ""
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        return []

    results: list[dict[str, Any]] = []
    locations = article.get("locations")
    locations = locations if isinstance(locations, list) else []
    for location in locations:
        if not isinstance(location, dict):
            continue
        source = location.get("source")
        source = source if isinstance(source, dict) else {}
        # Include the public record URL, never a PDF URL.
        record = {
            "id": location.get("landing_page_url"),
            "type": source.get("type") or "article location",
            "title": title,
            "primary_location": {"landing_page_url": location.get("landing_page_url")},
        }
        candidate = _candidate(record, reason="OpenAlex location attached to the exact DOI record")
        if candidate and candidate["url"] not in {c["url"] for c in results}:
            results.append(candidate)

    # Dissertations can have titles that differ completely from papers they
    # contain. Search bounded same-author records and rank any bibliographic
    # links, while clearly leaving identity and page selection to the user.
    article_author_ids = _author_ids(article)
    article_id = article.get("id") if isinstance(article.get("id"), str) else ""
    for author_id in article_author_ids[:3]:
        thesis_params: dict[str, str | int] = {
            "filter": f"type:dissertation,authorships.author.id:{author_id}",
            "per-page": 5,
        }
        if config.openalex_api_key:
            thesis_params["api_key"] = config.openalex_api_key
        try:
            with client() as http:
                response = http.get(BASE, params=thesis_params)
                response.raise_for_status()
                payload = response.json()
            theses = payload.get("results", []) if isinstance(payload, dict) else []
        except httpx.HTTPError:
            continue
        if not isinstance(theses, list):
            continue
        for thesis in theses:
            if (
                not isinstance(thesis, dict) or thesis.get("type") != "dissertation"
                or author_id not in _author_ids(thesis)
            ):
                continue
            referenced = thesis.get("referenced_works")
            referenced = referenced if isinstance(referenced, list) else []
            cited = bool(article_id and article_id in referenced)
            abstract = thesis.get("abstract_inverted_index")
            abstract_match = False
            if isinstance(abstract, dict) and title:
                terms = {word for word in re.findall(r"[\w'-]+", title.casefold()) if len(word) > 3}
                indexed = {str(word).casefold() for word in abstract}
                abstract_match = bool(terms) and terms.issubset(indexed)
            thesis_title = str(thesis.get("title") or thesis.get("display_name") or "")
            title_match = bool(title and title.casefold() in thesis_title.casefold())
            if cited:
                reason = "Same OpenAlex author; dissertation cites the exact article record"
            elif title_match:
                reason = "Same OpenAlex author; article title appears in dissertation title"
            elif abstract_match:
                reason = "Same OpenAlex author; article title terms appear in dissertation abstract"
            else:
                reason = (
                    "OpenAlex lists the article author on this dissertation; relation unconfirmed"
                )
            candidate = _candidate(thesis, reason=reason)
            if candidate:
                candidate["type"] = "dissertation"
                candidate["relatedness"] = (
                    "strong" if cited or title_match or abstract_match else "author-only"
                )
                if candidate["url"] not in {c["url"] for c in results}:
                    results.append(candidate)
    return results
