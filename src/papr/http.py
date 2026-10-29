from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from . import __version__

USER_AGENT = f"papr/{__version__} (+https://github.com/rossant/papr)"
_session: ContextVar[httpx.Client | None] = ContextVar("papr_http_session", default=None)
_deadline: ContextVar[float | None] = ContextVar("papr_http_deadline", default=None)


def remaining_time(default: float = 30.0) -> float:
    deadline = _deadline.get()
    remaining = default if deadline is None else min(default, deadline - time.monotonic())
    if remaining <= 0:
        raise httpx.TimeoutException("HTTP operation deadline exceeded")
    return remaining


@contextmanager
def http_deadline(seconds: float = 120.0) -> Iterator[None]:
    previous = _deadline.get()
    deadline = time.monotonic() + seconds
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


class _LimitedStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, release):
        self.stream = stream
        self.release = release
        self.closed = False

    def __iter__(self):
        for chunk in self.stream:
            remaining_time()
            yield chunk

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.stream.close()
            finally:
                self.release()


class _PoliteTransport(httpx.BaseTransport):
    """Limit active bodies per host, including redirects and streaming requests."""

    def __init__(self, transport: httpx.BaseTransport | None = None, *, per_host: int = 2):
        self.transport = transport or httpx.HTTPTransport()
        self.per_host = per_host
        self.lock = threading.Lock()
        self.hosts: dict[str, threading.BoundedSemaphore] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            semaphore = self.hosts.setdefault(
                request.url.host, threading.BoundedSemaphore(self.per_host)
            )
        # Each request has a finite retry budget even outside an item deadline.
        retry_deadline = time.monotonic() + remaining_time()
        for attempt in range(3):
            budget = min(remaining_time(), retry_deadline - time.monotonic())
            if budget <= 0 or not semaphore.acquire(timeout=max(0, budget)):
                raise httpx.PoolTimeout("Per-host request limit timed out", request=request)
            try:
                budget = min(remaining_time(), retry_deadline - time.monotonic())
                if budget <= 0:
                    raise httpx.TimeoutException("HTTP retry deadline exceeded", request=request)
                timeout = request.extensions.get("timeout", {}).copy()
                for key in ("connect", "read", "write", "pool"):
                    timeout[key] = min(timeout.get(key) or budget, budget)
                request.extensions["timeout"] = timeout
                response = self.transport.handle_request(request)
            except BaseException:
                semaphore.release()
                raise
            if response.is_closed:
                semaphore.release()
            else:
                response.stream = _LimitedStream(response.stream, semaphore.release)
            if response.status_code not in (429, 503) or attempt == 2:
                return response
            delay = 0.5 * 2**attempt
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    try:
                        when = parsedate_to_datetime(retry_after)
                        delay = max(0.0, (when - datetime.now(UTC)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            # Do not shorten Retry-After and hit a busy host prematurely.
            deadline = _deadline.get()
            retry_budget = retry_deadline - time.monotonic()
            if deadline is not None:
                retry_budget = min(retry_budget, deadline - time.monotonic())
            if delay >= retry_budget:
                return response
            response.close()
            time.sleep(delay)
        raise AssertionError("unreachable")

    def close(self):
        self.transport.close()


def _new_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0),
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        transport=_PoliteTransport(),
    )


@contextmanager
def client() -> Iterator[httpx.Client]:
    shared = _session.get()
    if shared is not None:
        yield shared
    else:
        with _new_client() as owned:
            yield owned


@contextmanager
def http_session() -> Iterator[httpx.Client]:
    """Reuse a thread-safe client for a command; workers must copy their context."""
    if _session.get() is not None:
        yield _session.get()
        return
    with _new_client() as owned:
        token = _session.set(owned)
        try:
            yield owned
        finally:
            _session.reset(token)
