"""Remote retries preserve the combined batch and reuse the fetch pipeline."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from papr import cli
from papr.acquisition import record_attempt
from papr.batches import load_manifest, record_artifacts, save_manifest
from papr.config import Config
from papr.model import Article


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    config = Config(
        download_dir=tmp_path / "output", pdf_compress=False, filename_template="{{title}}"
    )
    monkeypatch.setattr(Config, "load", lambda: config)
    return config


def manifest(tmp_path, config, rows):
    return save_manifest(rows, config, tmp_path / "source.json", output_dir=config.download_dir)


@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize("failure", [False, True])
def test_retry_preserves_successes_partial_exports_and_attempts(
    tmp_path, config, monkeypatch, jobs, failure,
):
    config.download_dir.mkdir()
    pdf = config.download_dir / "Paper.pdf"
    pdf.write_bytes(b"existing successful PDF")
    bibliography = config.download_dir / "Paper.bib"
    bibliography.write_text("original bibliography")
    completed = {
        "input": "completed", "status": "ok",
        **record_artifacts(Article("Completed"), [pdf], config),
    }
    partial = {
        "input": "original citation", "status": "error", "failed_stage": "fetch",
        "error": "old acquisition failure", "export_status": "partial",
        "outputs": [str(bibliography)],
        "zotero": {"status": "created", "metadata_only": True},
        "acquisition_attempts": [{"stage": "old-attempt"}],
        **record_artifacts(Article("Paper", doi="10.1000/paper"), [bibliography], config),
    }
    unresolved = {"input": "missing reference", "status": "error", "failed_stage": "resolve"}
    untouched = [
        {"input": "converter", "status": "error", "failed_stage": "process"},
        {"input": "Zotero", "status": "error", "failed_stage": "zotero"},
        {"input": "/private/import.json", "status": "error", "failed_stage": "input"},
    ]
    rows = [completed, partial, unresolved, *untouched]
    source = manifest(tmp_path, config, rows)
    original = source.read_bytes()
    resolved, fetched = [], []

    def resolve(item, cfg, interactive, **options):
        assert not interactive
        assert options == {"use_local": False, "use_remote": True, "enrich": True}
        resolved.append(item)
        if isinstance(item, str):
            assert item == "missing reference"
            raise LookupError("still unresolved")
        assert item.local_pdf is None
        article = deepcopy(item)
        article.oa_pdf_url = "https://example.org/refreshed.pdf"
        return article, None

    def fetch(article, destination, cfg, **options):
        assert article.oa_pdf_url == "https://example.org/refreshed.pdf"
        assert options["allow_ucl"] is False
        if jobs == 1:
            assert options["show_browser"] is True
        fetched.append(article.title)
        record_attempt("retry-source", article.oa_pdf_url, reason="retry")
        if failure:
            raise RuntimeError("still unavailable")
        destination.write_bytes(b"%PDF-1.7\nnew PDF")
        return "retry-source"

    monkeypatch.setattr(cli, "_resolve_item", resolve)
    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    monkeypatch.setattr("papr.zotero.save", lambda *a, **kw: pytest.fail("no Zotero write"))
    result = tmp_path / "result.json"
    report = tmp_path / "report.json"
    assert cli.main([
        "batch", "retry", str(source), "--manifest", str(result), "--report", str(report),
        "--jobs", str(jobs), "--oa-only", "--show-browser", "--non-interactive", "--name", "retry",
    ]) == 1
    assert source.read_bytes() == original
    combined = load_manifest(result)["items"]
    assert combined == json.loads(report.read_text())
    assert [row["input"] for row in combined] == [row["input"] for row in rows]
    assert combined[0] == completed
    assert combined[3:] == untouched
    assert combined[2]["status"] == "error"
    assert combined[2]["error"] == "still unresolved"
    retried = combined[1]
    assert retried["status"] == ("error" if failure else "ok")
    assert retried["acquisition_attempts"][0] == partial["acquisition_attempts"][0]
    assert len(retried["acquisition_attempts"]) == 2
    assert retried["zotero"] == partial["zotero"]
    assert retried["artifacts"][0] == partial["artifacts"][0]
    assert str(bibliography) in retried["outputs"]
    assert bibliography.read_text() == "original bibliography"
    assert pdf.read_bytes() == b"existing successful PDF"
    assert fetched == ["Paper"] and len(resolved) == 2
    if failure:
        assert retried["export_status"] == "partial"
    else:
        assert "error" not in retried and "failed_stage" not in retried
        assert any(Path(path).name.endswith("_2.pdf") for path in retried["outputs"])


def test_dry_run_does_not_resolve_or_write_even_with_report_paths(tmp_path, config, monkeypatch):
    source = manifest(tmp_path, config, [
        {"input": "10.1000/paper", "status": "error", "failed_stage": "resolve"},
    ])
    original = source.read_bytes()
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: pytest.fail("no network"))
    monkeypatch.setattr(cli, "fetch_pdf", lambda *a, **kw: pytest.fail("no fetching"))
    report = tmp_path / "report.json"
    assert cli.main([
        "batch", "retry", str(source), "--dry-run", "--manifest", str(source),
        "--report", str(report), "--name", "dryrun", "-o", str(tmp_path / "absent"),
    ]) == 0
    assert source.read_bytes() == original
    assert not report.exists() and not (tmp_path / "absent").exists()


def test_same_manifest_path_is_safe_and_identity_change_keeps_original(
    tmp_path, config, monkeypatch,
):
    article = Article("Original", doi="10.1000/original", local_pdf="/private/secret.pdf")
    row = {
        "input": "old citation", "status": "error", "failed_stage": "fetch",
        "article": article.to_dict(), "acquisition_attempts": [{"stage": "old"}],
    }
    source = manifest(tmp_path, config, [row])

    def resolve(item, *args, **options):
        assert item.local_pdf is None
        return Article("Unrelated", doi="10.1000/different"), None

    monkeypatch.setattr(cli, "_resolve_item", resolve)
    monkeypatch.setattr(cli, "fetch_pdf", lambda *a, **kw: pytest.fail("identity must match"))
    assert cli.main([
        "batch", "retry", str(source), "--manifest", str(source), "--non-interactive",
    ]) == 1
    retried = load_manifest(source)["items"][0]
    assert retried["article"] == row["article"]
    assert "identity" in retried["error"]
    assert retried["acquisition_attempts"] == row["acquisition_attempts"]


@pytest.mark.parametrize("extra", [
    ["-f", "md"], ["--name", "../invalid"], ["--report", "SOURCE"],
])
def test_invalid_options_fail_before_remote_work(tmp_path, config, monkeypatch, extra):
    source = manifest(tmp_path, config, [
        {"input": "10.1000/paper", "status": "error", "failed_stage": "resolve"},
    ])
    extra = [str(source) if value == "SOURCE" else value for value in extra]
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: pytest.fail("no network"))
    assert cli.main(["batch", "retry", str(source), *extra]) == 1


def test_saved_paths_and_pdf_successes_are_not_remote_retry_inputs(tmp_path, config, monkeypatch):
    rows = [
        {"input": "/private/ref.pdf", "status": "error", "failed_stage": "resolve"},
        {"input": "failed.refs", "status": "error", "failed_stage": "resolve"},
        {"input": "partially finished", "status": "error", "failed_stage": "fetch",
         "outputs": ["/already/exported.pdf"]},
        {"input": "excluded", "status": "excluded"},
        {"input": "planned", "status": "dry-run"},
    ]
    source = manifest(tmp_path, config, rows)
    target = tmp_path / "result.json"
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: pytest.fail("no remote work"))
    assert cli.main(["batch", "retry", str(source), "--manifest", str(target)]) == 1
    assert load_manifest(target)["items"] == rows


@pytest.mark.parametrize("overwrite", [False, True])
def test_named_selector_output_formats_and_explicit_overwrite(
    tmp_path, config, monkeypatch, overwrite,
):
    source = save_manifest(
        [{"input": "10.1000/new", "status": "error", "failed_stage": "resolve"}],
        config, tmp_path / "source.json", name="named-source",
    )
    original = source.read_bytes()
    output = tmp_path / "custom-output"
    output.mkdir()
    existing = output / "New.pdf"
    existing.write_bytes(b"original")
    monkeypatch.setattr(
        cli, "_resolve_item", lambda *a, **kw: (Article("New", doi="10.1000/new"), None)
    )

    def fetch(article, destination, config, **options):
        destination.write_bytes(b"%PDF-1.7\nnew")
        return "retry-source"

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    report = tmp_path / "report.json"
    extra = ["--overwrite"] if overwrite else []
    assert cli.main([
        "batch", "retry", "named-source", "-o", str(output), "-f", "pdf,bib",
        "--report", str(report), "--non-interactive", *extra,
    ]) == 0
    row = json.loads(report.read_text())[0]
    expected = existing if overwrite else output / "New_2.pdf"
    assert set(row["outputs"]) == {str(expected), str(output / "New.bib")}
    assert expected.read_bytes() == b"%PDF-1.7\nnew"
    assert existing.read_bytes() == (b"%PDF-1.7\nnew" if overwrite else b"original")
    assert source.read_bytes() == original
    new_manifests = [
        path for path in (config.data_dir / "batches").glob("*.json")
        if not path.name.startswith(".")
    ]
    assert len(new_manifests) == 1
    assert load_manifest(new_manifests[0])["items"] == [row]
