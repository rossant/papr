import base64
from unittest.mock import MagicMock

import httpx
import pytest

from papr.config import Config
from papr.fetchers import oa, ucl
from papr.fetchers.diagnostics import PdfAccessError, access_problem
from papr.model import Article, article_from_dict
from papr.resolvers.common import deduplicate
from papr.resolvers.crossref import from_item as crossref_item
from papr.resolvers.openalex import from_item as openalex_item


def test_all_metadata_pdf_locations_survive_merge_and_serialization():
    first = crossref_item({"title": ["Paper"], "DOI": "10.1234/paper", "link": [
        {"URL": "https://publisher.test/first.pdf", "content-type": "application/pdf"},
        {"URL": "https://publisher.test/second.pdf", "content-type": "application/pdf"},
    ]})
    second = openalex_item({"title": "Paper", "doi": "10.1234/paper", "locations": [
        {"pdf_url": "https://publisher.test/second.pdf"},
        {"pdf_url": "https://repository.test/paper.pdf"},
    ]})
    result = article_from_dict(deduplicate([first, second])[0].to_dict())
    assert result.oa_pdf_urls == [
        "https://publisher.test/first.pdf", "https://publisher.test/second.pdf",
        "https://repository.test/paper.pdf",
    ]
    with pytest.raises(ValueError, match="PDF source URLs"):
        article_from_dict({"title": "Paper", "oa_pdf_urls": "https://example.test/pdf"})


def test_second_known_pdf_is_tried_before_discovery(tmp_path, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/first.pdf":
            return httpx.Response(403)
        return httpx.Response(200, content=b"%PDF-1.7\noriginal")

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(oa, "_unpaywall_urls", lambda *a: pytest.fail("premature discovery"))
    article = Article("Paper", oa_pdf_urls=[
        "https://publisher.test/first.pdf", "https://archive.test/second.pdf",
    ])
    destination = tmp_path / "paper.pdf"
    assert oa.fetch(article, destination, Config()) == "https://archive.test/second.pdf"
    assert destination.read_bytes() == b"%PDF-1.7\noriginal"
    assert requests == ["/first.pdf", "/second.pdf"]


def test_unpaywall_tries_repository_after_best_location_fails(tmp_path, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.host == "api.unpaywall.org":
            return httpx.Response(200, json={
                "best_oa_location": {"url_for_pdf": "https://publisher.test/blocked.pdf"},
                "oa_locations": [
                    {"url_for_pdf": "https://publisher.test/blocked.pdf"},
                    {"url_for_pdf": "https://archive.test/original.pdf"},
                ],
            })
        if request.url.path == "/blocked.pdf":
            return httpx.Response(403)
        return httpx.Response(200, content=b"%PDF-1.7\narchive")

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    article = Article("Paper", doi="10.1234/paper")
    assert oa.fetch(article, tmp_path / "paper.pdf", Config(unpaywall_email="me@example.org"))
    assert requests == ["/v2/10.1234/paper", "/blocked.pdf", "/original.pdf"]


@pytest.mark.parametrize(("url", "body", "expected"), [
    ("https://publisher.test/pdf?c=pdf_country_code_mismatch&token=secret", "", "country mismatch"),
    ("https://publisher.test/article?fr=RR-16&token=secret", "", "automation"),
    ("https://pmc.ncbi.nlm.nih.gov/pdf/",
     '<base href="https://www.google.com/recaptcha/challengepage/">', "reCAPTCHA"),
    ("https://publisher.test/pdf", "craft/challenge/pdf", "verification page"),
])
def test_access_failure_diagnostics_do_not_expose_signed_urls(url, body, expected):
    problem = access_problem(url, body)
    assert expected in problem
    assert "secret" not in problem
    assert "token" not in problem


def test_recaptcha_script_on_normal_article_is_not_a_pdf_failure():
    assert access_problem(
        "https://publisher.test/article", "<script src='recaptcha/enterprise.js'>"
    ) is None


def test_native_browser_fetch_saves_real_pdf_not_viewer_html(tmp_path):
    pytest.importorskip("playwright.sync_api")
    page = MagicMock()
    page.evaluate.return_value = {
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC2640637/pdf/original.pdf",
        "data": base64.b64encode(b"%PDF-1.7\noriginal text layer").decode(),
    }
    path = tmp_path / "original.pdf"
    assert ucl._browser_fetch_pdf(page, page.evaluate.return_value["url"], path)
    assert path.read_bytes() == b"%PDF-1.7\noriginal text layer"
    page.evaluate.return_value = None
    assert ucl._browser_fetch_pdf(page, "https://publisher.test/verification", path) is None
    assert path.read_bytes() == b"%PDF-1.7\noriginal text layer"


def test_browser_verification_is_reported_instead_of_missing_pdf(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    page = MagicMock()
    page.url = "https://publisher.test/article?fr=RR-16"
    page.goto.return_value = None
    page.content.return_value = "verification"
    monkeypatch.setattr(ucl, "_browser_fetch_pdf", lambda *args: None)
    with pytest.raises(PdfAccessError, match="RR-16"):
        ucl._browser_pdf(page, "https://publisher.test/pdf", tmp_path / "paper.pdf")


def test_visible_browser_allows_user_verification_before_original_fetch(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    page = MagicMock()
    page.url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC2640637/pdf/original.pdf"
    page.goto.return_value = None
    page.content.side_effect = [
        '<base href="https://www.google.com/recaptcha/challengepage/">', "PDF viewer",
    ]
    destination = tmp_path / "paper.pdf"

    def original_fetch(page, url, path):
        path.write_bytes(b"%PDF-1.7\nverified original")
        return url

    monkeypatch.setattr(ucl, "_browser_fetch_pdf", original_fetch)
    assert ucl._browser_pdf(page, page.url, destination, verification_wait=60) == page.url
    assert destination.read_bytes() == b"%PDF-1.7\nverified original"
    page.wait_for_timeout.assert_any_call(1000)
