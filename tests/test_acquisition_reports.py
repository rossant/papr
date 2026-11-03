import json

import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr import cli
from papr.acquisition import capture_attempts, public_url, record_attempt
from papr.config import Config
from papr.fetchers import PdfUnavailable, oa
from papr.model import Article
from papr.prefetch import Prefetch


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    return Config(download_dir=tmp_path / "out", pdf_compress=False)


def test_attempt_urls_remove_credentials_query_and_fragment():
    attempts = []
    with capture_attempts(attempts):
        record_attempt("oa", "https://user:password@publisher.test/pdf?token=secret#private",
                       reason="HTTP 403", status_code=403, recovery="Try another source")
    assert attempts == [{
        "source": "oa", "url": "https://publisher.test/pdf", "outcome": "failed",
        "reason": "HTTP 403", "status_code": 403, "recovery": "Try another source",
    }]
    assert public_url("file:///private/paper.pdf") is None
    assert public_url("https://publisher.test:invalid/pdf") is None
    record_attempt("ignored", outcome="success")
    assert len(attempts) == 1


def test_http_attempts_keep_blocked_sources_before_success(config, tmp_path, monkeypatch):
    original = pdf_bytes()

    def respond(request):
        if request.url.path == "/blocked.pdf":
            return httpx.Response(403)
        if request.url.path == "/captcha.pdf":
            return httpx.Response(200, text='<base href="recaptcha/challengepage/">')
        if request.url.path == "/broken.pdf":
            return httpx.Response(200, content=b"%PDF-1.7\ntruncated")
        return httpx.Response(200, content=original)

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    article = Article("Paper", oa_pdf_urls=[
        f"https://publisher.test/{path}.pdf?token=secret"
        for path in ["blocked", "captcha", "broken", "original"]
    ])
    attempts = []
    with capture_attempts(attempts):
        assert oa.fetch(article, tmp_path / "paper.pdf", config)
    assert [row["outcome"] for row in attempts] == ["failed", "failed", "failed", "success"]
    assert attempts[0]["status_code"] == 403
    assert "reCAPTCHA" in attempts[1]["reason"]
    assert "secret" not in json.dumps(attempts)
    assert (tmp_path / "paper.pdf").read_bytes() == original


@pytest.mark.parametrize("jobs", [1, 2])
def test_failed_attempts_survive_cli_reports_and_manifests(
    config, tmp_path, monkeypatch, jobs,
):
    monkeypatch.setattr(cli, "_resolve_item", lambda item, *a, **k: (Article(item), None))

    def fetch(article, destination, *args, **kwargs):
        record_attempt("oa", f"https://publisher.test/{article.title}?token=secret",
                       reason="HTTP 403", recovery="Retry with --show-browser")
        raise PdfUnavailable("No valid PDF found")

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    report, manifest = tmp_path / "report.json", tmp_path / "manifest.json"
    args = cli._get_parser().parse_args([
        "one", "two", "--jobs", str(jobs), "--quiet", "--oa-only",
        "--report", str(report), "--manifest", str(manifest),
    ])
    assert cli._run_get(args, config) == 1
    rows = json.loads(report.read_text())
    assert json.loads(manifest.read_text())["items"] == rows
    for row in rows:
        assert row["status"] == "error"
        assert row["acquisition_attempts"] == [{
            "source": "oa", "url": f"https://publisher.test/{row['input']}",
            "outcome": "failed", "reason": "HTTP 403", "recovery": "Retry with --show-browser",
        }]


