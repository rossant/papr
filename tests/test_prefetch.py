import json
import threading
from contextvars import ContextVar
from pathlib import Path

import pytest

from papr import cli
from papr.config import Config
from papr.fetchers import PdfUnavailable
from papr.model import Article
from papr.prefetch import Prefetch
from papr.progress import Reporter, emit


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    return Config(download_dir=tmp_path / "out", pdf_compress=False)


def test_prefetch_overlaps_but_preparation_and_publication_stay_in_order(config):
    main_thread = threading.get_ident()
    barrier = threading.Barrier(2)
    inherited = ContextVar("test_prefetch_context", default=None)
    token = inherited.set("shared-http-session")
    prepared = []
    worker_threads = []

    def prepare(item):
        assert threading.get_ident() == main_thread
        prepared.append(item)
        return Article(item), None, True

    def fetch(article, destination, config, **kwargs):
        assert inherited.get() == "shared-http-session"
        assert kwargs["allow_ucl"] is False
        worker_threads.append(threading.get_ident())
        barrier.wait(timeout=3)
        emit("fetch", f"Fetched {article.title}")
        destination.write_bytes(b"%PDF-test")
        return article.title

    try:
        with Prefetch(
            ["first", "second", "third", "fourth"], prepare, fetch, config, jobs=2
        ) as pool:
            iterator = iter(pool)
            first = next(iterator)
            assert prepared == ["first", "second"]
            path, source = pool.result(first, allow_ucl=False, ucl_fetch=None)
            assert path.is_file() and source == "first"
            second = next(iterator)
            assert prepared == ["first", "second", "third"]
            assert pool.result(second, allow_ucl=False, ucl_fetch=None)[1] == "second"
            assert [pool.result(item, allow_ucl=False, ucl_fetch=None)[1] for item in iterator] == [
                "third",
                "fourth",
            ]
            temp_directory = Path(pool.temp.name)
        assert not temp_directory.exists()
        assert len(set(worker_threads)) == 2
        assert main_thread not in worker_threads
    finally:
        inherited.reset(token)


def test_refresh_duplicates_share_acquisition_and_serial_ucl_fallback(config):
    main_thread = threading.get_ident()
    oa_calls = []
    ucl_calls = []

    def prepare(item):
        return Article("Same", doi="10.1000/same"), None, True

    def fetch(article, destination, config, **kwargs):
        assert kwargs == {"allow_ucl": False, "refresh": True}
        oa_calls.append(article)
        raise PdfUnavailable("No OA PDF")

    def browser(article, destination, config, **kwargs):
        assert threading.get_ident() == main_thread
        assert kwargs == {"headless": False}
        ucl_calls.append(article)
        destination.write_bytes(b"%PDF-browser")
        return "ucl"

    with Prefetch(["one", "two", "three"], prepare, fetch, config, jobs=2, refresh=True) as pool:
        paths = [
            pool.result(item, allow_ucl=True, ucl_fetch=browser, show_browser=True)[0]
            for item in pool
        ]
        assert len(set(paths)) == 1
        assert paths[0].read_bytes() == b"%PDF-browser"
    assert len(oa_calls) == len(ucl_calls) == 1
    assert len(list((config.cache_dir / "pdf").glob("*.pdf"))) == 1
    assert not list((config.cache_dir / "pdf").glob("*.tmp.pdf"))


def test_failed_worker_continues_and_cleans_up(config):
    def prepare(item):
        if item == "resolve-error":
            raise LookupError("Missing reference")
        return Article(item), None, True

    def fetch(article, destination, config, **kwargs):
        destination.write_bytes(b"%PDF-partial")
        if article.title == "fetch-error":
            raise OSError("Download interrupted")
        return "oa"

    with Prefetch(
        ["resolve-error", "fetch-error", "success"], prepare, fetch, config, jobs=2
    ) as pool:
        iterator = iter(pool)
        assert isinstance(next(iterator).error, LookupError)
        with pytest.raises(OSError, match="interrupted"):
            pool.result(next(iterator), allow_ucl=False, ucl_fetch=None)
        assert pool.result(next(iterator), allow_ucl=False, ucl_fetch=None)[1] == "oa"
        temporary = Path(pool.temp.name)
    assert not temporary.exists()


