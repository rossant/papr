from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from papr.fetchers import browser

pytest.importorskip("playwright.sync_api")


def test_visible_browser_owns_normal_chrome_and_closes_only_its_process(tmp_path, monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    launch = MagicMock(return_value=process)
    monkeypatch.setattr(browser.subprocess, "Popen", launch)
    playwright = MagicMock()
    attached = playwright.chromium.connect_over_cdp.return_value
    context = MagicMock()
    attached.contexts = [context]
    with browser.native_context(playwright, tmp_path, "/usr/bin/chrome") as result:
        assert result is context
        args = launch.call_args.args[0]
        assert args[0] == "/usr/bin/chrome"
        assert "--enable-automation" not in args
        assert "--headless" not in args
        assert "--remote-debugging-address=127.0.0.1" in args
        assert f"--user-data-dir={tmp_path}" in args
    attached.new_browser_cdp_session.return_value.send.assert_any_call("Browser.close")
    process.wait.assert_called_once_with(timeout=5)
    process.terminate.assert_not_called()


def test_profile_in_use_does_not_attach_to_an_unrelated_browser(tmp_path, monkeypatch):
    process = MagicMock()
    process.poll.return_value = 0  # Chrome handed off to an already running process.
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **kw: process)
    playwright = MagicMock()
    with pytest.raises(RuntimeError, match="Close other windows"):
        with browser.native_context(playwright, tmp_path, "/usr/bin/chrome"):
            pytest.fail("must not yield an unrelated browser")
    playwright.chromium.connect_over_cdp.assert_not_called()
    process.terminate.assert_not_called()


def test_failed_attachment_still_cleans_up_owned_process(tmp_path, monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **kw: process)
    clock = iter([0, 16])
    monkeypatch.setattr(browser, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(RuntimeError, match="15 seconds"):
        with browser.native_context(MagicMock(), tmp_path, "/usr/bin/chrome"):
            pytest.fail("unavailable browser must not yield")
    process.wait.assert_called_once_with(timeout=5)
