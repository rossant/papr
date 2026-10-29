from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from papr import http


def test_session_borrows_one_client_and_closes_on_error(monkeypatch):
    created = []

    def make_client():
        c = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        created.append(c)
        return c

    monkeypatch.setattr(http, "_new_client", make_client)
    with pytest.raises(RuntimeError):
        with http.http_session() as session:
            with http.client() as first, http.http_session() as nested:
                assert first is nested is session
            assert not session.is_closed
            context = copy_context()
            with ThreadPoolExecutor(1) as executor:
                def worker():
                    with http.client() as borrowed:
                        return borrowed is session
                assert executor.submit(context.run, worker).result()
            raise RuntimeError("failure")
    assert session.is_closed
    with http.client() as separate:
        assert separate is not session
    assert separate.is_closed
    assert len(created) == 2


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_and_retry_budget(monkeypatch, status):
    calls = []
    sleeps = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": "1"})

    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    with httpx.Client(transport=http._PoliteTransport(httpx.MockTransport(respond))) as c:
        assert c.get("https://example.org").status_code == status
    assert len(calls) == 3
    assert sleeps == [1, 1]


def test_long_retry_after_does_not_retry_early(monkeypatch):
    calls = []
    sleeps = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "600"})

    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    with httpx.Client(transport=http._PoliteTransport(httpx.MockTransport(respond))) as c:
        assert c.get("https://example.org").status_code == 429
    assert len(calls) == 1
    assert sleeps == []


def test_retry_after_http_date(monkeypatch):
    sleeps = []
    responses = iter([503, 200])
    retry_after = format_datetime(datetime.now(UTC) + timedelta(seconds=4))
    transport = http._PoliteTransport(httpx.MockTransport(
        lambda request: httpx.Response(next(responses), headers={"retry-after": retry_after})
    ))
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    with httpx.Client(transport=transport) as c:
        assert c.get("https://example.org").status_code == 200
    assert len(sleeps) == 1
    assert 2 <= sleeps[0] <= 4


def test_host_slot_held_until_stream_closes_and_other_hosts_proceed():
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b"body"

    transport = http._PoliteTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=Body())), per_host=1
    )
    with httpx.Client(transport=transport) as c:
        with c.stream("GET", "https://one.test"):
            assert c.get("https://two.test").status_code == 200
            with http.http_deadline(0.001), pytest.raises(httpx.PoolTimeout):
                c.get("https://one.test")
        assert c.get("https://one.test").content == b"body"


def test_stream_failure_releases_host_slot():
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError("broken stream")
            yield

    calls = iter([BrokenBody(), httpx.ByteStream(b"ok")])
    transport = http._PoliteTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=next(calls))), per_host=1
    )
    with httpx.Client(transport=transport) as c:
        with pytest.raises(httpx.ReadError):
            c.get("https://one.test")
        assert c.get("https://one.test").content == b"ok"
