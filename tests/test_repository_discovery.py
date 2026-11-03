"""Repository fallback must preserve identity and publication version."""

import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr.acquisition import capture_attempts
from papr.config import Config
from papr.fetchers import oa, repositories
from papr.model import Article


def location(pdf=None, landing=None, *, kind="repository", version="publishedVersion"):
    return {
        "source": {"type": kind}, "version": version,
        "pdf_url": pdf, "landing_page_url": landing,
    }


def test_discovery_uses_exact_doi_and_published_repository_copies(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            {"doi": "https://doi.org/10.1234/OTHER", "locations": [
                location("https://repo.test/wrong.pdf"),
            ]},
            {"ids": {"doi": "https://doi.org/10.1234/PAPER"}, "locations": [
                location("https://repo.test/original.pdf", "https://repo.test/record"),
                location("https://repo.test/original.pdf"),
                location("https://repo.test/accepted.pdf", version="acceptedVersion"),
                location("https://repo.test/submitted.pdf", version="submittedVersion"),
                location("https://publisher.test/paper.pdf", kind="journal"),
                location("file:///private.pdf", "https://user:secret@repo.test/record"),
                None,
            ]},
        ]})

    monkeypatch.setattr(
        repositories, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond))
    )
    assert repositories.repository_urls(
        Article("Paper", doi="10.1234/paper"), Config(openalex_api_key="private-key")
    ) == ["https://repo.test/original.pdf", "https://repo.test/record"]
    assert requests[0].url.params["filter"] == "doi:10.1234/paper"
    assert requests[0].url.params["api_key"] == "private-key"


def test_no_doi_does_not_query_repository(monkeypatch):
    monkeypatch.setattr(repositories, "client", lambda: pytest.fail("Unexpected request"))
    assert repositories.repository_urls(Article("Paper"), Config()) == []


@pytest.mark.parametrize("body", [{}, [], {"results": "invalid"}])
def test_invalid_response_is_reportable(monkeypatch, body):
    monkeypatch.setattr(repositories, "client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ))
    with pytest.raises(ValueError, match="repository discovery"):
        repositories.repository_urls(Article("Paper", doi="10.1234/paper"), Config())


def test_repository_landing_recovers_pdf_after_known_sources_fail(tmp_path, monkeypatch):
    requests = []
    original = pdf_bytes(text="Paper doi:10.1234/paper")

    def respond(request):
        requests.append((request.url.host, request.url.path))
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": [{
                "doi": "10.1234/paper", "locations": [
                    location(landing="https://repo.test/record"),
                ],
            }]})
        if request.url.path == "/record":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                '<meta name="citation_pdf_url" content="/original.pdf">'
            ))
        if request.url.path == "/original.pdf":
            return httpx.Response(200, content=original)
        return httpx.Response(403)

    def mocked_client():
        return httpx.Client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(oa, "client", mocked_client)
    monkeypatch.setattr(repositories, "client", mocked_client)
    monkeypatch.setattr(oa, "_unpaywall_urls", lambda *args: [])
    article = Article("Paper", doi="10.1234/paper", oa_pdf_url="https://publisher.test/blocked")
    attempts = []
    destination = tmp_path / "paper.pdf"
    with capture_attempts(attempts):
        assert oa.fetch(article, destination, Config()) == "https://repo.test/original.pdf"
    assert destination.read_bytes() == original
    assert requests == [
        ("publisher.test", "/blocked"), ("api.openalex.org", "/works"),
        ("repo.test", "/record"), ("repo.test", "/original.pdf"),
    ]
    assert attempts[0]["status_code"] == 403
    assert any(row["source"] == "repository" and row["outcome"] == "discovered"
               for row in attempts)
    assert attempts[-1]["outcome"] == "success"


def test_repository_failure_does_not_prevent_publisher_fallback(tmp_path, monkeypatch):
    def respond(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(503)
        return httpx.Response(200, content=pdf_bytes())

    def mocked_client():
        return httpx.Client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(oa, "client", mocked_client)
    monkeypatch.setattr(repositories, "client", mocked_client)
    monkeypatch.setattr(oa, "_unpaywall_urls", lambda *args: [])
    attempts = []
    with capture_attempts(attempts):
        assert oa.fetch(Article("Paper", doi="10.1234/paper"), tmp_path / "p.pdf", Config())
    assert any(row["source"] == "repository" and row["outcome"] == "failed"
               for row in attempts)
    assert attempts[-1]["outcome"] == "success"
