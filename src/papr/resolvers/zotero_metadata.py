"""Map Zotero API and SQLite item fields into common article metadata."""

from __future__ import annotations

import re

from ..item_types import from_zotero
from ..model import Article, Person
from .common import extract_year, normalize_doi

NON_BIB_TYPES = {"attachment", "note", "annotation"}


PMID_RE = re.compile(r"(?im)^PMID\s*:\s*(\d+)\s*$")


def _pmid(extra: str | None) -> str | None:
    match = PMID_RE.search(extra or "")
    return match.group(1) if match else None


def _people(creators: list[dict]) -> list[Person]:
    authors = [c for c in creators if c.get("creatorType") == "author"]
    selected = authors or creators
    return [
        Person(
            family=c.get("lastName") or c.get("name") or "",
            given=c.get("firstName") or "",
        )
        for c in selected
        if c.get("lastName") or c.get("name")
    ]


def from_api_item(item: dict, local_pdf: str | None = None) -> Article | None:
    data = item.get("data", item)
    if data.get("itemType") in NON_BIB_TYPES or data.get("deleted"):
        return None
    title = (data.get("title") or "").strip()
    if not title:
        return None
    journal = (
        data.get("publicationTitle")
        or data.get("proceedingsTitle")
        or data.get("bookTitle")
        or None
    )
    return Article(
        title=title,
        authors=_people(data.get("creators") or []),
        year=extract_year(data.get("date")),
        journal=journal,
        volume=data.get("volume") or None,
        issue=data.get("issue") or None,
        pages=data.get("pages") or None,
        doi=normalize_doi(data.get("DOI")),
        pmid=_pmid(data.get("extra")),
        url=data.get("url") or None,
        abstract=data.get("abstractNote") or None,
        item_type=from_zotero(data.get("itemType")),
        source_item_type=data.get("itemType"),
        source_key=item.get("key") or data.get("key"),
        source_library=(
            f"{item['library'].get('type')}:{item['library'].get('id')}"
            if item.get("library")
            else None
        ),
        local_pdf=local_pdf,
        source="zotero",
    )
