"""Session cookies must survive the lifetime of the Chrome process."""

import json
import stat
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from papr.config import Config
from papr.fetchers import ucl
from papr.fetchers.ucl_session import restore_session, save_session, verified_article


def test_session_state_is_private_and_restored_in_a_fresh_context(tmp_path):
    authenticated = MagicMock()
    state = {
        "cookies": [{"name": "session", "value": "private", "domain": ".example.org",
                     "path": "/", "expires": -1}],
        "origins": [{"origin": "https://example.org", "localStorage": [
            {"name": "signed-in", "value": "yes"},
        ]}],
    }
    authenticated.storage_state.return_value = state
    save_session(authenticated, tmp_path)
    path = tmp_path / "session.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == state
    fresh = MagicMock()
    restore_session(fresh, tmp_path)
    fresh.add_cookies.assert_called_once_with(state["cookies"])
    assert "location.origin" in fresh.add_init_script.call_args.args[0]


def test_missing_state_uses_existing_profile_and_bad_state_is_actionable(tmp_path):
    context = MagicMock()
    restore_session(context, tmp_path)
    context.add_cookies.assert_not_called()
    (tmp_path / "session.json").write_text('{"cookies": "private secret"}')
    with pytest.raises(RuntimeError, match="run papr login ucl") as failure:
        restore_session(context, tmp_path)
    assert "secret" not in str(failure.value)


def test_login_waits_for_verified_access_before_saving_or_closing(tmp_path, monkeypatch):
    config = Config()
    monkeypatch.setattr(Config, "ucl_profile_dir", property(lambda self: tmp_path))
    context = MagicMock()
    page = MagicMock()
    context.pages = [page]
    context.storage_state.return_value = {"cookies": [], "origins": []}
    calls = []

    @contextmanager
    def browser(*args, **kwargs):
        assert kwargs == {"reuse_session": False}
        calls.append("open")
        yield context
        calls.append("close")

    def prompt(*args):
        assert calls[-1] != "close"
        if len(calls) == 1:
            page.url = "https://login.microsoftonline.com/login"
            assert not (tmp_path / "session.json").exists()
        else:
            page.url = "https://www-nature-com.libproxy.ucl.ac.uk/articles/nature12373"
        calls.append("confirm")
        return ""

    monkeypatch.setattr(ucl, "_playwright", lambda: MagicMock())
    monkeypatch.setattr(ucl, "_browser_context", browser)
    monkeypatch.setattr("builtins.input", prompt)
    ucl.login(config)
    assert calls == ["open", "confirm", "confirm", "close"]
    context.storage_state.assert_called_once()
    assert (tmp_path / "session.json").is_file()


def test_verification_rejects_login_pages_and_unrelated_sites():
    assert verified_article("https://www-nature-com.libproxy.ucl.ac.uk/articles/nature12373")
    assert not verified_article("https://login.microsoftonline.com/login")
    assert not verified_article("https://www.nature.com/articles/nature12373")
    assert not verified_article("https://www-nature-com.libproxy.ucl.ac.uk/login")
