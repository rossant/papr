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
