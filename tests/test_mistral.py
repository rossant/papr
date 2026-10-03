import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from papr.config import Config
from papr.processors import mistral


@pytest.fixture
def mocked_api(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nmock")
    monkeypatch.setenv("MISTRAL_API_KEY", "secret-test-key")
    original_client = httpx.Client
    calls = []
    sleeps = []
    monkeypatch.setattr(mistral.time, "sleep", sleeps.append)

    def setup(responses):
        replies = iter(responses)

        def respond(request):
            calls.append((request.method, request.url.path, request.content))
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-id"})
            if request.url.path == "/v1/files/file-id/url":
                return httpx.Response(
                    200, json={"url": "https://storage.test/private?token=secret"}
                )
            return next(replies)

        monkeypatch.setattr(
            mistral.httpx,
            "Client",
            lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
        )
        return pdf, calls, sleeps

    return setup


@pytest.mark.parametrize("status", [429, 503])
def test_ocr_retries_without_reuploading(mocked_api, status):
    pdf, calls, sleeps = mocked_api(
        [
            httpx.Response(status, json={"message": "Try again"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"pages": [{"markdown": "OCR output"}]}),
        ]
    )
    assert mistral.to_markdown(pdf, Config()) == "OCR output\n"
    assert sleeps == [0]
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/v1/files"),
        ("GET", "/v1/files/file-id/url"),
        ("POST", "/v1/ocr"),
        ("POST", "/v1/ocr"),
        ("DELETE", "/v1/files/file-id"),
    ]
    ocr_bodies = [json.loads(body) for _, path, body in calls if path == "/v1/ocr"]
    assert ocr_bodies[0] == ocr_bodies[1]


def test_retries_stop_after_three_attempts_and_cleanup(mocked_api):
    pdf, calls, sleeps = mocked_api(
        [httpx.Response(429, json={"message": "Rate limit exceeded"}) for _ in range(3)]
    )
    with pytest.raises(mistral.MistralOcrError, match="HTTP 429: Rate limit exceeded") as error:
        mistral.to_markdown(pdf, Config())
    assert "rate limits and quota" in str(error.value)
    assert sleeps == [1, 2]
    assert sum(path == "/v1/ocr" for _, path, _ in calls) == 3
    assert calls[-1][:2] == ("DELETE", "/v1/files/file-id")


def test_long_retry_after_surfaces_error_without_waiting(mocked_api):
    pdf, calls, sleeps = mocked_api(
        [httpx.Response(429, json={"message": "Busy"}, headers={"Retry-After": "120"})]
    )
    with pytest.raises(mistral.MistralOcrError, match="Retry later"):
        mistral.to_markdown(pdf, Config())
    assert sleeps == []
    assert sum(path == "/v1/ocr" for _, path, _ in calls) == 1
    assert calls[-1][:2] == ("DELETE", "/v1/files/file-id")


def test_http_date_retry_after(mocked_api):
    retry_at = datetime.now(UTC) + timedelta(seconds=10)
    pdf, _, sleeps = mocked_api(
        [
            httpx.Response(503, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
            httpx.Response(200, json={"pages": [{"markdown": "Done"}]}),
        ]
    )
    assert mistral.to_markdown(pdf, Config()) == "Done\n"
    assert len(sleeps) == 1
    assert 8 <= sleeps[0] <= 10


def test_other_statuses_are_not_retried_and_messages_redact_secrets(mocked_api):
    message = "Invalid secret-test-key https://storage.test/private?token=secret"
    pdf, calls, sleeps = mocked_api([httpx.Response(400, json={"message": message})])
    with pytest.raises(mistral.MistralOcrError) as error:
        mistral.to_markdown(pdf, Config())
    text = str(error.value)
    assert "HTTP 400" in text
    assert "secret-test-key" not in text
    assert "storage.test" not in text
    assert sleeps == []
    assert calls[-1][:2] == ("DELETE", "/v1/files/file-id")


@pytest.mark.parametrize("value", ["garbage", "-1", "0", "30", "NaN", "inf"])
def test_retry_delay_bounds(value):
    delay = mistral._retry_delay(httpx.Response(429, headers={"Retry-After": value}), 1)
    assert 0 <= delay <= 31
