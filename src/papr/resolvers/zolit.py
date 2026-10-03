from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

from ..config import Config
from ..model import Article, Person
from .common import normalize_doi, score_article


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


def search(query: str, config: Config) -> list[Article]:
    db = find_db(config)
    if not db:
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select item_key, title, year, creators_json, doi, zotero_json
            from items
            where title is not null and title != ''
            """
        ).fetchall()
        articles = []
        for row in rows:
            fields = _fields(row["zotero_json"])
            try:
                year = int(row["year"]) if row["year"] else None
            except (TypeError, ValueError):
                year = None
            article = Article(
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
                local_pdf=_local_pdf(conn, row["item_key"]),
                source="zolit",
            )
            article.score = score_article(query, article)
            if (article.score or 0.0) >= 0.35:
                articles.append(article)
    finally:
        conn.close()
    articles.sort(key=lambda a: a.score or 0.0, reverse=True)
    return articles[: config.max_candidates]
