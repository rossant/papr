import httpx
import pytest

from papr.config import Config
from papr.fetchers import PdfUnavailable, _cache_path, fetch_pdf, oa, ucl
from papr.fetchers.oa import is_pdf
from papr.model import Article


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config()


def test_pdf_validation():
    assert is_pdf(b"%PDF-1.7\n...")
    assert not is_pdf(b"anything", "application/pdf")
    assert not is_pdf(b"<html>no</html>", "text/html")


@pytest.mark.parametrize("pmcid", [
    "4166833", "PMC4166833", "pmc4166833",
    "https://www.ncbi.nlm.nih.gov/pmc/articles/4166833",
    "http://www.ncbi.nlm.nih.gov/pmc/articles/PMC4166833/",
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC4166833/",
])
def test_pmc_identifiers_and_urls_fetch_canonical_pdf(tmp_path, monkeypatch, config, pmcid):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.7\narticle")

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    assert oa.fetch(Article("Paper", pmcid=pmcid), tmp_path / "paper.pdf", config)
    assert requested == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC4166833/pdf/"]


def test_invalid_pmc_identifier_does_not_create_broken_url(config):
    assert list(oa.candidate_urls(Article("Paper", pmcid="https://example.org/123"), config)) == []


def test_oa_skips_mislabeled_pdf_and_broken_links(tmp_path, monkeypatch, config):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/bad.pdf":
            return httpx.Response(
                200, content=b"<html>Error</html>", headers={"content-type": "application/pdf"}
            )
        if request.url.path == "/landing":
            return httpx.Response(
                200,
                text='<a href="broken.pdf">PDF</a><a href="valid.pdf">PDF</a>',
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/broken.pdf":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=b"%PDF-1.7\nvalid")

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    article = Article(
        title="Paper", oa_pdf_url="https://example.org/bad.pdf", url="https://example.org/landing"
    )
    destination = tmp_path / "paper.pdf"
    assert oa.fetch(article, destination, config) == "https://example.org/valid.pdf"
    assert destination.read_bytes() == b"%PDF-1.7\nvalid"
    assert requested == ["/bad.pdf", "/landing", "/broken.pdf", "/valid.pdf"]


def test_local_attachment_precedes_cache(tmp_path, monkeypatch, config):
    local = tmp_path / "local.pdf"
    local.write_bytes(b"%PDF-1.7\nlocal")
    article = Article(title="Paper", local_pdf=str(local))
    cache = _cache_path(article, config)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"%PDF-1.7\ncached")
    destination = tmp_path / "output.pdf"
    assert fetch_pdf(article, destination, config) == f"local:{local}"
    assert destination.read_bytes() == local.read_bytes()
    assert cache.read_bytes() == local.read_bytes()
    assert list(cache.parent.glob("*.tmp.pdf")) == []


def test_invalid_cache_is_replaced(tmp_path, monkeypatch, config):
    article = Article(title="Paper")
    cache = _cache_path(article, config)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"<html>error</html>")

    def fetch(article, destination, config):
        destination.write_bytes(b"%PDF-1.7\nnew")
        return "https://example.org/paper.pdf"

    monkeypatch.setattr(oa, "fetch", fetch)
    destination = tmp_path / "output.pdf"
    assert fetch_pdf(article, destination, config).startswith("https:")
    assert cache.read_bytes() == destination.read_bytes() == b"%PDF-1.7\nnew"


def test_invalid_oa_result_falls_back_to_ucl(tmp_path, monkeypatch, config):
    def bad_fetch(article, destination, config):
        destination.write_bytes(b"error")
        return "bad"

    def good_fetch(article, destination, config):
        destination.write_bytes(b"%PDF-1.7\nucl")
        return "ucl"

    monkeypatch.setattr(oa, "fetch", bad_fetch)
    monkeypatch.setattr(ucl, "fetch", good_fetch)
    destination = tmp_path / "output.pdf"
    assert fetch_pdf(Article(title="Paper"), destination, config) == "ucl"
    assert destination.read_bytes() == b"%PDF-1.7\nucl"


def test_temp_files_are_unique_and_removed_on_error(tmp_path, monkeypatch, config):
    seen = []

    def failing_fetch(article, destination, config):
        seen.append(destination)
        destination.write_bytes(b"partial")
        raise RuntimeError("failure")

    monkeypatch.setattr(oa, "fetch", failing_fetch)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="failure"):
            fetch_pdf(Article(title="Paper"), tmp_path / "output.pdf", config)
    assert seen[0] != seen[1]
    assert all(not path.exists() for path in seen)
    assert not (tmp_path / "output.pdf").exists()


