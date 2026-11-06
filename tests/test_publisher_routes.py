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
from papr.fetchers.publishers import canonical_publisher_url, publisher_pdf_urls
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


@pytest.mark.parametrize("route", ["", "article/"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_legacy_springer_landing_is_canonicalized_before_proxying(route, scheme):
    old = f"{scheme}://link.springer.com/{route}10.1007/BF00707189?tracking=old"
    canonical = "https://link.springer.com/article/10.1007/BF00707189"
    assert canonical_publisher_url(old) == canonical
    assert ucl._proxy_url(Article("Paper", url=old)) == f"{ucl.UCL_LOGIN}?url={canonical}"


def test_canonicalization_preserves_article_scope_and_rejects_unknown_hosts():
    for url in (
        "http://link.springer.com.evil.test/10.1007/paper",
        "http://user:password@link.springer.com/10.1007/paper",
        "http://link.springer.com/chapter/10.1007/book",
        "http://link.springer.com/10.1007/paper/supplement.pdf",
        "http://www.ovid.com/10.1007/paper",
    ):
        assert canonical_publisher_url(url) == url
    old = "http://link.springer.com.libproxy.ucl.ac.uk/10.1007/paper"
    assert canonical_publisher_url(old) == (
        "https://link-springer-com.libproxy.ucl.ac.uk/article/10.1007/paper"
    )


@pytest.mark.parametrize("host", [
    "onlinelibrary.wiley.com", "onlinelibrary-wiley-com.libproxy.ucl.ac.uk",
])
@pytest.mark.parametrize("view", ["", "abs/", "full/", "pdf/", "epdf/", "pdfdirect/"])
def test_wiley_viewer_routes_target_only_the_same_doi(host, view):
    landing = f"https://{host}/doi/{view}10.1111/dmcn.15676"
    direct = f"https://{host}/doi/pdfdirect/10.1111/dmcn.15676"
    assert publisher_pdf_urls(landing) == [direct]
    assert article_identity(landing) == article_identity(direct)
    other = f"https://{host}/doi/pdfdirect/10.1111/other"
    assert rank_candidate_urls(landing, [other, direct], other) == [direct]


def test_wiley_wrapper_downloads_the_original_pdf_without_related_candidates(tmp_path, monkeypatch):
    landing = "https://onlinelibrary-wiley-com.libproxy.ucl.ac.uk/doi/pdf/10.1111/dmcn.15676"
    direct = landing.replace("/doi/pdf/", "/doi/pdfdirect/")
    original = pdf_bytes(text="doi: 10.1111/dmcn.15676")
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if str(request.url) == direct:
            return httpx.Response(200, content=original)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            f'<iframe src="{direct}"></iframe>'
            '<a href="/doi/pdfdirect/10.1111/other">Related PDF</a>'
        ))

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    article = Article("Paper", doi="10.1111/dmcn.15676", oa_pdf_url=landing)
    path = tmp_path / "original.pdf"
    assert oa.fetch(article, path, Config()) == direct
    assert path.read_bytes() == original
    assert requested == [landing, direct]

    page = Mock(url=landing)
    monkeypatch.setattr(
        ucl, "_collect_candidate_urls", lambda page: {"links": [], "citation": None}
    )
    assert ucl._candidate_urls(page) == [direct]


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


@pytest.mark.parametrize("accession", ["00005176-200007001-00004", "00005176-199705000-00001"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_lww_accession_uses_https_before_ucl_proxy_redirect(accession, scheme):
    old = f"{scheme}://journals.lww.com/{accession}"
    expected = f"https://journals.lww.com/{accession}"
    assert canonical_publisher_url(old) == expected
    assert ucl._proxy_url(Article("Paper", url=old)) == f"{ucl.UCL_LOGIN}?url={expected}"
    dotted = f"http://journals.lww.com.libproxy.ucl.ac.uk/{accession}"
    assert canonical_publisher_url(dotted) == (
        f"https://journals-lww-com.libproxy.ucl.ac.uk/{accession}"
    )


@pytest.mark.parametrize("url", [
    "http://journals.lww.com.evil.test/00005176-200007001-00004",
    "http://user:secret@journals.lww.com/00005176-200007001-00004",
    "http://journals.lww.com/search?query=00005176-200007001-00004",
    "http://journals.lww.com/unidentified",
])
def test_lww_canonicalization_does_not_expand_trust_or_guess_article(url):
    assert canonical_publisher_url(url) == url


def test_sciencedirect_http_200_content_error_is_not_missing_links(tmp_path, monkeypatch):
    markup = (
        '<title>ScienceDirect</title>'
        '<h1>There was a problem providing the content you requested</h1>'
        '<p>Reference number: private-reference</p><p>IP Address: private-address</p>'
        '<p>CPE00001</p>'
    )
    url = "https://www-sciencedirect-com.libproxy.ucl.ac.uk/science/article/pii/S0022347688806346"
    problem = access_problem(url, markup)
    assert "CPE00001" in problem and "--show-browser" in problem
    assert "private-reference" not in problem and "private-address" not in problem
    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=markup)
    )))
    attempts = []
    with capture_attempts(attempts):
        assert oa._download(url, tmp_path / "paper.pdf") is None
    assert len(attempts) == 1
    assert "CPE00001" in attempts[0]["reason"]
    assert "No matching PDF" not in attempts[0]["reason"]
    assert not (tmp_path / "paper.pdf").exists()


def test_sciencedirect_error_detection_requires_publisher_and_actual_error_page():
    markup = 'There was a problem providing the content you requested CPE00001'
    assert access_problem("https://unrelated.test/paper", markup) is None
    url = "https://www.sciencedirect.com/science/article/pii/S123"
    assert access_problem(url, "CPE00001") is None
