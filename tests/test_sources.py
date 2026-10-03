import httpx
import pytest

from papr.config import Config
from papr.sources import _zotero_status


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
