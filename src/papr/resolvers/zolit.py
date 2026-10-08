from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

from ..config import Config
from ..item_types import from_zotero
from ..model import Article, Person
from .common import normalize_doi, score_article
from .session import cached_library


def _config_paths(config: Config) -> list[Path]:
    paths: list[Path] = []
    if config.zolit_repo:
        paths.extend(
            [
                config.zolit_repo / "config.local.toml",
                config.zolit_repo / "config.toml",
            ]
        )
    if config.zolit_sbs_repo:
        sibling = config.zolit_sbs_repo.parent / "zolit"
        paths.extend([sibling / "config.local.toml", sibling / "config.toml"])

    installed_domain = Path.home() / ".zolit" / "domains" / "sbs"
    if installed_domain.exists():
        try:
            sbs_repo = installed_domain.resolve().parent.parent
            sibling = sbs_repo.parent / "zolit"
            paths.extend([sibling / "config.local.toml", sibling / "config.toml"])
        except OSError:
            pass

    paths.extend(
        [
            Path.home() / ".zolit" / "config.toml",
            Path.home() / ".config" / "zolit" / "config.toml",
            Path.cwd() / "config.local.toml",
            Path.cwd().parent / "zolit" / "config.local.toml",
        ]
    )
    return paths


def find_db(config: Config) -> Path | None:
    if config.zolit_db:
        path = config.zolit_db.expanduser()
        return path if path.exists() else None
    for cfg_path in _config_paths(config):
        if not cfg_path.exists():
            continue
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        local_data = data.get("paths", {}).get("local_data_dir", "./data.local")
        root = Path(local_data).expanduser()
        if not root.is_absolute():
            root = cfg_path.parent / root
        db = root.resolve() / "zolit.sqlite"
        if db.exists():
            return db
    if config.zolit_repo:
        db = config.zolit_repo.expanduser() / "data.local" / "zolit.sqlite"
        if db.exists():
            return db
    return None


def _people(value: str | None) -> list[Person]:
    try:
        creators = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    authors = [c for c in creators if c.get("creator_type") == "author"]
    selected = authors or creators
    return [
        Person(
            family=c.get("last_name") or c.get("name") or "",
            given=c.get("first_name") or "",
        )
        for c in selected
        if c.get("last_name") or c.get("name")
    ]


def _fields(value: str | None) -> dict:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload.get("fields", payload.get("zotero", {}).get("fields", {}))


def _local_pdf(conn: sqlite3.Connection, item_key: str) -> str | None:
    try:
        rows = conn.execute(
            """
            select path from attachments
            where item_key = ? and exists_on_disk = 1
              and (content_type = 'application/pdf' or lower(path) like '%.pdf')
            order by attachment_key
            """,
            (item_key,),
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        if row["path"] and Path(row["path"]).exists():
            return row["path"]
    return None


def _article_from_row(
    conn: sqlite3.Connection, row: sqlite3.Row, local_pdfs: dict[str, str] | None = None
) -> Article:
    fields = _fields(row["zotero_json"])
    try:
        payload = json.loads(row["zotero_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    zotero_data = payload.get("zotero", payload)
    item_type = zotero_data.get("item_type") or zotero_data.get("itemType")
    try:
        year = int(row["year"]) if row["year"] else None
    except (TypeError, ValueError):
        year = None
    return Article(
        title=row["title"],
        authors=_people(row["creators_json"]),
        year=year,
        journal=fields.get("publicationTitle") or fields.get("bookTitle") or None,
        volume=fields.get("volume") or None,
        issue=fields.get("issue") or None,
        pages=fields.get("pages") or None,
        doi=normalize_doi(row["doi"]),
        url=fields.get("url") or None,
        abstract=fields.get("abstractNote") or None,
        item_type=from_zotero(item_type),
        source_item_type=item_type,
        source_key=row["item_key"],
        source_library=(
            f"group:{zotero_data['group_id']}"
            if zotero_data.get("group_id") is not None
            else f"library:{zotero_data['library_id']}"
            if zotero_data.get("library_id") is not None
            else None
        ),
        local_pdf=(
            local_pdfs.get(row["item_key"])
            if local_pdfs is not None
            else _local_pdf(conn, row["item_key"])
        ),
        source="zolit",
    )


def _all_articles(db: Path, group_id: int | None = None) -> list[Article]:
    def load() -> list[Article]:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select item_key, title, year, creators_json, doi, zotero_json
                from items where title is not null and title != ''
                """
            ).fetchall()
            if group_id is not None:
                scoped = []
                for row in rows:
                    try:
                        payload = json.loads(row["zotero_json"] or "{}")
                        metadata = payload.get("zotero", payload)
                        if metadata.get("group_id") == group_id:
                            scoped.append(row)
                    except (ValueError, TypeError, AttributeError):
                        continue
                rows = scoped
            local_pdfs: dict[str, str] = {}
            try:
                attachments = conn.execute(
                    """
                    select item_key, path from attachments
                    where exists_on_disk = 1
                      and (content_type = 'application/pdf' or lower(path) like '%.pdf')
                    order by attachment_key
                    """
                ).fetchall()
            except sqlite3.Error:
                attachments = []
            for attachment in attachments:
                path = attachment["path"]
                if path and Path(path).expanduser().is_file():
                    local_pdfs.setdefault(attachment["item_key"], str(Path(path).expanduser()))
            return [_article_from_row(conn, row, local_pdfs) for row in rows]
        finally:
            conn.close()

    return cached_library(("zolit", str(db.resolve()), group_id), load)


def search(query: str, config: Config) -> list[Article]:
    db = find_db(config)
    if not db:
        return []
    articles = _all_articles(db, config.zotero_group_id)
    for article in articles:
        article.score = score_article(query, article)
    articles = [a for a in articles if (a.score or 0.0) >= 0.35]
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]


def by_doi(doi: str, config: Config) -> Article | None:
    target = normalize_doi(doi)
    db = find_db(config)
    if not target or not db:
        return None
    for article in _all_articles(db, config.zotero_group_id):
        if article.doi == target:
            article.score = 1.0
            return article
    return None
