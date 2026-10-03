from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config
from .resolvers import zolit, zolit_sbs, zotero


@dataclass(frozen=True)
class SourceStatus:
    name: str
    available: bool
    detail: str


def _count_sqlite(path: Path, table: str) -> int | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(f"select count(*) from {table}").fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _zotero_status(config: Config) -> SourceStatus:
    base = config.zotero_api_url.rstrip("/")
    if base:
        try:
            with httpx.Client(timeout=0.6, headers={"Zotero-API-Version": "3"}) as client:
                r = client.get(
                    f"{base}/users/0/items/top",
                    params={"format": "keys", "limit": 1},
                )
                if r.status_code == 200:
                    count = r.headers.get("Total-Results")
                    detail = "local API"
                    if count:
                        detail += f", {count} top-level items"
                    return SourceStatus("zotero", True, detail)
                if r.status_code == 403:
                    return SourceStatus("zotero", False, "local API disabled in Zotero")
        except httpx.HTTPError:
            pass
    data_dir = zotero.find_data_dir(config)
    if data_dir:
        return SourceStatus("zotero", True, f"SQLite fallback: {data_dir}")
    return SourceStatus("zotero", False, "not detected")


def _zolit_status(config: Config) -> SourceStatus:
    db = zolit.find_db(config)
    if not db:
        return SourceStatus("zolit", False, "not detected")
    count = _count_sqlite(db, "items")
    detail = str(db)
    if count is not None:
        detail += f", {count} items"
    return SourceStatus("zolit", True, detail)


def _sbs_status(config: Config) -> SourceStatus:
    domain = zolit_sbs.find_domain(config)
    if not domain:
        return SourceStatus("zolit-sbs", False, "not detected")
    try:
        count = len(
            zolit_sbs.parse_key_publications(
                (domain / "key-publications.md").read_text(encoding="utf-8")
            )
        )
    except OSError:
        count = 0
    return SourceStatus("zolit-sbs", True, f"{domain}, {count} structured seeds")


def statuses(config: Config) -> list[SourceStatus]:
    return [
        _zotero_status(config),
        _zolit_status(config),
        _sbs_status(config),
        SourceStatus("crossref", True, "remote"),
        SourceStatus("openalex", True, "remote"),
        SourceStatus(
            "ucl",
            config.ucl_profile_dir.exists(),
            "saved browser profile" if config.ucl_profile_dir.exists() else "not logged in",
        ),
        SourceStatus(
            "mistral",
            bool(os.getenv("MISTRAL_API_KEY")),
            "key configured" if os.getenv("MISTRAL_API_KEY") else "key not configured",
        ),
    ]
