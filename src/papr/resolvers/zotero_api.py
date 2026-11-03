"""Read-only Zotero metadata and local attachment discovery over HTTP."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from ..config import Config
from ..model import Article
from ..progress import emit
from .common import is_local_pdf, normalize_doi, score_article
from .zotero_metadata import from_api_item

logger = logging.getLogger(__name__)


API_BUDGET_SECONDS = 3.0


def _file_url_to_path(value: str) -> Path | None:
    parsed = urlparse(value.strip())
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def _api_get(client: httpx.Client, url: str, deadline: float, **kwargs) -> httpx.Response:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx.TimeoutException("Zotero API lookup time budget exceeded")
    return client.get(url, timeout=min(1.0, remaining), **kwargs)


def _library_prefix(config: Config) -> str:
    group_id = getattr(config, "zotero_group_id", None)
    return f"/groups/{group_id}" if group_id is not None else "/users/0"


def _api_attachment(
    client: httpx.Client,
    base: str,
    item_key: str,
    deadline: float,
    *,
    prefix: str = "/users/0",
) -> str | None:
    r = _api_get(
        client,
        f"{base}{prefix}/items/{item_key}/children",
        deadline,
        params={"itemType": "attachment", "format": "json"},
    )
    if r.status_code >= 400:
        return None
    for child in r.json():
        data = child.get("data", child)
        if data.get("deleted"):
            continue
        filename = data.get("filename") or ""
        content_type = data.get("contentType") or ""
        if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        key = child.get("key") or data.get("key")
        if not key:
            continue
        file_r = _api_get(client, f"{base}{prefix}/items/{key}/file/view/url", deadline)
        if file_r.status_code >= 400:
            continue
        path = _file_url_to_path(file_r.text)
        if path and is_local_pdf(path):
            return str(path)
    return None


def articles(query: str, config: Config, qmode: str) -> list[Article]:
    base = config.zotero_api_url.rstrip("/")
    if not base:
        return []
    prefix = _library_prefix(config)
    headers = {"Zotero-API-Version": "3"}
    deadline = time.monotonic() + API_BUDGET_SECONDS
    group_id = getattr(config, "zotero_group_id", None)
    label = f"Zotero group {group_id}" if group_id is not None else "Zotero desktop"
    emit("resolve", f"Querying {label} API")
    with httpx.Client(timeout=1.0, headers=headers) as client:
        r = _api_get(
            client,
            f"{base}{prefix}/items/top",
            deadline,
            params={
                "q": query,
                "qmode": qmode,
                "format": "json",
                "limit": max(20, config.max_candidates * 4),
            },
        )
        if r.status_code in {403, 404}:
            reason = "disabled in Zotero" if r.status_code == 403 else "unavailable"
            emit("resolve", f"Zotero desktop API {reason}; trying local database")
            return []
        r.raise_for_status()
        candidates = []
        for item in r.json():
            key = item.get("key") or item.get("data", {}).get("key")
            article = from_api_item(item)
            if article:
                if group_id is not None:
                    article.source_library = f"group:{group_id}"
                article.score = score_article(query, article)
                if qmode == "everything":
                    if article.doi != normalize_doi(query):
                        continue
                    article.score = 1.0
                elif article.score < 0.35:
                    continue
                candidates.append((key, article))
        candidates.sort(key=lambda pair: pair[1].score or 0.0, reverse=True)
        candidates = candidates[: config.max_candidates]
        for index, (key, article) in enumerate(candidates, start=1):
            if not key:
                continue
            emit("resolve", f"Checking Zotero PDF {index}/{len(candidates)}")
            try:
                article.local_pdf = _api_attachment(client, base, key, deadline, prefix=prefix)
            except httpx.HTTPError:
                # Keep useful metadata even when attachment requests are unavailable.
                logger.debug("Zotero attachment lookup failed", exc_info=True)
                if time.monotonic() >= deadline:
                    break
    return [article for _, article in candidates]
