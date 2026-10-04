"""Scoped pipeline events and terminal progress, independent of diagnostic logging."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

_current: ContextVar[Reporter | None] = ContextVar("papr_reporter", default=None)
STAGES = ("resolve", "fetch", "compress", "process", "export", "zotero")


def emit(
    stage: str,
    detail: str,
    *,
    completed: float | None = None,
    total: float | None = None,
    unit: str | None = None,
) -> None:
    reporter = _current.get()
    if reporter is not None:
        reporter.event(stage, detail, completed=completed, total=total, unit=unit)


@contextmanager
def pause():
    reporter = _current.get()
    if reporter is None:
        yield
    else:
        with reporter.pause():
            yield


class ProgressLogHandler(logging.StreamHandler):
    """Keep verbose diagnostics above a running live display."""

    def emit(self, record):
        with pause():
            super().emit(record)


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


class Reporter:
    def __init__(self, enabled: bool = True, *, live: bool | None = None):
        self.enabled = enabled
        self.console = Console(file=sys.stderr, markup=False, highlight=False)
        self.is_live = enabled and (self.console.is_terminal if live is None else live)
        self.started = time.monotonic()
        self.total = 0
        self.completed = 0
        self.succeeded = 0
        self.durations: list[float] = []
        self.label = ""
        self.stage: str | None = None
        self.item_started: float | None = None
        self.stage_started = self.started
        self.timings: dict[str, float] = {}
        self.details: dict[str, str] = {}
        self.measurements: dict[str, tuple[float | None, float | None, str | None]] = {}
        self.cache_hits = 0
        self.cached_stages: set[str] = set()
        self.spinner = Spinner("dots")
        self.display: Live | None = None

    def __enter__(self):
        self.token = _current.set(self)
        if self.is_live:
            self._start_display()
        return self

    def _start_display(self):
        # A fresh Live instance forgets the old frame height after a prompt/result.
        self.display = Live(
            console=self.console,
            get_renderable=self.render,
            refresh_per_second=4,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self.display.start()

    def __exit__(self, *exc):
        try:
            if self.display:
                self.display.stop()
        finally:
            _current.reset(self.token)

    def start_batch(self, total: int):
        self.total = total
        if self.enabled and not self.is_live:
            self.console.print(f"Processing {total} paper(s)")

    def start_item(self, label: str):
        self.label = label
        self.item_started = time.monotonic()
        self.stage = None
        self.timings = {}
        self.details = {}
        self.measurements = {}
        self.cached_stages = set()
        if self.enabled and not self.is_live:
            self.console.print(f"[{self.completed + 1}/{self.total}] {label}")

    def _close_stage(self, now: float):
        if self.stage is not None:
            self.timings[self.stage] = self.timings.get(self.stage, 0) + now - self.stage_started

    def event(self, stage, detail, *, completed=None, total=None, unit=None):
        now = time.monotonic()
        if stage != self.stage:
            self._close_stage(now)
            self.stage = stage
            self.stage_started = now
        changed = self.details.get(stage) != detail
        self.details[stage] = detail
        self.measurements[stage] = (completed, total, unit)
        if "cached" in detail.lower() or "cache hit" in detail.lower():
            if stage not in self.cached_stages:
                self.cache_hits += 1
                self.cached_stages.add(stage)
        if self.enabled and not self.is_live and changed:
            self.console.print(f"  {stage.capitalize()}: {detail}")

    def finish_item(self, status: str) -> dict:
        now = time.monotonic()
        self._close_stage(now)
        elapsed = now - self.item_started if self.item_started is not None else 0.0
        result = {
            "duration": round(elapsed, 3),
            "timings": {stage: round(value, 3) for stage, value in self.timings.items()},
        }
        if status == "error" and self.stage:
            result["failed_stage"] = self.stage
        self.completed += 1
        self.succeeded += status != "error"
        self.durations.append(elapsed)
        self.item_started = None
        self.stage = None
        return result

    @contextmanager
    def pause(self):
        started = time.monotonic()
        display = self.display
        if display:
            display.stop()
            self.display = None
        try:
            yield
        finally:
            delay = time.monotonic() - started
            self.started += delay
            self.stage_started += delay
            if self.item_started is not None:
                self.item_started += delay
            if display:
                self._start_display()

    def render(self):
        now = time.monotonic()
        elapsed = now - self.started
        eta = "estimating…"
        if self.completed >= self.total and self.total:
            eta = "0s"
        elif len(self.durations) >= 2:
            average = sum(self.durations) / len(self.durations)
            current = now - self.item_started if self.item_started is not None else 0
            remaining = average * max(0, self.total - self.completed) - current
            eta = "~" + _duration(remaining) if remaining > 0 else "estimating…"
        batch = Table.grid(padding=(0, 1))
        batch.add_row(
            Text("Papers"),
            ProgressBar(total=max(self.total, 1), completed=self.completed),
            Text(f"{self.completed}/{self.total}  elapsed {_duration(elapsed)}  ETA {eta}"),
        )
        if self.stage == "input" and self.item_started is None:
            loading = Table.grid(padding=(0, 1))
            loading.add_row(
                self.spinner,
                Text(self.details.get("input", "Loading inputs")),
                Text(_duration(now - self.stage_started)),
            )
            return loading
        stages = Table.grid(padding=(0, 2))
        for stage in STAGES:
            active = stage == self.stage
            seen = stage in self.details
            mark = self.spinner if active else Text("✓" if seen else "·")
            duration = self.timings.get(stage, 0)
            if active:
                duration += now - self.stage_started
            detail = self.details.get(stage, "")
            completed, total, unit = self.measurements.get(stage, (None, None, None))
            if completed is not None:
                count = f"{completed:g}/{total:g}" if total else f"{completed:g}"
                detail += f" · {count} {unit or ''}"
            stage_detail = Text(detail)
            if active and total is not None and total > 0 and completed is not None:
                stage_detail = Group(stage_detail, ProgressBar(total=total, completed=completed))
            stages.add_row(
                mark,
                Text(stage.capitalize()),
                stage_detail,
                Text(_duration(duration) if seen else ""),
            )
        return Group(batch, Text(self.label, overflow="ellipsis", no_wrap=True), stages)

    def summary(self, failures: int, output_dir: Path):
        if not self.enabled:
            return
        if self.display:
            self.display.stop()
            self.display = None
        succeeded = self.succeeded
        self.console.print(
            f"Done: {succeeded} succeeded, {failures} failed · "
            f"{self.cache_hits} cache hit(s) · {_duration(time.monotonic() - self.started)}"
        )
        self.console.print(f"Output: {output_dir}")
