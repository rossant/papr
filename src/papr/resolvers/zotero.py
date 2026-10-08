from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from ..config import Config
from ..item_types import from_zotero
from ..model import Article, Person
from ..progress import emit
from .common import extract_year, normalize_doi, score_article
from .session import cached_library

logger = logging.getLogger(__name__)

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
API_BUDGET_SECONDS = 3.0
SNAPSHOT_TIMEOUT_SECONDS = 1.0
SQLITE_BUSY_TIMEOUT_SECONDS = 0.05


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
        if path and path.exists():
            return str(path)
    return None


def _api_articles(query: str, config: Config, qmode: str) -> list[Article]:
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


def search_api(query: str, config: Config) -> list[Article]:
    articles = _api_articles(query, config, "titleCreatorYear")
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def _snapshot(source: Path) -> sqlite3.Connection:
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS
    src = sqlite3.connect(
        f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS
    )
    dst = None

    reported_busy = False

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal reported_busy
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("Zotero database snapshot timed out; database is busy")
        if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and not reported_busy:
            emit("resolve", "Zotero database is busy; retrying briefly")
            reported_busy = True

    try:
        dst = sqlite3.connect(":memory:")
        # backup() otherwise retries SQLITE_BUSY forever, ignoring connect(timeout=...).
        src.backup(dst, pages=128, progress=progress, sleep=SQLITE_BUSY_TIMEOUT_SECONDS)
    except BaseException:
        if dst is not None:
            dst.close()
        raise
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    return dst


def _active_filter(conn: sqlite3.Connection, alias: str) -> str:
    # Some older exports and minimal fixtures have no trash table.
    has_trash = conn.execute(
        "select 1 from sqlite_master where type='table' and name='deletedItems'"
    ).fetchone()
    if not has_trash:
        return ""
    return f" and not exists (select 1 from deletedItems d where d.itemID = {alias}.itemID)"


def _sqlite_articles(
    conn: sqlite3.Connection,
    data_dir: Path,
    library_id: int | None = None,
) -> list[tuple[int, Article]]:
    library_filter = " and i.libraryID = ?" if library_id is not None else ""
    has_library = any(row[1] == "libraryID" for row in conn.execute("pragma table_info(items)"))
    library_select = ", i.libraryID" if has_library else ", null as libraryID"
    try:
        group_by_library = dict(conn.execute("select libraryID, groupID from groups").fetchall())
    except sqlite3.Error:
        group_by_library = {}
    active_filter = _active_filter(conn, "i")
    rows = conn.execute(
        f"""
        select i.itemID, i.key, it.typeName{library_select}
        from items i
        join itemTypes it on it.itemTypeID = i.itemTypeID
        where it.typeName not in ('attachment', 'note', 'annotation'){library_filter}{active_filter}
        """,
        (library_id,) if library_id is not None else (),
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
    attachment_filter = " and child.libraryID = ?" if library_id is not None else ""
    attachment_filter += _active_filter(conn, "child")
    attachment_rows = conn.execute(
        f"""
        select ia.parentItemID, child.key, ia.path, ia.contentType
        from itemAttachments ia
        join items child on child.itemID = ia.itemID
        where ia.parentItemID in ({placeholders}){attachment_filter}
        order by ia.itemID
        """,
        [*item_ids, *([library_id] if library_id is not None else [])],
    ).fetchall()
    attachments: dict[int, str] = {}
    for attachment in attachment_rows:
        path = _attachment_path(attachment, data_dir)
        if path:
            attachments.setdefault(attachment["parentItemID"], str(path))
    out = []
    for row in rows:
        values = fields.get(row["itemID"], {})
        item = {
            "itemType": row["typeName"],
            "key": row["key"],
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
            group = group_by_library.get(row["libraryID"])
            if group is not None:
                article.source_library = f"group:{group}"
            elif row["libraryID"] is not None:
                article.source_library = f"library:{row['libraryID']}"
            article.local_pdf = attachments.get(row["itemID"])
            out.append((row["itemID"], article))
    return out


def _sqlite_attachment(conn: sqlite3.Connection, item_id: int, data_dir: Path) -> str | None:
    active_filter = _active_filter(conn, "child")
    rows = conn.execute(
        f"""
        select child.key, ia.path, ia.contentType
        from itemAttachments ia
        join items child on child.itemID = ia.itemID
        where ia.parentItemID = ?{active_filter}
        order by ia.itemID
        """,
        (item_id,),
    ).fetchall()
    for row in rows:
        path = _attachment_path(row, data_dir)
        if path:
            return str(path)
    return None


def _attachment_path(row: sqlite3.Row, data_dir: Path) -> Path | None:
    raw = row["path"] or ""
    if row["contentType"] != "application/pdf" and not raw.lower().endswith(".pdf"):
        return None
    if raw.startswith("storage:"):
        path = data_dir / "storage" / row["key"] / raw.removeprefix("storage:")
    else:
        path = Path(raw).expanduser()
    return path if path.is_absolute() and path.is_file() else None


def _sqlite_all(config: Config) -> list[Article]:
    data_dir = find_data_dir(config)
    if not data_dir:
        return []

    group_id = getattr(config, "zotero_group_id", None)

    def load() -> list[Article]:
        emit("resolve", "Reading Zotero database snapshot")
        try:
            conn = _snapshot(data_dir / "zotero.sqlite")
        except (sqlite3.Error, OSError):
            emit("resolve", "Zotero database unavailable; continuing with other sources")
            logger.debug("Zotero SQLite snapshot failed", exc_info=True)
            # Cache this failure for the command too, rather than waiting again per paper.
            return []
        try:
            library_id = None
            if group_id is not None:
                try:
                    group = conn.execute(
                        "select libraryID from groups where groupID = ?",
                        (group_id,),
                    ).fetchone()
                except sqlite3.Error:
                    group = None
                if group is None or group["libraryID"] is None:
                    emit("resolve", f"Zotero group {group_id} is absent from the local database")
                    return []
                library_id = group["libraryID"]
                emit("resolve", f"Loading Zotero group {group_id} metadata")
            else:
                emit("resolve", "Loading Zotero library metadata")
            return [article for _, article in _sqlite_articles(conn, data_dir, library_id)]
        finally:
            conn.close()

    return cached_library(("zotero", str(data_dir.resolve()), group_id), load)


def search_sqlite(query: str, config: Config) -> list[Article]:
    articles = _sqlite_all(config)
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def by_doi(doi: str, config: Config) -> Article | None:
    target = normalize_doi(doi)
    if not target:
        return None
    try:
        for article in _api_articles(target, config, "everything"):
            if article.doi == target:
                article.score = 1.0
                return article
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("Zotero API unavailable; trying SQLite", exc_info=True)
    try:
        for article in _sqlite_all(config):
            if article.doi == target:
                article.score = 1.0
                return article
    except (sqlite3.Error, OSError):
        emit("resolve", "Zotero database unavailable; continuing with other sources")
        logger.debug("Zotero SQLite lookup failed", exc_info=True)
    return None


def search(query: str, config: Config) -> list[Article]:
    try:
        result = search_api(query, config)
        if result:
            return result
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("Zotero API unavailable; trying SQLite", exc_info=True)
    try:
        return search_sqlite(query, config)
    except (sqlite3.Error, OSError):
        emit("resolve", "Zotero database unavailable; continuing with other sources")
        logger.debug("Zotero SQLite search failed", exc_info=True)
        return []
