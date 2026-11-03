"""Bounded read-only Zotero snapshots, library scoping, and stored attachments."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..config import Config
from ..model import Article
from ..progress import emit
from .common import is_local_pdf
from .session import cached_library
from .zotero_metadata import from_api_item

logger = logging.getLogger(__name__)


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


def snapshot(source: Path) -> sqlite3.Connection:
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


def _attachment_path(row: sqlite3.Row, data_dir: Path) -> Path | None:
    raw = row["path"] or ""
    if row["contentType"] != "application/pdf" and not raw.lower().endswith(".pdf"):
        return None
    if raw.startswith("storage:"):
        path = data_dir / "storage" / row["key"] / raw.removeprefix("storage:")
    else:
        path = Path(raw).expanduser()
    return path if path.is_absolute() and is_local_pdf(path) else None


def all_articles(config: Config) -> list[Article]:
    data_dir = find_data_dir(config)
    if not data_dir:
        return []

    group_id = getattr(config, "zotero_group_id", None)

    def load() -> list[Article]:
        emit("resolve", "Reading Zotero database snapshot")
        try:
            conn = snapshot(data_dir / "zotero.sqlite")
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
