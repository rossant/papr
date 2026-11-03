"""Scoped, serializable acquisition diagnostics, independent of terminal progress."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

_current: ContextVar[list[dict] | None] = ContextVar("papr_acquisition_attempts", default=None)


@contextmanager
def capture_attempts(attempts: list[dict]):
    """Capture one item or worker's attempts without sharing another item's list."""
    token = _current.set(attempts)
    try:
        yield attempts
    finally:
        _current.reset(token)


def public_url(value: str) -> str | None:
    """Remove credentials, signed query parameters and fragments from diagnostics."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


def record_attempt(
    source: str,
    url: str | None = None,
    *,
    outcome: str = "failed",
    reason: str | None = None,
    recovery: str | None = None,
    status_code: int | None = None,
) -> None:
    """Store safe structured fields; callers must not include raw exception URLs."""
    attempts = _current.get()
    if attempts is None:
        return
    attempt = {"source": source, "outcome": outcome}
    if url and (safe := public_url(url)):
        attempt["url"] = safe
    if reason:
        attempt["reason"] = reason
    if recovery:
        attempt["recovery"] = recovery
    if status_code is not None:
        attempt["status_code"] = status_code
    attempts.append(attempt)
