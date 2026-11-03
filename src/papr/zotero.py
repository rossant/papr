"""Reconcile bibliography entries, collections, and PDFs through the desktop API."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from . import zotero_attachments
from .config import Config
from .model import Article
from .progress import emit
from .resolvers.common import extract_year, normalize_doi
from .zotero_api import (
    DesktopAPI,
    ZoteroError,
    item_data,
    list_response,
    object_key,
)


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _find_parent(api: DesktopAPI, article: Article, *, pdf_md5: str | None = None) -> dict | None:
    doi = normalize_doi(article.doi)

    def query(value: str) -> list[dict]:
        return list_response(api.request(
            "GET", f"{api.prefix}/items/top",
            params={"q": value, "qmode": "everything", "format": "json"},
        ))

    def unique(matches: list[dict]) -> dict | None:
        if len(matches) > 1:
            raise ZoteroError(
                "Multiple Zotero items match this paper; merge the duplicates before saving"
            )
        return matches[0] if matches else None

    if doi:
        matched = unique([
            item for item in query(doi)
            if normalize_doi(item_data(item).get("DOI")) == doi
            and item_data(item).get("itemType") not in {"attachment", "note", "annotation"}
        ])
        if matched:
            return matched

    expected_authors = [_normalized(person.family) for person in article.authors]
    if not _normalized(article.title):
        return None
    incomplete = article.year is None or not expected_authors or not all(expected_authors)
    matches = []
    potential = []
    for item in query(article.title):
        data = item_data(item)
        if data.get("itemType") in {"attachment", "note", "annotation"}:
            continue
        if _normalized(data.get("title", "")) != _normalized(article.title):
            continue
        existing_doi = normalize_doi(data.get("DOI"))
        if doi and existing_doi and existing_doi != doi:
            continue
        existing_year = extract_year(data.get("date"))
        if article.year is not None and existing_year is not None and article.year != existing_year:
            continue
        if incomplete:
            potential.append(item)
            # A content hash proves identity when bibliographic fields cannot do so.
            if pdf_md5 and zotero_attachments.has_pdf(api, object_key(item), pdf_md5):
                matches.append(item)
            continue
        creators = data.get("creators", [])
        if not isinstance(creators, list) or not all(
            isinstance(person, dict) for person in creators
        ):
            raise ZoteroError("Zotero returned invalid creator data")
        actual_authors = [
            _normalized(person.get("lastName") or person.get("name") or "")
            for person in creators if person.get("creatorType") == "author"
        ]
        if (
            existing_year == article.year
            and actual_authors == expected_authors
        ):
            matches.append(item)
    matched = unique(matches)
    if incomplete and potential and matched is None:
        raise ZoteroError(
            "Incomplete metadata and an existing Zotero title could describe different papers. "
            "Resolve the metadata or existing entry before saving; the PDF is preserved"
        )
    return matched


def _collection(api: DesktopAPI, requested: str | None) -> tuple[str | None, str | None]:
    if not requested:
        return None, None
    collections = list_response(api.request(
        "GET", f"{api.prefix}/collections", params={"format": "json"}
    ))
    matches = [item for item in collections if object_key(item) == requested]
    if not matches:
        matches = [item for item in collections if item_data(item).get("name") == requested]
    if len(matches) > 1:
        raise ZoteroError("Multiple Zotero collections have that name; specify a collection key")
    item = matches[0] if matches else api.create(
        f"{api.prefix}/collections", {"name": requested, "parentCollection": False}
    )
    return object_key(item), item_data(item).get("name", requested)


def _bibliography(article: Article, collection_key: str | None) -> dict:
    # Article primarily represents scholarly journal papers. Keep unsupported types in extra.
    data = {
        "itemType": "journalArticle", "title": article.title,
        "creators": [
            {"creatorType": "author", "firstName": person.given, "lastName": person.family}
            for person in article.authors
        ],
        "tags": [], "collections": [collection_key] if collection_key else [], "relations": {},
    }
    fields = {
        "date": str(article.year) if article.year else None,
        "publicationTitle": article.journal, "volume": article.volume, "issue": article.issue,
        "pages": article.pages, "DOI": normalize_doi(article.doi), "url": article.url,
        "abstractNote": article.abstract,
    }
    data.update({name: value for name, value in fields.items() if value})
    extra = []
    if article.pmid:
        extra.append(f"PMID: {article.pmid}")
    if article.pmcid:
        extra.append(f"PMCID: {article.pmcid}")
    if article.item_type != "article-journal":
        extra.append(f"Type: {article.item_type}")
    if extra:
        data["extra"] = "\n".join(extra)
    return data


def library_info(config: Config) -> dict:
    """Verify the configured library identity without authorization or mutations."""
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
            api = DesktopAPI(client, config, interactive=False)
            return {"library": api.library_name or "My Library", "group_id": api.group_id}
    except httpx.HTTPError:
        raise ZoteroError(
            "Could not verify the Zotero library; open Zotero and enable its local API"
        ) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ZoteroError("Zotero returned invalid library metadata") from None


def save(
    article: Article, pdf: Path, config: Config, *, collection: str | None = None,
    interactive: bool = True,
) -> dict:
    """Add or reuse a bibliography entry and store the supplied (already compressed) PDF."""
    if not pdf.is_file():
        raise ZoteroError("The PDF to save in Zotero does not exist")
    if pdf.stat().st_size >= 4 * 1024**3:
        raise ZoteroError("Zotero requires attachments smaller than 4 GB")
    md5 = zotero_attachments.digest(pdf)
    emit("zotero", "Checking Zotero for an existing paper")
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            api = DesktopAPI(client, config, interactive)
            parent = _find_parent(api, article, pdf_md5=md5)
            collection_key, collection_name = _collection(
                api, collection if collection is not None else config.zotero_collection
            )
            added = parent is None
            if added:
                emit("zotero", "Adding bibliography entry to Zotero")
                parent = api.create(f"{api.prefix}/items", _bibliography(article, collection_key))
            elif collection_key and collection_key not in item_data(parent).get("collections", []):
                current = item_data(parent)
                version = current.get("version", parent.get("version"))
                if version is None:
                    raise ZoteroError("Zotero did not provide an item version; saving was stopped")
                api.request(
                    "PATCH", f"{api.prefix}/items/{object_key(parent)}", write=True,
                    headers={"If-Unmodified-Since-Version": str(version)},
                    json={"collections": [*current.get("collections", []), collection_key]},
                )
            attachment, present = zotero_attachments.ensure_attachment(
                api, object_key(parent), pdf, md5,
                reuse_existing_pdf=api.group_id is not None and not added,
            )
            if not present:
                zotero_attachments.upload(api, attachment, pdf, md5)
            result = {
                "status": "added" if added else ("already-present" if present else "attached"),
                "item_key": object_key(parent), "attachment_key": object_key(attachment),
                "collection": collection_name, "collection_key": collection_key,
            }
            if api.group_id is not None:
                result.update(group_id=api.group_id, library=api.library_name)
            return result
    except httpx.HTTPError:
        raise ZoteroError(
            "Could not communicate with Zotero. Open Zotero and enable its local API; "
            "the downloaded PDF is preserved"
        ) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ZoteroError(
            "Zotero returned an invalid response; the downloaded PDF is preserved"
        ) from None
