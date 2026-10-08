import json
import sqlite3

import httpx
import pytest

from papr.config import Config
from papr.sources import _sbs_status, _zolit_status, _zotero_status


@pytest.mark.parametrize("sqlite_available", [False, True])
def test_disabled_zotero_api_still_checks_sqlite(tmp_path, monkeypatch, sqlite_available):
    if sqlite_available:
        (tmp_path / "zotero.sqlite").touch()
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    monkeypatch.setattr(
        "papr.sources.httpx.Client",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    status = _zotero_status(Config(zotero_data_dir=tmp_path))
    assert status.available is sqlite_available
    if sqlite_available:
        assert "SQLite fallback" in status.detail
    else:
        assert "disabled" in status.detail


def test_zotero_check_detects_unreadable_sqlite(tmp_path):
    (tmp_path / "zotero.sqlite").write_bytes(b"not sqlite")
    status = _zotero_status(Config(zotero_api_url="", zotero_data_dir=tmp_path), check=True)
    assert status.available is False
    assert status.state == "unavailable"
    assert "unavailable" in status.detail


def test_zotero_check_detects_locked_sqlite(tmp_path, monkeypatch):
    path = tmp_path / "zotero.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("create table items (itemID integer)")
    writer = sqlite3.connect(path)
    writer.execute("begin exclusive")
    monkeypatch.setattr("papr.sources.zotero.SNAPSHOT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("papr.sources.zotero.SQLITE_BUSY_TIMEOUT_SECONDS", 0.01)
    try:
        assert (
            _zotero_status(Config(zotero_api_url="", zotero_data_dir=tmp_path), check=True).state
            == "unavailable"
        )
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("change", ["none", "main", "wal", "missing"])
def test_zolit_freshness_tracks_database_and_wal(tmp_path, change):
    source = tmp_path / "zotero.sqlite"
    source.write_bytes(b"source")
    stat = source.stat()
    fingerprint = {"main": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}, "wal": None}
    db = tmp_path / "zolit.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table items (item_key text)")
        conn.execute(
            "create table source_sync (source text, synced_at text, source_path text, "
            "source_fingerprint_json text)"
        )
        conn.execute(
            "insert into source_sync values (?, ?, ?, ?)",
            (
                "zotero",
                "2026-10-08T12:00:00+00:00",
                str(source),
                json.dumps(fingerprint),
            ),
        )
    if change == "main":
        source.write_bytes(b"changed source")
    elif change == "wal":
        (tmp_path / "zotero.sqlite-wal").write_bytes(b"new commit")
    elif change == "missing":
        source.unlink()
    status = _zolit_status(Config(zolit_db=db), check=True)
    assert status.available
    assert status.last_synced_at == "2026-10-08T12:00:00+00:00"
    assert (
        status.state
        == {"none": "ready", "main": "changed", "wal": "changed", "missing": "unknown"}[change]
    )


def test_zolit_legacy_index_reports_unknown_freshness(tmp_path):
    db = tmp_path / "zolit.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table items (item_key text)")
    status = _zolit_status(Config(zolit_db=db), check=True)
    assert status.available and status.state == "unknown"


def test_invalid_sbs_seed_file_is_unavailable(tmp_path):
    domain = tmp_path / "domains" / "sbs"
    domain.mkdir(parents=True)
    (domain / "references.csl.json").write_text("invalid json")
    status = _sbs_status(Config(zolit_sbs_repo=tmp_path))
    assert not status.available and status.state == "unavailable"
