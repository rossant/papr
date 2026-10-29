"""Bounded OA acquisition; publication and browser access stay on the caller thread."""

from __future__ import annotations

import shutil
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from .config import Config
from .fetchers import PdfUnavailable, _cache_path, _is_local_pdf
from .model import Article
from .progress import EventBuffer, emit


@dataclass
class PreparedItem:
    item: str | Article | Path
    article: Article | None = None
    local_pdf: Path | None = None
    error: Exception | None = None
    events: EventBuffer = field(default_factory=EventBuffer)
    future: Future | None = None
    fetch_events: EventBuffer = field(default_factory=EventBuffer)
    started: float = field(default_factory=perf_counter)
    resolve_duration: float = 0.0


class Prefetch:
    """Keep at most ``jobs`` prepared items and share acquisition by PDF cache key.

    prepare() always runs on the consuming thread. Worker callbacks must not take
    the state lock, publish outputs, or launch a browser. The caller must hold the
    state lock until this context exits, protecting cache writes against pruning.
    """

    def __init__(
        self,
        items: Iterable,
        prepare: Callable,
        fetch: Callable,
        config: Config,
        *,
        jobs: int,
        refresh: bool = False,
    ):
        self.items = iter(items)
        self.prepare = prepare
        self.fetch = fetch
        self.config = config
        self.jobs = jobs
        self.refresh = refresh
        self.futures: dict[Path, tuple[Future, EventBuffer]] = {}
        self.fallbacks: dict[Future, tuple[Path, str] | Exception] = {}
        self.fallback_durations: dict[Future, float] = {}

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="papr-prefetch-")
        self.executor = ThreadPoolExecutor(max_workers=self.jobs, thread_name_prefix="papr-fetch")
        return self

    def __exit__(self, *exc):
        # Workers must stop before temporary files or the shared HTTP session close.
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            self.temp.cleanup()

    def __iter__(self) -> Iterator[PreparedItem]:
        pending: deque[PreparedItem] = deque()
        exhausted = False
        while pending or not exhausted:
            while len(pending) < self.jobs and not exhausted:
                try:
                    item = next(self.items)
                except StopIteration:
                    exhausted = True
                    break
                prepared = PreparedItem(item)
                with prepared.events.capture():
                    try:
                        article, local, needs_pdf = self.prepare(item)
                        if local is None and article.local_pdf:
                            candidate = Path(article.local_pdf).expanduser()
                            if _is_local_pdf(candidate):
                                local = candidate
                        prepared.article, prepared.local_pdf = article, local
                        prepared.resolve_duration = perf_counter() - prepared.started
                        if needs_pdf and local is None:
                            key = _cache_path(article, self.config)
                            if key not in self.futures:
                                destination = Path(self.temp.name) / f"{len(self.futures)}.pdf"
                                context = copy_context()
                                future = self.executor.submit(
                                    context.run,
                                    self._fetch,
                                    article,
                                    destination,
                                    prepared.fetch_events,
                                )
                                self.futures[key] = future, prepared.fetch_events
                            prepared.future, prepared.fetch_events = self.futures[key]
                    except Exception as exc:
                        prepared.resolve_duration = perf_counter() - prepared.started
                        prepared.error = exc
                pending.append(prepared)
            if pending:
                yield pending.popleft()

    def _fetch(self, article: Article, destination: Path, events: EventBuffer):
        started = perf_counter()
        try:
            with events.capture():
                source = self.fetch(
                    article, destination, self.config, allow_ucl=False, refresh=self.refresh
                )
            return destination, source
        finally:
            events.duration = perf_counter() - started

    def result(
        self,
        prepared: PreparedItem,
        *,
        allow_ucl: bool,
        ucl_fetch: Callable,
        show_browser: bool = False,
    ) -> tuple[Path, str]:
        future = prepared.future
        assert future is not None
        try:
            while True:
                try:
                    result = future.result(timeout=0.1)
                    break
                except TimeoutError:
                    if future.done():
                        result = future.result()
                        break
                    prepared.fetch_events.replay()
        except PdfUnavailable:
            prepared.fetch_events.replay()
            if not allow_ucl:
                raise
            if future not in self.fallbacks:
                started = perf_counter()
                try:
                    self.fallbacks[future] = self._ucl(prepared.article, ucl_fetch, show_browser)
                except Exception as exc:
                    self.fallbacks[future] = exc
                finally:
                    self.fallback_durations[future] = perf_counter() - started
            result = self.fallbacks[future]
            if isinstance(result, Exception):
                raise result from None
        finally:
            # Only the coordinator touches the live reporter.
            prepared.fetch_events.replay()
        return result

    def timings(self, prepared: PreparedItem) -> dict[str, float]:
        """Actual stage work; item duration includes time in the bounded queue.

        Different papers overlap, so stage durations must not be summed across
        papers to infer batch elapsed time.
        """
        result = {"resolve": round(prepared.resolve_duration, 3)}
        if prepared.future is not None:
            result["fetch"] = round(
                prepared.fetch_events.duration + self.fallback_durations.get(prepared.future, 0), 3
            )
        return result

    def _ucl(self, article: Article, fetch: Callable, show_browser: bool) -> tuple[Path, str]:
        emit("fetch", "Open-access PDF unavailable; trying UCL access")
        cache = _cache_path(article, self.config)
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache.parent, suffix=".tmp.pdf", delete=False) as file:
            temporary = Path(file.name)
        try:
            source = (
                fetch(article, temporary, self.config, headless=False)
                if show_browser
                else fetch(article, temporary, self.config)
            )
            if not source or not _is_local_pdf(temporary):
                raise PdfUnavailable(f"No valid PDF found for {article.doi or article.title}")
            temporary.replace(cache)
            destination = Path(self.temp.name) / f"ucl-{len(self.fallbacks)}.pdf"
            shutil.copy2(cache, destination)
            return destination, source
        finally:
            temporary.unlink(missing_ok=True)
