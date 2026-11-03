"""Known publisher views preserve the requested article and institutional host."""

from unittest.mock import Mock

import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr.acquisition import capture_attempts
from papr.config import Config
from papr.fetchers import oa, ucl
from papr.fetchers.candidates import article_identity, rank_candidate_urls
from papr.fetchers.diagnostics import access_problem
from papr.fetchers.publishers import publisher_pdf_urls
from papr.model import Article


@pytest.mark.parametrize("host", ["www.ovid.com", "www-ovid-com.libproxy.ucl.ac.uk"])
def test_ovid_scripted_pdf_button_uses_current_accession_and_proxy(host):
    base = f"https://{host}/jnls/investigativeradiology"
    accession = "00004424-200308000-00003~magnetic-resonance-imaging"
    landing = f"{base}/abstract/{accession}?redirectionsource=fulltextview"
    pdf = f"{base}/pdf/{accession}"
    assert publisher_pdf_urls(landing) == [pdf]
    assert article_identity(landing) == article_identity(pdf)
    assert rank_candidate_urls(landing, publisher_pdf_urls(landing)) == [pdf]
    other = f"{base}/pdf/00004424-200308000-00004~related"
    assert rank_candidate_urls(landing, [other, pdf], other) == [pdf]


@pytest.mark.parametrize("host", ["link.springer.com", "link-springer-com.libproxy.ucl.ac.uk"])
@pytest.mark.parametrize("route", ["article/", "", "content/pdf/"])
def test_springer_original_route_keeps_exact_doi_and_host(host, route):
    suffix = ".pdf" if route == "content/pdf/" else ""
    landing = f"https://{host}/{route}10.1007/BF00707189{suffix}"
    pdf = f"https://{host}/content/pdf/10.1007/BF00707189.pdf"
    assert publisher_pdf_urls(landing) == [pdf]
    assert article_identity(landing) == article_identity(pdf)
    assert rank_candidate_urls(landing, publisher_pdf_urls(landing)) == [pdf]


@pytest.mark.parametrize("url", [
    "https://www.ovid.com/00004424-200308000-00003",  # journal is not yet known
    "https://www.ovid.com/jnls/journal/abstract/unidentified",
    "https://www.ovid.com/search?doi=10.1234/paper",
    "https://link.springer.com/search?query=10.1007/paper",
    "https://link.springer.com/chapter/10.1007/book-chapter",
    "https://link.springer.com/content/pdf/10.1007/paper/supplement.pdf",
    "https://link.springer.com.evil.test/article/10.1007/paper",
    "https://user:password@link.springer.com/article/10.1007/paper",
])
def test_unidentified_and_untrusted_pages_do_not_generate_pdf_routes(url):
    assert publisher_pdf_urls(url) == []


@pytest.mark.parametrize(("landing", "markup", "expected"), [
    (
        "https://www.ovid.com/jnls/investigativeradiology/abstract/"
        "00004424-200308000-00003~magnetic-resonance-imaging",
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"view":"abstract","accessionNumber":"00004424-200308000-00003",'
        '"hasPdf":true}</script>',
        "https://www.ovid.com/jnls/investigativeradiology/pdf/"
        "00004424-200308000-00003~magnetic-resonance-imaging",
    ),
    (
        "https://link.springer.com/article/10.1007/s001120050166",
        '<meta name="citation_doi" content="10.1007/s001120050166">'
        '<button>Download PDF</button>',
        "https://link.springer.com/content/pdf/10.1007/s001120050166.pdf",
    ),
])
def test_http_and_browser_share_known_original_routes(
    tmp_path, monkeypatch, landing, markup, expected,
):
    original = pdf_bytes()
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if str(request.url) == expected:
            return httpx.Response(200, content=original)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=markup)

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    destination = tmp_path / "original.pdf"
    assert oa.fetch(Article("Paper", url=landing), destination, Config()) == expected
    assert destination.read_bytes() == original
    assert requested == [landing, expected]

    page = Mock(url=landing)
    monkeypatch.setattr(
        ucl, "_collect_candidate_urls", lambda page: {"citation": None, "links": []}
    )
    assert ucl._candidate_urls(page) == [expected]
    page.wait_for_timeout.assert_not_called()


def test_springer_client_challenge_is_an_access_failure_not_missing_links(tmp_path, monkeypatch):
    # Minimal markers from the actual 3 KB response at both article and PDF URLs.
    markup = '<title>Client Challenge</title><script src="/_fs-ch-abc/script.js"></script>'
    problem = access_problem("https://link.springer.com/content/pdf/10.1007/paper.pdf", markup)
    assert "JavaScript browser challenge" in problem
    assert "--show-browser" in problem
    assert access_problem("https://link.springer.com/article/10.1007/paper", "Article text") is None
    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=markup)
    )))
    attempts = []
    with capture_attempts(attempts):
        assert oa.fetch(Article("Paper", url="https://link.springer.com/article/10.1007/paper"),
                        tmp_path / "paper.pdf", Config()) is None
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "failed"
    assert "JavaScript browser challenge" in attempts[0]["reason"]
