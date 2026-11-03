"""Retain references when acquisition fails and attach the original PDF on retry."""

import json
from pathlib import Path

import pytest
from test_zotero_save import environment  # noqa: F401

from papr import cli, zotero
from papr.batches import load_manifest
from papr.config import Config
from papr.fetchers import PdfUnavailable
from papr.fetchers.diagnostics import PdfAccessError
from papr.model import Article


def test_metadata_save_creates_only_reference_and_can_attach_on_retry(environment):  # noqa: F811
    server, pdf, article, config = environment
    metadata = zotero.save(article, None, config)
    assert metadata["metadata_only"] is True
    assert metadata["status"] == "added"
    assert metadata["item_key"] == "PARENT12"
    assert "attachment_key" not in metadata
    assert list(server.items) == ["PARENT12"]
    assert server.file_payload is None
    server.requests.clear()

    assert zotero.save(article, None, config, interactive=False)["status"] == "already-present"
    assert all(request.method == "GET" for request in server.requests)
    attached = zotero.save(article, pdf, config, interactive=False)
    assert attached["status"] == "attached"
    assert attached["item_key"] == metadata["item_key"]
    assert len(server.items) == 2
    assert server.file_payload == pdf.read_bytes()


def test_metadata_save_keeps_ambiguous_match_protection(environment):  # noqa: F811
    server, _, article, config = environment
    article.doi = None
    article.authors = []
    server.item("OTHER123", {"itemType": "journalArticle", "title": article.title})
    with pytest.raises(zotero.ZoteroError, match="Incomplete metadata"):
        zotero.save(article, None, config)
    assert server.authorizations == 0
    assert not server.collections
    assert list(server.items) == ["OTHER123"]


@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize("formats", ["pdf", "pdf,bib,json", "bib"])
@pytest.mark.parametrize("zotero_fails", [False, True])
@pytest.mark.parametrize("failure", [PdfUnavailable, PdfAccessError])
def test_pdf_failure_retains_metadata_and_original_error(
    tmp_path, monkeypatch, jobs, formats, zotero_fails, failure,
):
    config = Config(download_dir=tmp_path / "out", pdf_compress=False)
    article = Article("A paper", doi="10.1000/paper")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: (article, None))

    def fetch(*args, **kwargs):
        raise failure("Publisher rejected PDF access")

    saved = []

    def save(value, pdf, cfg, **kwargs):
        assert value is article and pdf is None and cfg is config
        saved.append(value)
        if zotero_fails:
            raise zotero.ZoteroError("Zotero is closed")
        return {"status": "added", "item_key": "PARENT12", "metadata_only": True}

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    monkeypatch.setattr(zotero, "save", save)
    report, manifest = tmp_path / "report.json", tmp_path / "manifest.json"
    args = cli._get_parser().parse_args([
        "10.1000/paper", "--zotero", "--oa-only", "--non-interactive", "--quiet",
        "--jobs", str(jobs), "-f", formats, "--filename", "{{title}}",
        "--report", str(report), "--manifest", str(manifest),
    ])
    assert cli._run_get(args, config) == 1
    assert saved == [article]
    row, = json.loads(report.read_text())
    assert row["status"] == "error"
    assert row["error"] == "Publisher rejected PDF access"
    assert row["failed_stage"] == "fetch"
    assert row["pdf_status"] == "unavailable"
    assert row["zotero"]["metadata_only"] is True
    assert row["zotero"]["status"] == ("error" if zotero_fails else "added")
    if zotero_fails:
        assert row["zotero"]["error"] == "Zotero is closed"
    outputs = [Path(value) for value in row["outputs"]]
    expected = {".bib", ".json"} if formats == "pdf,bib,json" else (
        {".bib"} if formats == "bib" else set()
    )
    assert {path.suffix for path in outputs} == expected
    assert all(path.is_file() for path in outputs)
    assert len(row.get("artifacts", [])) == len(outputs)
    if outputs:
        assert row["export_status"] == "partial"
    assert load_manifest(manifest)["items"] == [row]


@pytest.mark.parametrize("zotero_requested", [False, True])
def test_unexpected_acquisition_error_does_not_trigger_metadata_save(
    tmp_path, monkeypatch, zotero_requested,
):
    config = Config(download_dir=tmp_path / "out")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: (Article("A paper"), None))

    def fetch(*args, **kwargs):
        raise OSError("Disk write failed")

    def save(*args, **kwargs):
        pytest.fail("Unexpected errors must not trigger Zotero writes")

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    monkeypatch.setattr(zotero, "save", save)
    options = ["A paper", "--oa-only", "--non-interactive", "--quiet", "--jobs", "1"]
    if zotero_requested:
        options.append("--zotero")
    assert cli._run_get(cli._get_parser().parse_args(options), config) == 1


def test_unavailable_pdf_without_zotero_does_not_write_metadata(tmp_path, monkeypatch):
    config = Config(download_dir=tmp_path / "out")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: (Article("A paper"), None))

    def fetch(*args, **kwargs):
        raise PdfUnavailable("No valid PDF found")

    def save(*args, **kwargs):
        pytest.fail("Saving metadata requires --zotero")

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    monkeypatch.setattr(zotero, "save", save)
    args = cli._get_parser().parse_args([
        "A paper", "--oa-only", "--non-interactive", "--quiet", "--jobs", "1",
    ])
    assert cli._run_get(args, config) == 1
    assert not config.download_dir.exists()


def test_failed_metadata_export_still_saves_zotero_and_preserves_pdf_error(tmp_path, monkeypatch):
    config = Config(download_dir=tmp_path / "out")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: (Article("A paper"), None))

    def fetch(*args, **kwargs):
        raise PdfUnavailable("No valid PDF found")

    def export(*args, **kwargs):
        raise OSError("Metadata output is unwritable")

    saved = []

    def save(article, pdf, config, **kwargs):
        saved.append(article.title)
        return {"status": "added", "metadata_only": True}

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    monkeypatch.setattr(cli, "export_outputs", export)
    monkeypatch.setattr(zotero, "save", save)
    report = tmp_path / "report.json"
    args = cli._get_parser().parse_args([
        "A paper", "--zotero", "--oa-only", "--non-interactive", "--quiet", "--jobs", "1",
        "-f", "pdf,bib", "--report", str(report),
    ])
    assert cli._run_get(args, config) == 1
    assert saved == ["A paper"]
    row, = json.loads(report.read_text())
    assert row["error"] == "No valid PDF found"
    assert row["failed_stage"] == "fetch"
    assert row["metadata_export_error"] == "Metadata output is unwritable"
    assert row["zotero"]["status"] == "added"