def test_cli_local_priority_order_collisions_and_progress_isolation(config, tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    barrier = threading.Barrier(2)
    local = tmp_path / "local.pdf"
    local.write_bytes(b"%PDF-local")
    calls = []
    events = []
    original_event = Reporter.event

    def report_event(self, *args, **kwargs):
        assert threading.get_ident() == main_thread
        events.append((self.label, args[0], args[1]))
        return original_event(self, *args, **kwargs)

    monkeypatch.setattr(Reporter, "event", report_event)

    def resolve(item, *args, **kwargs):
        assert threading.get_ident() == main_thread
        return Article("Same filename", doi=f"10.1000/{item}"), local if item == "local" else None

    def fetch(article, destination, config, **kwargs):
        calls.append(article.doi)
        assert article.doi != "10.1000/local"
        barrier.wait(timeout=3)
        emit("fetch", article.doi)
        destination.write_bytes(b"%PDF-remote")
        return "oa"

    monkeypatch.setattr(cli, "_resolve_item", resolve)
    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    report = tmp_path / "report.json"
    manifest = tmp_path / "manifest.json"
    args = cli._get_parser().parse_args(
        [
            "first",
            "local",
            "last",
            "--jobs",
            "3",
            "--quiet",
            "--no-compress",
            "--report",
            str(report),
            "--manifest",
            str(manifest),
        ]
    )
    assert cli._run_get(args, config) == 0
    rows = json.loads(report.read_text())
    assert [row["input"] for row in rows] == ["first", "local", "last"]
    assert [row["status"] for row in rows] == ["ok"] * 3
    outputs = [Path(row["outputs"][0]) for row in rows]
    assert len(set(outputs)) == 3
    assert outputs[1].read_bytes() == b"%PDF-local"
    assert sorted(calls) == ["10.1000/first", "10.1000/last"]
    assert all(doi in label for label, stage, doi in events if doi.startswith("10.1000/"))
    assert manifest.is_file()


def test_cli_single_job_preserves_sequential_resolution(config, tmp_path, monkeypatch):
    seen = []

    def resolve(item, *args, **kwargs):
        seen.append(f"resolve:{item}")
        return Article(item), None

    def fetch(article, destination, config, **kwargs):
        assert threading.current_thread() is threading.main_thread()
        seen.append(f"fetch:{article.title}")
        destination.write_bytes(b"%PDF-test")
        return "oa"

    monkeypatch.setattr(cli, "_resolve_item", resolve)
    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    args = cli._get_parser().parse_args(["one", "two", "--jobs", "1", "--quiet"])
    assert cli._run_get(args, config) == 0
    assert seen == ["resolve:one", "fetch:one", "resolve:two", "fetch:two"]


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_jobs_option_rejects_invalid_values(value):
    with pytest.raises(SystemExit):
        cli._get_parser().parse_args(["paper", "--jobs", value])


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4"])
def test_jobs_configuration_is_validated(value):
    with pytest.raises(ValueError, match="jobs must be a positive integer"):
        Config(jobs=value)


def test_local_pdf_attribute_bypasses_failed_duplicate_future(config, tmp_path):
    local = tmp_path / "attached.pdf"
    local.write_bytes(b"%PDF-attachment")
    calls = []

    def prepare(item):
        article = Article("Same", doi="10.1000/same")
        if item == "attached":
            article.local_pdf = str(local)
        return article, None, True

    def fetch(article, destination, config, **kwargs):
        calls.append(article)
        raise PdfUnavailable("Remote unavailable")

    with Prefetch(["remote", "attached"], prepare, fetch, config, jobs=2) as pool:
        first, second = list(pool)
        with pytest.raises(PdfUnavailable):
            pool.result(first, allow_ucl=False, ucl_fetch=None)
        assert second.local_pdf == local
        assert second.future is None
    assert len(calls) == 1


def test_live_progress_drains_before_worker_finishes_and_timings_measure_work(config):
    from time import perf_counter

    release = threading.Event()
    observed = []

    def prepare(item):
        release.wait(timeout=0.02)
        return Article(item), None, True

    def fetch(article, destination, config, **kwargs):
        emit("fetch", "Downloading", completed=64, total=128, unit="B")
        assert release.wait(timeout=3), "coordinator did not display progress"
        destination.write_bytes(b"%PDF-test")
        return "oa"

    class ObservingReporter(Reporter):
        def event(self, stage, detail, **kwargs):
            if detail == "Downloading":
                observed.append(kwargs)
                release.set()
            super().event(stage, detail, **kwargs)

    with ObservingReporter(enabled=False):
        with Prefetch(["paper"], prepare, fetch, config, jobs=2) as pool:
            prepared = next(iter(pool))
            started_wait = perf_counter()
            pool.result(prepared, allow_ucl=False, ucl_fetch=None)
            timings = pool.timings(prepared)
            assert timings["resolve"] >= 0.015
            assert timings["fetch"] >= 0.09
            assert perf_counter() - started_wait >= 0.09
    assert observed == [{"completed": 64, "total": 128, "unit": "B"}]


def test_event_buffer_bounds_and_coalesces_download_updates():
    from papr.progress import EventBuffer

    buffer = EventBuffer()
    for count in range(1000):
        buffer.event("fetch", "Downloading", completed=count)
    assert len(buffer.events) == 1
    assert buffer.events[0][2]["completed"] == 999
    for count in range(1000):
        buffer.event("fetch", f"Trying source {count}")
    assert len(buffer.events) == 128


@pytest.mark.parametrize("use_ucl", [False, True])
def test_staging_is_bounded_and_late_duplicates_reuse_cache(config, monkeypatch, use_ucl):
    from papr.fetchers import fetch_pdf, oa

    oa_calls = []
    ucl_calls = []

    def prepare(item):
        return Article(f"Paper {item}", doi=f"10.1000/{item}"), None, True

    def fetch_oa(article, destination, config):
        oa_calls.append(article.doi)
        if use_ucl:
            return None
        destination.write_bytes(b"%PDF-open-access")
        return "oa:test"

    def fetch_ucl(article, destination, config):
        ucl_calls.append(article.doi)
        destination.write_bytes(b"%PDF-institutional")
        return "ucl:test"

    monkeypatch.setattr(oa, "fetch", fetch_oa)
    items = [str(index) for index in range(30)] + ["0"]
    with Prefetch(items, prepare, fetch_pdf, config, jobs=2, refresh=True) as pool:
        directory = Path(pool.temp.name)
        first_path = None
        for index, prepared in enumerate(pool):
            path, source = pool.result(prepared, allow_ucl=True, ucl_fetch=fetch_ucl)
            assert path.read_bytes().startswith(b"%PDF-")
            assert source == ("ucl:test" if use_ucl else "oa:test")
            assert len(list(directory.glob("*.pdf"))) <= 2
            if index == 0:
                first_path = path
            if index == len(items) - 1:
                assert path.parent == config.cache_dir / "pdf"
            pool.release(prepared)
            assert not first_path.exists()
            assert len(list(directory.glob("*.pdf"))) <= 1
        assert not list(directory.iterdir())
    assert len(oa_calls) == 30
    assert len(ucl_calls) == (30 if use_ucl else 0)
    assert len(list((config.cache_dir / "pdf").glob("*.pdf"))) == 30


def test_release_cleans_failed_worker_staging_and_preserves_later_failure(config):
    calls = []

    def prepare(item):
        return Article(item), None, True

    def fetch(article, destination, config, **kwargs):
        calls.append(article.title)
        destination.write_bytes(b"partial-download")
        raise OSError("Download interrupted")

    with Prefetch(["same"] * 20, prepare, fetch, config, jobs=2, refresh=True) as pool:
        directory = Path(pool.temp.name)
        for prepared in pool:
            with pytest.raises(OSError, match="interrupted"):
                pool.result(prepared, allow_ucl=False, ucl_fetch=None)
            pool.release(prepared)
            assert not list(directory.iterdir())
    assert calls == ["same"]


def test_release_waits_for_worker_when_publication_fails_before_result(config):
    started = threading.Event()

    def prepare(item):
        return Article(item), None, True

    def fetch(article, destination, config, **kwargs):
        destination.write_bytes(b"%PDF-in-progress")
        started.set()
        threading.Event().wait(timeout=0.03)
        assert destination.exists(), "staging was removed while the worker was active"
        destination.write_bytes(b"%PDF-finished")
        return "oa"

    with Prefetch(["paper"], prepare, fetch, config, jobs=2) as pool:
        prepared = next(iter(pool))
        assert started.wait(timeout=3)
        pool.release(prepared)
        assert prepared.future.done()
        assert not list(Path(pool.temp.name).iterdir())
