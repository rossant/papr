"""Exercise progress instrumentation with real processor and fetch behavior."""

import gzip

import httpx
import pytest
from pypdf import PdfWriter

from papr import fetchers
from papr.config import Config
from papr.fetchers import oa
from papr.model import Article
from papr.processors import native


def test_native_progress_tracks_real_pdf_pages(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    events = []
    monkeypatch.setattr(native, "emit", lambda stage, detail, **values: events.append(values))

    assert native.extract_text(pdf) == "\n"
    measured = [event for event in events if "completed" in event]
    assert [event["completed"] for event in measured] == [0, 1, 2, 3]
    assert all(event["total"] == 3 and event["unit"] == "pages" for event in measured)


def test_pdf_cache_reports_hit_without_calling_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    config = Config()
    article = Article(title="Cached paper")
    cache = fetchers._cache_path(article, config)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"%PDF-1.7\ncached")
    events = []
    monkeypatch.setattr(fetchers, "emit", lambda stage, detail: events.append((stage, detail)))

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("Cache hit must avoid network requests")

    monkeypatch.setattr(fetchers.oa, "fetch", unexpected_fetch)
    monkeypatch.setattr(fetchers.ucl, "fetch", unexpected_fetch)
    destination = tmp_path / "output.pdf"
    assert fetchers.fetch_pdf(article, destination, config) == "cache"
    assert destination.read_bytes() == cache.read_bytes()
    assert any(stage == "fetch" and "cached" in detail for stage, detail in events)


@pytest.mark.parametrize("length", ["known", "missing", "invalid", "compressed"])
def test_pdf_download_reports_streamed_bytes(monkeypatch, length):
    content = b"%PDF-1.7\n" + b"paper text" * 20
    wire = gzip.compress(content) if length == "compressed" else content
    headers = {"content-type": "application/pdf"}
    if length in {"known", "compressed"}:
        headers["content-length"] = str(len(wire))
    elif length == "invalid":
        headers["content-length"] = "unknown"
    if length == "compressed":
        headers["content-encoding"] = "gzip"

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for start in range(0, len(wire), 12):
                yield wire[start : start + 12]

    def respond(request):
        return httpx.Response(200, headers=headers, stream=Chunks())

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    events = []
    monkeypatch.setattr(oa, "emit", lambda stage, detail, **values: events.append(values))
    result = oa._download("https://example.org/paper.pdf?token=private")
    assert result is not None and result[0] == content
    measured = [event for event in events if "completed" in event]
    assert len(measured) > 2
    completed = [event["completed"] for event in measured]
    assert completed[0] == 0 and completed[-1] == len(content)
    assert completed == sorted(completed)
    expected_total = len(content) if length == "known" else None
    assert all(event["total"] == expected_total for event in measured)
    assert all(event["unit"] == "bytes" for event in measured)
