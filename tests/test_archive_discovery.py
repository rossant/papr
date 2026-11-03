from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from papr import archive_discovery
from papr.cache_cli import run
from papr.config import Config


def _config() -> Config:
    return Config.load()


def _article(doi: str = "10.1234/right") -> dict:
    return {
        "id": "https://openalex.org/W0",
        "doi": doi,
        "title": "A study of useful things",
        "authorships": [{"author": {
            "display_name": "Ada Example", "id": "https://openalex.org/A123"
        }}],
        "locations": [{"landing_page_url": "https://repo.example/record/1?token=secret"}],
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_rejects_doi_mismatch_without_thesis_search(monkeypatch):
    calls = []

    class Client:
        def get(self, url, params):
            calls.append(params)
            return Response({"results": [_article("10.1234/wrong")]})

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(archive_discovery, "client", client)
    assert archive_discovery.discover("10.1234/right", _config()) == []
    assert len(calls) == 1


def test_thesis_suggestions_are_unverified_and_urls_redacted(monkeypatch):
    thesis = _article()
    thesis.update({
        "title": "Infantile external hydrocephalus: epidemiological and clinical aspects",
        "doi": "10.9999/thesis",
        "type": "dissertation",
        "id": "https://openalex.org/W1",
        "referenced_works": ["https://openalex.org/W0"],
        "primary_location": {"landing_page_url": "https://repository.example/item?token=secret#part"},
    })
    calls = []

    class Client:
        def get(self, url, params):
            calls.append(params)
            results = [_article()] if len(calls) == 1 else [thesis]
            return Response({"results": results})

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(archive_discovery, "client", client)
    results = archive_discovery.discover("10.1234/right", _config())
    assert len(calls) == 2
    assert calls[1]["filter"] == "type:dissertation,authorships.author.id:A123"
    assert any(
        c["verification"] == "unverified" and c["type"] == "dissertation"
        and c["relatedness"] == "strong"
        for c in results
    )
    assert all("token" not in c["url"] and "#" not in c["url"] for c in results)


def test_invalid_doi_makes_no_client_or_request(monkeypatch):
    def forbidden():
        raise AssertionError("client must not be opened for invalid DOI")

    monkeypatch.setattr(archive_discovery, "client", forbidden)
    with pytest.raises(ValueError, match="valid DOI"):
        archive_discovery.discover("not a doi", _config())


def test_preserves_doi_suffixes_and_ignores_malformed_openalex_shapes(monkeypatch):
    calls = []

    class Client:
        def get(self, url, params):
            calls.append(params)
            if len(calls) == 1:
                return Response({
                    "results": [None, {"doi": "10.1234/right+suffix", "locations": None}]
                })
            return Response({"results": [None, {"type": "work", "authorships": None}]})

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(archive_discovery, "client", client)
    assert archive_discovery.discover("10.1234/right+suffix", _config()) == []
    assert calls[0]["filter"] == "doi:10.1234/right+suffix"


def test_cli_sanitizes_http_errors_and_does_not_write_cache(monkeypatch, tmp_path, capsys):
    cfg = _config()
    cache_dir, data_dir = tmp_path / "cache", tmp_path / "data"
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: cache_dir))
    monkeypatch.setattr(Config, "data_dir", property(lambda self: data_dir))

    def fail(*args, **kwargs):
        request = httpx.Request("GET", "https://example.org/?token=secret")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("signed https://example.org/?token=secret", request=request,
                                    response=response)

    monkeypatch.setattr(archive_discovery, "discover", fail)
    assert run(["discover", "10.1234/right"], cfg) == 1
    output = capsys.readouterr()
    assert "token" not in output.err and "secret" not in output.err
    assert not cache_dir.exists()
    assert not data_dir.exists()


def test_dissertation_lookup_failure_preserves_exact_locations(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [_article()]}) if len(requests) == 1 else (
            httpx.Response(503)
        )

    monkeypatch.setattr(archive_discovery, "client", lambda: httpx.Client(
        transport=httpx.MockTransport(respond)
    ))
    result = archive_discovery.discover("10.1234/right", Config())
    assert len(result) == 1
    assert result[0]["url"] == "https://repo.example/record/1"
    assert result[0]["verification"] == "unverified"


def test_dissertation_author_is_checked_and_short_title_does_not_prove_relation(monkeypatch):
    article = _article()
    article["title"] = "A B"
    thesis = {**_article(), "type": "dissertation", "title": "Unrelated dissertation",
              "primary_location": {"landing_page_url": "https://repo.example/thesis"},
              "abstract_inverted_index": {"Example": [0]}}
    wrong = {**thesis, "authorships": [{"author": {"id": "https://openalex.org/A999"}}]}
    count = []

    def respond(request):
        count.append(request)
        return httpx.Response(200, json={
            "results": [article] if len(count) == 1 else [wrong, thesis],
        })

    monkeypatch.setattr(archive_discovery, "client", lambda: httpx.Client(
        transport=httpx.MockTransport(respond)
    ))
    result = archive_discovery.discover("10.1234/right", Config())
    dissertations = [row for row in result if row["type"] == "dissertation"]
    assert len(dissertations) == 1
    assert dissertations[0]["relatedness"] == "author-only"


@pytest.mark.parametrize("payload", [[], None, "unexpected"])
def test_malformed_top_level_metadata_is_actionable(monkeypatch, payload):
    monkeypatch.setattr(archive_discovery, "client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ))
    with pytest.raises(ValueError, match="invalid metadata"):
        archive_discovery.discover("10.1234/right", Config())
