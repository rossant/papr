from unittest.mock import MagicMock

import pytest

from papr.fetchers import ucl

playwright = pytest.importorskip("playwright.sync_api")


def test_pdf_links_retried_after_navigation():
    page = MagicMock()
    page.evaluate.side_effect = [
        playwright.Error(
            "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
        ),
        {"citation": None, "links": [f"https://example.test/paper-{i}.pdf" for i in range(25)]},
    ]
    assert ucl._collect_candidate_urls(page)["links"] == [
        f"https://example.test/paper-{i}.pdf" for i in range(25)
    ]
    assert page.evaluate.call_count == 2
    assert page.wait_for_load_state.call_count == 2
    page.wait_for_load_state.assert_called_with("domcontentloaded", timeout=10_000)
    page.wait_for_timeout.assert_called_once_with(250)


def test_navigation_race_during_load_state_is_retried():
    page = MagicMock()
    page.wait_for_load_state.side_effect = [
        playwright.Error("Cannot find context with specified id"),
        None,
    ]
    page.evaluate.return_value = {"citation": None, "links": ["https://example.test/paper.pdf"]}
    assert ucl._collect_candidate_urls(page)["links"] == ["https://example.test/paper.pdf"]
    assert page.evaluate.call_count == 1
    page.wait_for_timeout.assert_called_once_with(250)


def test_navigation_retries_are_bounded():
    page = MagicMock()
    page.evaluate.side_effect = playwright.Error("Execution context was destroyed")
    with pytest.raises(playwright.Error, match="Execution context was destroyed"):
        ucl._collect_candidate_urls(page)
    assert page.evaluate.call_count == 3
    assert [call.args[0] for call in page.wait_for_timeout.call_args_list] == [250, 500]


@pytest.mark.parametrize(
    "error",
    [
        playwright.Error("ReferenceError: documentQuery is not defined"),
        playwright.TimeoutError("Timeout 10000ms exceeded"),
        ValueError("unexpected result"),
    ],
)
def test_unrelated_errors_are_not_retried(error):
    page = MagicMock()
    page.evaluate.side_effect = error
    with pytest.raises(type(error), match=str(error)):
        ucl._collect_candidate_urls(page)
    assert page.evaluate.call_count == 1
    page.wait_for_timeout.assert_not_called()


def test_pdf_links_wait_for_javascript_hydration(monkeypatch):
    page = MagicMock()
    page.url = "https://publisher.test/articles/paper"
    collect = MagicMock(
        side_effect=[
            {"citation": None, "links": []},
            {"citation": None, "links": []},
            {"citation": None, "links": []},
            {"citation": None, "links": ["/articles/paper/pdf"]},
        ]
    )
    monkeypatch.setattr(ucl, "_collect_candidate_urls", collect)
    assert ucl._candidate_urls(page) == ["https://publisher.test/articles/paper/pdf"]
    assert page.wait_for_timeout.call_count == 3
    page.wait_for_timeout.assert_called_with(1000)


def test_pdf_hydration_wait_is_bounded(monkeypatch):
    page = MagicMock()
    page.url = "https://publisher.test/articles/paper"
    collect = MagicMock(return_value={"citation": None, "links": []})
    monkeypatch.setattr(ucl, "_collect_candidate_urls", collect)
    assert ucl._candidate_urls(page) == []
    assert collect.call_count == 11
    assert page.wait_for_timeout.call_count == 10


def test_browser_pdf_saves_inline_response(tmp_path):
    page = MagicMock()
    page.url = "https://publisher.test/article"
    response = page.goto.return_value
    response.ok = True
    response.body.return_value = b"%PDF-1.7\ninline"
    response.url = "https://publisher.test/article/pdf"
    destination = tmp_path / "article.pdf"
    assert ucl._browser_pdf(page, response.url, destination) == response.url
    assert destination.read_bytes() == b"%PDF-1.7\ninline"
    page.goto.assert_called_once_with(
        response.url, wait_until="domcontentloaded", timeout=30_000, referer=page.url
    )
    handler = page.on.call_args.args[1]
    page.remove_listener.assert_called_once_with("download", handler)
    page.wait_for_timeout.assert_not_called()


