from __future__ import annotations

import json
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
    state: str = "detected"
    last_synced_at: str | None = None


def _count_sqlite(path: Path, table: str) -> int | None:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.1)
        try:
            row = conn.execute(f"select count(*) from {table}").fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _zotero_status(config: Config, *, check: bool = False) -> SourceStatus:
    base = config.zotero_api_url.rstrip("/")
    unavailable = "not detected"
    if base:
        try:
            with httpx.Client(timeout=0.6, headers={"Zotero-API-Version": "3"}) as client:
                prefix = (
                    f"/groups/{config.zotero_group_id}"
                    if config.zotero_group_id is not None
                    else "/users/0"
                )
                r = client.get(
                    f"{base}{prefix}/items/top",
                    params={"format": "keys", "limit": 1},
                )
                if r.status_code == 200:
                    count = r.headers.get("Total-Results")
                    detail = "local API"
                    if config.zotero_group_id is not None:
                        detail += f", group {config.zotero_group_id}"
                    if count:
                        detail += f", {count} top-level items"
                    return SourceStatus("zotero", True, detail, "ready")
                if r.status_code == 403:
                    unavailable = "local API disabled in Zotero"
        except httpx.HTTPError:
            pass
    data_dir = zotero.find_data_dir(config)
    if data_dir:
        if check:
            try:
                conn = zotero._snapshot(data_dir / "zotero.sqlite")
                try:
                    conn.execute("select count(*) from items").fetchone()
                    if config.zotero_group_id is not None:
                        group = conn.execute(
                            "select libraryID from groups where groupID=?",
                            (config.zotero_group_id,),
                        ).fetchone()
                        if group is None or group[0] is None:
                            return SourceStatus(
                                "zotero",
                                False,
                                "configured group absent from SQLite",
                                "unavailable",
                            )
                finally:
                    conn.close()
            except (sqlite3.Error, OSError) as exc:
                return SourceStatus(
                    "zotero", False, f"SQLite fallback unavailable: {exc}", "unavailable"
                )
            return SourceStatus("zotero", True, f"SQLite fallback readable: {data_dir}", "ready")
        return SourceStatus("zotero", True, f"SQLite fallback: {data_dir}")
    return SourceStatus("zotero", False, unavailable, "unavailable")


def _zolit_status(config: Config, *, check: bool = False) -> SourceStatus:
    db = zolit.find_db(config)
    if not db:
        return SourceStatus("zolit", False, "not detected", "unavailable")
    count = _count_sqlite(db, "items")
    detail = str(db)
    if count is None:
        return SourceStatus("zolit", False, f"{detail}, database unreadable", "unavailable")
    detail += f", {count} items"
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("select * from source_sync where source='zotero'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row is None:
        return SourceStatus("zolit", True, f"{detail}, last sync unknown", "unknown")
    synced_at = row["synced_at"]
    detail += f", synced {synced_at}"
    state = "unknown"
    if check:
        try:
            expected = json.loads(row["source_fingerprint_json"])
            source = Path(row["source_path"])

            def fingerprint(path: Path) -> dict | None:
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    return None
                return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

            current = {"main": fingerprint(source), "wal": fingerprint(Path(str(source) + "-wal"))}
            if current["main"] is None:
                detail += ", source database unavailable; freshness unknown"
            elif not isinstance(expected, dict) or "main" not in expected or "wal" not in expected:
                detail += ", invalid sync fingerprint; freshness unknown"
            else:
                state = "ready" if current == expected else "changed"
                detail += (
                    ", source unchanged" if state == "ready" else ", source changed since sync"
                )
        except (OSError, ValueError, TypeError):
            detail += ", freshness unknown"
    return SourceStatus("zolit", True, detail, state, synced_at)


def _sbs_status(config: Config) -> SourceStatus:
    domain = zolit_sbs.find_domain(config)
    if not domain:
        return SourceStatus("zolit-sbs", False, "not detected")
    try:
        count = len(zolit_sbs.load_references(domain))
    except (OSError, ValueError, TypeError) as exc:
        return SourceStatus(
            "zolit-sbs", False, f"{domain}, invalid references: {exc}", "unavailable"
        )
    return SourceStatus("zolit-sbs", True, f"{domain}, {count} structured seeds", "ready")


def statuses(config: Config, *, check: bool = False) -> list[SourceStatus]:
    return [
        _zotero_status(config, check=check),
        _zolit_status(config, check=check),
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
