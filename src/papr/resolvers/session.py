"""Reuse local library reads during one command without sharing mutable results."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from ..model import Article

_libraries: ContextVar[dict[tuple, list[Article]] | None] = ContextVar(
    "papr_local_libraries", default=None
)


@contextmanager
def resolution_session() -> Iterator[None]:
    token = _libraries.set({})
    try:
        yield
    finally:
        _libraries.reset(token)


def cached_library(key: tuple, loader: Callable[[], list[Article]]) -> list[Article]:
    cache = _libraries.get()
    if cache is None:
        return loader()
    if key not in cache:
        cache[key] = loader()
    return deepcopy(cache[key])
