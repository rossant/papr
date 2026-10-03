"""Check timing, scoping and live rendering independently of pipeline mocks."""

import io
import logging

from rich.console import Console

from papr import progress


def test_stage_revisits_accumulate_and_failures_identify_active_stage(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    with progress.Reporter(enabled=False) as reporter:
        reporter.start_batch(1)
        reporter.start_item("Paper")
        progress.emit("resolve", "Searching")
        clock[0] = 2
        progress.emit("export", "Copying PDF")
        clock[0] = 3
        progress.emit("process", "OCR")
        clock[0] = 8
        progress.emit("export", "Writing Markdown")
        clock[0] = 10
        result = reporter.finish_item("error")
    assert result == {
        "duration": 10.0,
        "timings": {"resolve": 2.0, "export": 3.0, "process": 5.0},
        "failed_stage": "export",
    }


def test_prompt_time_excluded_and_nested_reporters_restore_context(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    with progress.Reporter(enabled=False) as outer:
        outer.start_item("Paper")
        progress.emit("resolve", "Searching")
        clock[0] = 2
        with progress.pause():
            clock[0] = 102
        with progress.Reporter(enabled=False) as inner:
            progress.emit("fetch", "Nested")
            assert inner.stage == "fetch"
        progress.emit("resolve", "Choosing")
        clock[0] = 103
        assert outer.finish_item("ok")["timings"] == {"resolve": 3.0}
    progress.emit("fetch", "Outside context")
    assert outer.stage is None


def test_live_render_shows_loading_then_measurements_and_estimated_eta(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    reporter = progress.Reporter(enabled=False)
    reporter.event("input", "Loading references")
    console = Console(file=io.StringIO(), width=120, color_system=None)
    with console.capture() as capture:
        console.print(reporter.render())
    assert "Loading references" in capture.get()
    reporter.start_batch(3)
    for number in range(2):
        reporter.start_item(str(number))
        clock[0] += 10
        reporter.finish_item("ok")
    reporter.start_item("Last paper")
    reporter.event("process", "Extracting pages", completed=2, total=5, unit="pages")
    with console.capture() as capture:
        console.print(reporter.render())
    rendered = capture.get()
    assert "ETA ~10s" in rendered
    assert "2/5 pages" in rendered
    assert "2/3" in rendered


def test_diagnostics_do_not_interpret_markup(capsys):
    with progress.Reporter(enabled=True, live=False):
        handler = progress.ProgressLogHandler()
        handler.emit(logging.LogRecord("papr", logging.DEBUG, "", 0, "[private]", (), None))
    assert "[private]" in capsys.readouterr().err


def test_live_resume_keeps_preceding_result_visible():
    output = io.StringIO()
    reporter = progress.Reporter(live=True)
    reporter.console = Console(file=output, force_terminal=True, width=100)
    with reporter:
        reporter.start_batch(1)
        reporter.start_item("Paper")
        progress.emit("resolve", "Searching")
        reporter.display.refresh()
        with progress.pause():
            output.write("RESULT\n")
        reporter.display.refresh()
        # The first frame after a result must not move up and erase that result.
        resumed = output.getvalue().split("RESULT\n", 1)[1]
        assert "Papers" in resumed
        assert "\x1b[1A" not in resumed.split("Papers", 1)[0]
