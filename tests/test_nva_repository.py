"""NVA JavaScript landings use exact-DOI, public published-file metadata."""

import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr.acquisition import capture_attempts
from papr.fetchers import oa, repositories
from papr.model import Article

RECORD = "0123456789ab-01234567-89ab-cdef-0123-456789abcdef"
FILE = "01234567-89ab-cdef-0123-456789abcdef"
LANDING = f"https://nva.sikt.no/registration/{RECORD}"
API = f"https://api.nva.unit.no/publication/{RECORD}"
SIGNED = "https://nva-public.s3.amazonaws.com/original.pdf?signature=private-signature"


def record(*, doi="10.1234/right", **changes):
    artifact = {
        "identifier": FILE,
        "name": "original.pdf",
        "type": "OpenFile",
        "mimeType": "application/pdf",
        "publisherVersion": "PublishedVersion",
        "allowedOperations": ["download"],
    }
    artifact.update(changes)
    return {
        "status": "PUBLISHED",
        "entityDescription": {"reference": {"doi": doi}},
        "associatedArtifacts": [artifact],
    }


def test_nva_ui_downloads_matching_published_pdf_and_redacts_signed_query(tmp_path, monkeypatch):
    original = pdf_bytes(text="Paper DOI 10.1234/right")
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if str(request.url) == LANDING:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=('<html><script src="app.js"></script><div id="root"></div></html>'),
            )
        if str(request.url) == API:
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(200, json=record())
        if str(request.url) == f"{API}/filelink/{FILE}":
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(200, json={"id": SIGNED})
        if str(request.url) == SIGNED:
            return httpx.Response(200, content=original)
        return httpx.Response(404)

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    destination = tmp_path / "original.pdf"
    attempts = []
    with capture_attempts(attempts):
        result = oa._download(LANDING, destination, Article("Paper", doi="10.1234/right"))
    assert result[1] == SIGNED
    assert destination.read_bytes() == original
    assert calls == [LANDING, API, f"{API}/filelink/{FILE}", SIGNED]
    assert "private-signature" not in str(attempts)
    assert any(a["source"] == "repository_api" and a["outcome"] == "discovered" for a in attempts)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "EmbargoedFile"},
        {"type": "HiddenFile"},
        {"publisherVersion": "AcceptedVersion"},
        {"publisherVersion": "SubmittedVersion"},
        {"mimeType": "text/html"},
        {"allowedOperations": []},
        {"allowedOperations": "download"},
        {"identifier": "../private"},
        {"name": "supplement.pdf"},
        {"name": "original-s001.pdf"},
    ],
)
def test_nonpublic_unpublished_or_nonpdf_files_are_never_requested(changes):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=record(**changes))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert (
            repositories.nva_pdf_urls(client, LANDING, Article("Paper", doi="10.1234/right")) == []
        )
    assert calls == [API]


def test_wrong_doi_is_not_followed_even_if_file_is_open():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=record(doi="10.1234/other"))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert (
            repositories.nva_pdf_urls(client, LANDING, Article("Paper", doi="10.1234/right")) == []
        )
    assert calls == [API]


@pytest.mark.parametrize(
    "url",
    [
        "https://nva.sikt.no.evil.test/registration/" + RECORD,
        "https://user:secret@nva.sikt.no/registration/" + RECORD,
        "https://nva.sikt.no/registration/../private",
        "http://nva.sikt.no/registration/" + RECORD,
    ],
)
def test_untrusted_landing_does_not_request_api(url):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("Untrusted API request"))
    ) as client:
        assert repositories.nva_pdf_urls(client, url, Article("Paper", doi="10.1234/right")) == []


@pytest.mark.parametrize("payload", [None, [], {"associatedArtifacts": "malformed"}])
def test_malformed_nva_record_cannot_supply_download(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        assert (
            repositories.nva_pdf_urls(client, LANDING, Article("Paper", doi="10.1234/right")) == []
        )


@pytest.mark.parametrize("status", ["DRAFT", "DELETED", None])
def test_nonpublished_record_is_not_followed(status):
    data = record()
    data["status"] = status
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        urls = repositories.nva_pdf_urls(client, LANDING, Article("Paper", doi="10.1234/right"))
    assert urls == [] and calls == [API]
