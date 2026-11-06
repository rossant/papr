"""Find published repository copies using an exact DOI, never a title guess."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..acquisition import record_attempt
from ..config import Config
from ..http import client, remaining_time
from ..model import Article
from ..progress import emit
from ..resolvers.common import normalize_doi
from .candidates import _supplement

BASE = "https://api.openalex.org/works"


def _public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
        ):
            return value
    except ValueError:
        pass
    return None


def repository_urls(article: Article, config: Config) -> list[str]:
    """Discover repository PDFs and landing pages, preserving the journal version.

    Local bibliography matches often contain no remote locations. Querying here
    makes those locations available only after the already known URLs fail.
    Accepted/submitted manuscripts and other works are deliberately excluded.
    """
    doi = normalize_doi(article.doi)
    if not doi:
        return []
    emit("fetch", "Searching repositories for the published article")
    params = {"filter": f"doi:{doi}"}
    if config.openalex_api_key:
        params["api_key"] = config.openalex_api_key
    with client() as c:
        response = c.get(BASE, params=params, timeout=remaining_time())
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("OpenAlex returned invalid repository discovery data")
    urls = []
    for work in data["results"]:
        if not isinstance(work, dict):
            continue
        ids = work.get("ids") or {}
        found = work.get("doi") or (ids.get("doi") if isinstance(ids, dict) else None)
        if not isinstance(found, str) or normalize_doi(found) != doi:
            continue
        locations = work.get("locations") or []
        if not isinstance(locations, list):
            continue
        for location in locations:
            if not isinstance(location, dict):
                continue
            source = location.get("source") or {}
            if (
                not isinstance(source, dict) or source.get("type") != "repository"
                or location.get("version") != "publishedVersion"
            ):
                continue
            for value in (location.get("pdf_url"), location.get("landing_page_url")):
                url = _public_url(value)
                if url and url not in urls:
                    urls.append(url)
    return urls


def nva_pdf_urls(c, landing_url: str, article: Article | None) -> list[str]:
    """Use NVA's public API for an exact-DOI, openly downloadable published file.

    The registration UI renders download links with JavaScript. File-link responses
    contain short-lived signed URLs; never record those query parameters.
    """
    parsed = urlsplit(landing_url)
    identifier = re.fullmatch(
        r"/registration/([0-9a-f]{12}-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?",
        parsed.path,
    )
    doi = normalize_doi(article.doi) if article else None
    if parsed.hostname != "nva.sikt.no" or parsed.scheme != "https" or not identifier or not doi:
        return []
    if parsed.username is not None or parsed.password is not None:
        return []
    api = f"https://api.nva.unit.no/publication/{identifier[1]}"
    response = c.get(api, headers={"Accept": "application/json"}, timeout=remaining_time())
    response.raise_for_status()
    try:
        record = response.json()
    except ValueError:
        record_attempt("repository_api", api, reason="NVA API returned invalid JSON")
        return []
    if not isinstance(record, dict) or record.get("status") != "PUBLISHED":
        return []
    description = record.get("entityDescription") or {}
    reference = description.get("reference") if isinstance(description, dict) else None
    found = reference.get("doi") if isinstance(reference, dict) else None
    if not isinstance(found, str) or normalize_doi(found) != doi:
        record_attempt("repository_api", api,
                       reason="NVA record DOI does not match requested article")
        return []
    artifacts = record.get("associatedArtifacts") or []
    if not isinstance(artifacts, list):
        return []
    urls = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        file_id = artifact.get("identifier")
        operations = artifact.get("allowedOperations")
        name = artifact.get("name")
        if (
            artifact.get("type") != "OpenFile"
            or artifact.get("mimeType") != "application/pdf"
            or not isinstance(name, str) or _supplement(name)
            or artifact.get("publisherVersion") != "PublishedVersion"
            or not isinstance(operations, list) or "download" not in operations
            or not isinstance(file_id, str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", file_id)
        ):
            continue
        link = c.get(f"{api}/filelink/{file_id}", headers={"Accept": "application/json"},
                     timeout=remaining_time())
        link.raise_for_status()
        try:
            payload = link.json()
        except ValueError:
            continue
        url = _public_url(payload.get("id")) if isinstance(payload, dict) else None
        if url and urlsplit(url).scheme == "https" and url not in urls:
            urls.append(url)
        if len(urls) == 8:
            break
    record_attempt("repository_api", api, outcome="discovered" if urls else "unavailable",
                   reason="NVA published PDF locations found" if urls else
                   "No openly downloadable published PDF in the matching NVA record")
    return urls