def test_shared_prefetch_attempts_include_fallback_for_every_duplicate(config):
    calls = []

    def prepare(item):
        return Article("Paper", doi="10.1000/same"), None, True

    def fetch(*args, **kwargs):
        calls.append("oa")
        record_attempt("oa", "https://publisher.test/pdf?token=secret", reason="HTTP 403")
        raise PdfUnavailable("Blocked")

    def browser(article, destination, *args, **kwargs):
        calls.append("ucl")
        record_attempt("browser", "https://publisher.test/pdf?token=secret", outcome="success")
        destination.write_bytes(pdf_bytes())
        return "https://publisher.test/pdf"

    with Prefetch(["one", "two", "three"], prepare, fetch, config, jobs=2) as pool:
        logs = []
        for prepared in pool:
            pool.result(prepared, allow_ucl=True, ucl_fetch=browser)
            pool.release(prepared)
            logs.append(pool.attempts(prepared))
    assert calls == ["oa", "ucl"]
    assert logs[0] == logs[1] == logs[2]
    assert [row["source"] for row in logs[0]] == ["oa", "browser"]


def test_discovered_publisher_links_report_each_failed_response(config, tmp_path, monkeypatch):
    def respond(request):
        if request.url.path == "/paper":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                '<meta name="citation_pdf_url" content="/paper/pdf?token=secret">'
            ))
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<base href="https://www.google.com/recaptcha/challengepage/">'
        ))

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    attempts = []
    with capture_attempts(attempts):
        assert oa.fetch(Article("Paper", url="https://publisher.test/paper"),
                        tmp_path / "paper.pdf", config) is None
    assert attempts[0]["outcome"] == "discovered"
    assert attempts[1]["url"] == "https://publisher.test/paper/pdf"
    assert "reCAPTCHA" in attempts[1]["reason"]
    assert attempts[1]["recovery"].startswith("Retry with --show-browser")


def test_ucl_unconfigured_profile_has_actionable_attempt(config, tmp_path, monkeypatch):
    from papr.fetchers import ucl

    monkeypatch.setattr(Config, "ucl_profile_dir", property(lambda self: tmp_path / "missing"))
    attempts = []
    with capture_attempts(attempts):
        assert ucl.fetch(Article("Paper"), tmp_path / "paper.pdf", config) is None
    assert attempts == [{
        "source": "ucl", "outcome": "unavailable", "reason": "No saved browser profile",
        "recovery": "Run papr login ucl before retrying institutional access",
    }]


def test_prefetch_records_richer_duplicate_used_after_first_failure(config):
    def prepare(item):
        article = Article("Paper", doi="10.1000/same", url=f"https://publisher.test/{item}")
        return article, None, True

    def fetch(article, destination, *args, **kwargs):
        success = article.url.endswith("richer")
        record_attempt("oa", article.url, outcome="success" if success else "failed")
        if not success:
            raise PdfUnavailable("No PDF")
        destination.write_bytes(pdf_bytes())
        return article.url

    with Prefetch(["first", "richer"], prepare, fetch, config, jobs=2) as pool:
        iterator = iter(pool)
        first = next(iterator)
        assert pool.result(first, allow_ucl=True, ucl_fetch=None)[1].endswith("richer")
        pool.release(first)
        assert [attempt["outcome"] for attempt in pool.attempts(first)] == ["failed", "success"]
        richer = next(iterator)
        pool.result(richer, allow_ucl=False, ucl_fetch=None)
        pool.release(richer)
        assert [attempt["outcome"] for attempt in pool.attempts(richer)] == ["success"]


def test_corrupt_linked_pdf_does_not_hide_valid_alternative(config, tmp_path, monkeypatch):
    original = pdf_bytes()

    def respond(request):
        if request.url.path == "/paper":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                '<meta name="citation_pdf_url" content="/paper/broken.pdf">'
                '<a href="/paper/valid.pdf">PDF</a>'
            ))
        if request.url.path == "/paper/broken.pdf":
            return httpx.Response(200, content=b"%PDF-1.7\ntruncated")
        return httpx.Response(200, content=original)

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    attempts = []
    with capture_attempts(attempts):
        assert oa.fetch(Article("Paper", url="https://publisher.test/paper"),
                        tmp_path / "paper.pdf", config) == "https://publisher.test/paper/valid.pdf"
    assert [attempt["outcome"] for attempt in attempts] == ["discovered", "failed", "success"]
    assert (tmp_path / "paper.pdf").read_bytes() == original