def test_browser_pdf_saves_download_after_aborted_navigation(tmp_path):
    page = MagicMock()
    page.goto.side_effect = playwright.Error("net::ERR_ABORTED")
    download = MagicMock()
    download.url = "https://publisher.test/article/pdf"
    destination = tmp_path / "article.pdf"

    def save(path):
        path.write_bytes(b"%PDF-1.7\ndownload")

    download.save_as.side_effect = save

    def deliver_download(milliseconds):
        handler = page.on.call_args.args[1]
        handler(download)

    page.wait_for_timeout.side_effect = deliver_download
    assert ucl._browser_pdf(page, download.url, destination) == download.url
    assert destination.read_bytes() == b"%PDF-1.7\ndownload"
    assert list(tmp_path.glob("*.browser.pdf")) == []
    page.remove_listener.assert_called_once()


def test_browser_pdf_aborted_without_download_is_not_success(tmp_path):
    page = MagicMock()
    page.goto.side_effect = playwright.Error("net::ERR_ABORTED")
    with pytest.raises(playwright.Error, match="net::ERR_ABORTED"):
        ucl._browser_pdf(page, "https://publisher.test/article/pdf", tmp_path / "article.pdf")
    assert page.wait_for_timeout.call_count == 20
    page.remove_listener.assert_called_once()


def test_browser_pdf_rejects_invalid_download(tmp_path):
    page = MagicMock()
    page.goto.return_value = None
    download = MagicMock()
    download.save_as.side_effect = lambda path: path.write_bytes(b"<html>Denied</html>")
    page.wait_for_timeout.side_effect = lambda milliseconds: page.on.call_args.args[1](download)
    destination = tmp_path / "article.pdf"
    assert ucl._browser_pdf(page, "https://publisher.test/article/pdf", destination) is None
    assert not destination.exists()
    assert list(tmp_path.glob("*.browser.pdf")) == []


def test_browser_pdf_propagates_other_navigation_errors(tmp_path):
    page = MagicMock()
    page.goto.side_effect = playwright.Error("net::ERR_CONNECTION_RESET")
    with pytest.raises(playwright.Error, match="net::ERR_CONNECTION_RESET"):
        ucl._browser_pdf(page, "https://publisher.test/article/pdf", tmp_path / "article.pdf")
    page.wait_for_timeout.assert_not_called()
    page.remove_listener.assert_called_once()


def test_fetch_uses_browser_headers_and_falls_back_after_api_denial(tmp_path, monkeypatch):
    from papr.config import Config
    from papr.model import Article

    monkeypatch.setattr(Config, "ucl_profile_dir", property(lambda self: tmp_path))
    manager = MagicMock()
    browser = manager.__enter__.return_value
    context = browser.chromium.launch_persistent_context.return_value
    page = MagicMock()
    context.pages = [page]
    page.url = "https://publisher.test/articles/paper"
    page.evaluate.return_value = "Browser User Agent"
    page.goto.return_value.headers = {"content-type": "text/html"}
    context.request.get.return_value.ok = False
    context.request.get.return_value.body.return_value = b"Forbidden"
    monkeypatch.setattr(ucl, "_playwright", lambda: lambda: manager)
    monkeypatch.setattr(ucl, "_launch_kwargs", lambda: {})
    pdf_url = "https://publisher.test/articles/paper/pdf"
    monkeypatch.setattr(ucl, "_candidate_urls", lambda page: [pdf_url])
    fallback = MagicMock(return_value=pdf_url)
    monkeypatch.setattr(ucl, "_browser_pdf", fallback)
    destination = tmp_path / "paper.pdf"
    assert (
        ucl.fetch(Article(title="Paper", url=page.url), destination, Config(), headless=False)
        == pdf_url
    )
    browser.chromium.launch_persistent_context.assert_called_once_with(
        user_data_dir=str(tmp_path), headless=False, accept_downloads=True
    )
    context.request.get.assert_called_once_with(
        pdf_url,
        timeout=60_000,
        headers={
            "User-Agent": "Browser User Agent",
            "Referer": page.url,
        },
    )
    fallback.assert_called_once_with(page, pdf_url, destination, referer=page.url)
    context.close.assert_called_once()


def test_browser_pdf_preserves_original_article_referrer(tmp_path):
    page = MagicMock()
    page.url = "https://publisher.test/preview"
    page.goto.return_value.ok = True
    page.goto.return_value.body.return_value = b"%PDF-1.7\nfull"
    page.goto.return_value.url = "https://publisher.test/article/pdf"
    original = "https://publisher.test/article"
    assert ucl._browser_pdf(
        page, page.goto.return_value.url, tmp_path / "full.pdf", referer=original
    )
    assert page.goto.call_args.kwargs["referer"] == original
