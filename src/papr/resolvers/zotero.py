from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from ..config import Config
from ..model import Article, Person
from .common import extract_year, normalize_doi, score_article

NON_BIB_TYPES = {"attachment", "note", "annotation"}
FIELD_NAMES = {
    "title",
    "date",
    "DOI",
    "publicationTitle",
    "proceedingsTitle",
    "bookTitle",
    "volume",
    "issue",
    "pages",
    "url",
    "abstractNote",
    "extra",
}
PMID_RE = re.compile(r"(?im)^PMID\s*:\s*(\d+)\s*$")


def find_data_dir(config: Config) -> Path | None:
    if config.zotero_data_dir:
        path = config.zotero_data_dir.expanduser()
        return path if (path / "zotero.sqlite").exists() else None
    candidates = [
        Path.home() / "Zotero",
        Path.home() / "Library" / "Application Support" / "Zotero",
    ]
    return next((p for p in candidates if (p / "zotero.sqlite").exists()), None)


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
    if data.get("itemType") in NON_BIB_TYPES:
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
        local_pdf=local_pdf,
        source="zotero",
    )


def _file_url_to_path(value: str) -> Path | None:
    parsed = urlparse(value.strip())
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def _api_attachment(client: httpx.Client, base: str, item_key: str) -> str | None:
    r = client.get(
        f"{base}/users/0/items/{item_key}/children",
        params={"itemType": "attachment", "format": "json"},
    )
    if r.status_code >= 400:
        return None
    for child in r.json():
        data = child.get("data", child)
        filename = data.get("filename") or ""
        content_type = data.get("contentType") or ""
        if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        key = child.get("key") or data.get("key")
        if not key:
            continue
        file_r = client.get(f"{base}/users/0/items/{key}/file/view/url")
        if file_r.status_code >= 400:
            continue
        path = _file_url_to_path(file_r.text)
        if path and path.exists():
            return str(path)
    return None


def search_api(query: str, config: Config) -> list[Article]:
    base = config.zotero_api_url.rstrip("/")
    if not base:
        return []
    headers = {"Zotero-API-Version": "3"}
    with httpx.Client(timeout=1.0, headers=headers) as c:
        r = c.get(
            f"{base}/users/0/items/top",
            params={
                "q": query,
                "qmode": "titleCreatorYear",
                "format": "json",
                "limit": max(20, config.max_candidates * 4),
            },
        )
        if r.status_code in {403, 404}:
            return []
        r.raise_for_status()
        articles = []
        for item in r.json():
            key = item.get("key") or item.get("data", {}).get("key")
            local_pdf = _api_attachment(c, base, key) if key else None
            article = from_api_item(item, local_pdf)
            if article:
                article.score = score_article(query, article)
                articles.append(article)
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def _snapshot(source: Path) -> sqlite3.Connection:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    return dst


def _sqlite_articles(conn: sqlite3.Connection, data_dir: Path) -> list[tuple[int, Article]]:
    rows = conn.execute(
        """
        select i.itemID, i.key, it.typeName
        from items i
        join itemTypes it on it.itemTypeID = i.itemTypeID
        where it.typeName not in ('attachment', 'note', 'annotation')
        """
    ).fetchall()
    if not rows:
        return []
    item_ids = [row["itemID"] for row in rows]
    placeholders = ",".join("?" for _ in item_ids)
    field_names = sorted(FIELD_NAMES)
    field_placeholders = ",".join("?" for _ in field_names)
    field_rows = conn.execute(
        f"""
        select d.itemID, f.fieldName, v.value
        from itemData d
        join fields f on f.fieldID = d.fieldID
        join itemDataValues v on v.valueID = d.valueID
        where d.itemID in ({placeholders})
          and f.fieldName in ({field_placeholders})
        """,
        [*item_ids, *field_names],
    ).fetchall()
    fields: dict[int, dict[str, str]] = {}
    for row in field_rows:
        fields.setdefault(row["itemID"], {})[row["fieldName"]] = row["value"]
    creator_rows = conn.execute(
        f"""
        select ic.itemID, ct.creatorType, c.firstName, c.lastName, c.fieldMode
        from itemCreators ic
        join creators c on c.creatorID = ic.creatorID
        join creatorTypes ct on ct.creatorTypeID = ic.creatorTypeID
        where ic.itemID in ({placeholders})
        order by ic.itemID, ic.orderIndex
        """,
        item_ids,
    ).fetchall()
    creators: dict[int, list[dict]] = {}
    for row in creator_rows:
        name = row["lastName"] if row["fieldMode"] == 1 else ""
        creators.setdefault(row["itemID"], []).append(
            {
                "creatorType": row["creatorType"],
                "firstName": row["firstName"] or "",
                "lastName": row["lastName"] or name or "",
                "name": name,
            }
        )
    out = []
    for row in rows:
        values = fields.get(row["itemID"], {})
        item = {
            "itemType": row["typeName"],
            "title": values.get("title", ""),
            "date": values.get("date", ""),
            "DOI": values.get("DOI", ""),
            "publicationTitle": values.get("publicationTitle", ""),
            "proceedingsTitle": values.get("proceedingsTitle", ""),
            "bookTitle": values.get("bookTitle", ""),
            "volume": values.get("volume", ""),
            "issue": values.get("issue", ""),
            "pages": values.get("pages", ""),
            "url": values.get("url", ""),
            "abstractNote": values.get("abstractNote", ""),
            "extra": values.get("extra", ""),
            "creators": creators.get(row["itemID"], []),
        }
        article = from_api_item(item)
        if article:
            article.local_pdf = _sqlite_attachment(conn, row["itemID"], data_dir)
            out.append((row["itemID"], article))
    return out


def _sqlite_attachment(conn: sqlite3.Connection, item_id: int, data_dir: Path) -> str | None:
    rows = conn.execute(
        """
        select child.key, ia.path, ia.contentType
        from itemAttachments ia
        join items child on child.itemID = ia.itemID
        where ia.parentItemID = ?
        order by ia.itemID
        """,
        (item_id,),
    ).fetchall()
    for row in rows:
        raw = row["path"] or ""
        if row["contentType"] != "application/pdf" and not raw.lower().endswith(".pdf"):
            continue
        if raw.startswith("storage:"):
            path = data_dir / "storage" / row["key"] / raw.removeprefix("storage:")
        else:
            path = Path(raw).expanduser()
        if path.is_absolute() and path.exists():
            return str(path)
    return None


def search_sqlite(query: str, config: Config) -> list[Article]:
    data_dir = find_data_dir(config)
    if not data_dir:
        return []
    conn = _snapshot(data_dir / "zotero.sqlite")
    try:
        articles = [article for _, article in _sqlite_articles(conn, data_dir)]
    finally:
        conn.close()
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def search(query: str, config: Config) -> list[Article]:
    try:
        result = search_api(query, config)
        if result:
            return result
    except (httpx.HTTPError, OSError, ValueError):
        pass
    try:
        return search_sqlite(query, config)
    except (sqlite3.Error, OSError):
        return []
