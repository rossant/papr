"""Repository fallback must preserve identity and publication version."""

import httpx
import pytest

from papr.config import Config
from papr.fetchers import repositories
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
