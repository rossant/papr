"""Find published repository copies using an exact DOI, never a title guess."""

from __future__ import annotations

from urllib.parse import urlsplit

from ..config import Config
from ..http import client, remaining_time
from ..model import Article
from ..progress import emit
from ..resolvers.common import normalize_doi

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
