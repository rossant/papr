"""Keep papr state and personal configuration out of disposable test runs."""

import pytest


@pytest.fixture(autouse=True)
def isolated_papr_state(tmp_path, monkeypatch):
    # Patch the app's path providers, leaving uv's build cache available to the
    # offline packaging test. Individual tests can override these providers.
    monkeypatch.setattr("papr.config.user_config_dir", lambda app: str(tmp_path / "settings"))
    monkeypatch.setattr("papr.config.user_data_dir", lambda app: str(tmp_path / "data"))
    monkeypatch.setattr("papr.config.user_cache_dir", lambda app: str(tmp_path / "cache"))