def test_failed_refresh_preserves_old_cache(tmp_path, monkeypatch, config):
    article = Article(title="Paper")
    cache = _cache_path(article, config)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"%PDF-1.7\nold")
    monkeypatch.setattr(oa, "fetch", lambda *args: None)
    with pytest.raises(PdfUnavailable):
        fetch_pdf(article, tmp_path / "output.pdf", config, refresh=True, allow_ucl=False)
    assert cache.read_bytes() == b"%PDF-1.7\nold"
    assert list(cache.parent.glob("*.tmp.pdf")) == []


def test_show_browser_requests_visible_institutional_browser(tmp_path, monkeypatch, config):
    monkeypatch.setattr(oa, "fetch", lambda *args: None)
    seen = []

    def institutional_fetch(article, destination, config, *, headless=True):
        seen.append(headless)
        destination.write_bytes(b"%PDF-1.7\narticle")
        return "ucl"

    monkeypatch.setattr(ucl, "fetch", institutional_fetch)
    assert fetch_pdf(Article("Paper"), tmp_path / "output.pdf", config, show_browser=True) == "ucl"
    assert seen == [False]


@pytest.mark.parametrize("source", ["oa", "pmc"])
def test_direct_sources_do_not_look_up_unpaywall(tmp_path, monkeypatch, config, source):
    def unexpected(*args):
        pytest.fail("Unpaywall must be lazy")

    monkeypatch.setattr(oa, "_unpaywall_urls", unexpected)
    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"%PDF-1.7\ndirect")
    )))
    article = Article(title="Paper", doi="10.1234/paper")
    if source == "oa":
        article.oa_pdf_url = "https://example.org/paper.pdf"
    else:
        article.pmcid = "123"
    destination = tmp_path / "paper.pdf"
    assert oa.fetch(article, destination, config)
    assert destination.read_bytes() == b"%PDF-1.7\ndirect"


def test_unpaywall_runs_after_direct_failure(tmp_path, monkeypatch, config):
    calls = []

    def unpaywall(*args):
        calls.append("unpaywall")
        return ["https://example.org/discovered.pdf"]

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path == "/direct.pdf" else httpx.Response(
            200, content=b"%PDF-1.7\ndiscovered"
        )

    monkeypatch.setattr(oa, "_unpaywall_urls", unpaywall)
    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    assert oa.fetch(
        Article(title="Paper", oa_pdf_url="https://example.org/direct.pdf"),
        tmp_path / "paper.pdf", config,
    )
    assert calls == ["/direct.pdf", "unpaywall", "/discovered.pdf"]


def test_partial_pdf_failure_preserves_destination_and_cleans_temp(tmp_path, monkeypatch, config):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.7\npartial"
            raise httpx.ReadError("interrupted")

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=BrokenBody())
    )))
    destination = tmp_path / "paper.pdf"
    destination.write_bytes(b"%PDF-1.7\nold")
    assert oa.fetch(
        Article(title="Paper", oa_pdf_url="https://example.org/paper.pdf"), destination, config
    ) is None
    assert destination.read_bytes() == b"%PDF-1.7\nold"
    assert list(tmp_path.glob("*.oa.pdf")) == []


def test_pdf_streams_to_disk_without_returning_full_body(tmp_path, monkeypatch):
    content = b"%PDF-1.7\n" + b"x" * 100_000

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(content), 1000):
                yield content[offset:offset + 1000]

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Body())
    )))
    destination = tmp_path / "streamed.pdf"
    result = oa._download("https://example.org/paper.pdf", destination)
    assert result[0] == b"%PDF-"
    assert destination.read_bytes() == content


def test_expired_item_deadline_stops_before_network(tmp_path, monkeypatch, config):
    from papr.http import http_deadline

    monkeypatch.setattr(oa, "http_deadline", lambda: http_deadline(0))
    monkeypatch.setattr(oa, "client", lambda: pytest.fail("deadline must prevent network requests"))
    assert oa.fetch(
        Article(title="Paper", doi="10.1234/paper", oa_pdf_url="https://example.org/paper.pdf"),
        tmp_path / "paper.pdf", config,
    ) is None
    assert list(tmp_path.iterdir()) == []
