"""Access failures retain useful evidence without exposing credentials or signed URLs."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr.acquisition import capture_attempts, record_attempt
from papr.config import Config
from papr.fetchers import oa, ucl
from papr.fetchers.diagnostics import (
    PdfAccessError,
    access_problem,
    navigation_failure,
    response_problem,
)
from papr.model import Article


@pytest.mark.parametrize("title", ["Just a moment...", "Un instant…"])
def test_localized_cloudflare_challenge_requires_strong_markers(title):
    markup = f'<title>{title}</title><script src="/cdn-cgi/challenge-platform/script.js"></script>'
    assert "Cloudflare" in access_problem("https://publisher.test/article", markup)
    normal = '<title>Research paper</title><script src="https://static.cloudflareinsights.com/x.js">'
    assert access_problem("https://publisher.test/article", normal) is None
    assert access_problem("https://publisher.test/article", f"<title>{title}</title>") is None


def test_navigation_code_is_kept_without_raw_exception_url_or_login_advice():
    failure = navigation_failure(RuntimeError(
        "Page.goto net::ERR_BLOCKED_BY_CLIENT at https://user:password@publisher.test/?token=secret"
    ))
    assert failure["error_code"] == "net::ERR_BLOCKED_BY_CLIENT"
    assert "proxy" in failure["recovery"]
    for value in failure.values():
        assert all(secret not in value for secret in ("password", "token", "secret", "login"))


def test_attempt_metadata_is_safe_and_normalized():
    attempts = []
    with capture_attempts(attempts):
        record_attempt("ucl", "https://user:password@publisher.test/pdf?token=secret",
                       content_type="Text/HTML; charset=UTF-8; token=secret",
                       error_code="net::ERR_BLOCKED_BY_CLIENT")
    assert attempts[0]["url"] == "https://publisher.test/pdf"
    assert attempts[0]["content_type"] == "text/html"
    assert attempts[0]["error_code"] == "net::ERR_BLOCKED_BY_CLIENT"
    assert "secret" not in str(attempts)


def test_error_response_body_is_bounded_and_does_not_touch_pdf(tmp_path):
    chunks = []

    def stream():
        for _ in range(100):
            chunks.append(True)
            yield b"x" * 65536

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            return stream()

    destination = tmp_path / "existing.pdf"
    destination.write_bytes(b"preserve existing file")
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, stream=Stream())
    )) as client:
        response, body = oa._read_response(client, "https://publisher.test/pdf", destination)
    assert response.status_code == 403
    assert len(body) == 65536 and len(chunks) == 1
    assert destination.read_bytes() == b"preserve existing file"


def test_http_challenge_reports_final_url_status_and_media_type(tmp_path, monkeypatch):
    def respond(request):
        if request.url.path == "/article":
            return httpx.Response(302, headers={"location": "/verify?token=secret"})
        return httpx.Response(
            403, headers={"content-type": "text/html; charset=utf-8"},
            text='<title>Un instant…</title><script>window._cf_chl_opt={}</script>',
        )

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ))
    attempts = []
    with capture_attempts(attempts):
        assert oa.fetch(Article("Paper", url="https://publisher.test/article"),
                        tmp_path / "paper.pdf", Config()) is None
    assert attempts[0]["url"] == "https://publisher.test/verify"
    assert attempts[0]["status_code"] == 403
    assert attempts[0]["content_type"] == "text/html"
    assert "Cloudflare" in attempts[0]["reason"]
    assert "secret" not in str(attempts)


def test_response_classifier_separates_login_viewer_and_normal_article():
    assert "sign-in" in response_problem("https://login.microsoftonline.com/login", "", 200)
    assert "HTML viewer" in response_problem(
        "https://publisher.test/pdf", '<iframe src="/pdfdirect"></iframe>', 200, expect_pdf=True
    )
    assert response_problem("https://publisher.test/article", "Read the article", 200) is None


@pytest.fixture
def browser(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(Config, "ucl_profile_dir", property(lambda self: tmp_path))
    context = MagicMock()
    page = MagicMock()
    context.pages = [page]
    manager = MagicMock()
    monkeypatch.setattr(ucl, "_playwright", lambda: lambda: manager)

    @contextmanager
    def fake_browser(*args, **kwargs):
        yield context

    monkeypatch.setattr(ucl, "_browser_context", fake_browser)
    return context, page


def test_browser_denial_reports_final_page_before_link_discovery(browser, tmp_path, monkeypatch):
    _, page = browser
    page.url = "https://publisher-proxy.test/article?token=secret"
    response = page.goto.return_value
    response.status = 403
    response.headers = {"content-type": "text/html"}
    page.content.return_value = '<title>Un instant…</title><script>window._cf_chl_opt={}</script>'
    monkeypatch.setattr(ucl, "_candidate_urls", lambda page: pytest.fail("Access already denied"))
    attempts = []
    with capture_attempts(attempts), pytest.raises(PdfAccessError, match="Cloudflare"):
        ucl.fetch(Article("Paper", doi="10.1234/paper"), tmp_path / "paper.pdf", Config())
    assert attempts == [{
        "source": "ucl", "url": "https://publisher-proxy.test/article", "outcome": "failed",
        "reason": (
            "Publisher returned a Cloudflare browser verification challenge. "
            "Complete verification in a user-assisted browser or try another original source."
        ),
        "recovery": "Retry with --show-browser and complete browser verification",
        "status_code": 403, "content_type": "text/html",
    }]


def test_browser_blocked_navigation_keeps_error_code(browser, tmp_path):
    from playwright.sync_api import Error

    _, page = browser
    page.goto.side_effect = Error("net::ERR_BLOCKED_BY_CLIENT https://publisher.test/?token=secret")
    attempts = []
    with capture_attempts(attempts), pytest.raises(PdfAccessError, match="ERR_BLOCKED_BY_CLIENT"):
        ucl.fetch(Article("Paper", doi="10.1234/paper"), tmp_path / "paper.pdf", Config())
    assert attempts[0]["error_code"] == "net::ERR_BLOCKED_BY_CLIENT"
    assert "login" not in str(attempts).replace("https://libproxy.ucl.ac.uk/login", "")
    assert "secret" not in str(attempts)


def test_valid_landing_pdf_is_saved_before_html_classification(browser, tmp_path, monkeypatch):
    _, page = browser
    response = page.goto.return_value
    response.status = 200
    response.url = "https://publisher.test/paper.pdf"
    response.headers = {"content-type": "application/pdf"}
    response.body.return_value = pdf_bytes()
    page.content.side_effect = AssertionError("A valid PDF is not an HTML landing page")
    monkeypatch.setattr(ucl, "_candidate_urls", lambda page: pytest.fail("PDF already available"))
    destination = tmp_path / "paper.pdf"
    assert ucl.fetch(Article("Paper", doi="10.1234/paper"), destination, Config()) == response.url
    assert destination.read_bytes() == response.body.return_value


def test_browser_pdf_html_viewer_is_distinguished_from_denial(browser, tmp_path, monkeypatch):
    context, page = browser
    page.url = "https://publisher.test/doi/10.1234/paper"
    page.content.return_value = "<html>Readable article</html>"
    page.evaluate.return_value = "Browser User Agent"
    page.goto.return_value.status = 200
    page.goto.return_value.headers = {"content-type": "text/html"}
    pdf = "https://publisher.test/doi/pdf/10.1234/paper"
    monkeypatch.setattr(ucl, "_candidate_urls", lambda page: [pdf])
    reply = context.request.get.return_value
    reply.status = 200
    reply.headers = {"content-type": "text/html; charset=UTF-8"}
    reply.url = pdf + "?token=secret"
    reply.body.return_value = b'<iframe src="/doi/pdfdirect/10.1234/paper"></iframe>'
    monkeypatch.setattr(ucl._browser_pdf_transport, "browser_pdf", lambda *a, **kw: None)
    attempts = []
    with capture_attempts(attempts):
        assert ucl.fetch(Article("Paper", doi="10.1234/paper"),
                         tmp_path / "paper.pdf", Config()) is None
    viewer = next(row for row in attempts if "HTML viewer" in row.get("reason", ""))
    assert viewer["url"] == pdf and viewer["status_code"] == 200
    assert viewer["content_type"] == "text/html"
    assert "same-article" in viewer["recovery"]
    assert "secret" not in str(attempts)
