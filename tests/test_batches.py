import json

import pytest
from pypdf import PdfWriter

from papr import cli
from papr.artifacts import cache_path
from papr.batches import (
    delivered_identities,
    export_batch,
    identities,
    identity,
    load_manifest,
    record_artifacts,
    save_manifest,
)
from papr.config import Config
from papr.model import Article


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    config = Config(download_dir=tmp_path / "Downloads", pdf_compress=False)
    monkeypatch.setattr(Config, "load", lambda: config)
    return config


def batch(tmp_path, config, article=None):
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    metadata = tmp_path / "paper.csl.json"
    metadata.write_text('[{"title":"Paper"}]')
    row = {
        "status": "ok",
        "input": "10.1000/example",
        "source": "local",
        "compression": {"status": "disabled"},
        **record_artifacts(
            article or Article("Paper", doi="10.1000/example"), [pdf, metadata], config
        ),
    }
    return save_manifest([row], config, tmp_path / "batch.json"), pdf, metadata


def test_offline_reexport_after_original_files_moved(tmp_path, config, monkeypatch):
    manifest, pdf, metadata = batch(tmp_path, config)
    expected = pdf.read_bytes()
    pdf.rename(tmp_path / "moved.pdf")
    metadata.unlink()
    monkeypatch.setattr("papr.resolvers.resolve", lambda *a, **k: pytest.fail("no resolution"))
    output = tmp_path / "elsewhere"
    assert cli.main(["batch", "export", str(manifest), "-o", str(output)]) == 0
    assert (output / "paper.pdf").read_bytes() == expected
    assert json.loads((output / "paper.csl.json").read_text())[0]["title"] == "Paper"
    assert cli.main(["batch", "check", str(manifest)]) == 0


@pytest.mark.parametrize("failure", ["modified", "missing", "corrupt-cache"])
def test_entire_batch_preflight_prevents_partial_export(tmp_path, config, failure):
    manifest, pdf, metadata = batch(tmp_path, config)
    artifact = load_manifest(manifest)["items"][0]["artifacts"][1]
    if failure == "modified":
        metadata.write_text("changed")
    else:
        metadata.unlink()
        cached = cache_path(artifact["sha256"], config)
        if failure == "missing":
            cached.unlink()
        else:
            cached.write_text("corrupt")
    output = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match="changed|missing"):
        export_batch(manifest, output, config)
    assert not output.exists()
    assert pdf.exists()


def test_same_destination_reuses_identical_files_but_preserves_other_content(tmp_path, config):
    manifest, pdf, _ = batch(tmp_path, config)
    output = tmp_path / "elsewhere"
    export_batch(manifest, output, config)
    export_batch(manifest, output, config)
    assert sorted(p.name for p in output.iterdir()) == ["paper.csl.json", "paper.pdf"]
    (output / "paper.pdf").write_bytes(b"other content")
    export_batch(manifest, output, config)
    assert (output / "paper.pdf").read_bytes() == b"other content"
    assert (output / "paper_2.pdf").read_bytes() == pdf.read_bytes()


def test_exclusions_use_successful_pdf_identity_not_title(tmp_path, config):
    manifest, _, _ = batch(tmp_path, config)
    rows = export_batch(manifest, tmp_path / "out", config, exclude=[manifest])
    assert rows[0]["status"] == "excluded"
    assert not (tmp_path / "out").exists()
    data = load_manifest(manifest)
    data["items"][0]["status"] = "error"
    manifest.write_text(json.dumps(data))
    assert not delivered_identities([manifest])
    assert identity(Article("Paper")) == ""
    assert identity(Article("Paper", doi="10.1000/EXAMPLE")) == "doi:10.1000/example"


def test_dry_run_has_no_files_history_or_new_manifest(tmp_path, config):
    manifest, _, _ = batch(tmp_path, config)
    output = tmp_path / "elsewhere"
    assert cli.main(["batch", "export", str(manifest), "-o", str(output), "--dry-run"]) == 0
    assert not output.exists()
    assert not (config.data_dir / "latest-download.json").exists()
    assert not (config.data_dir / "batches").exists()


@pytest.mark.parametrize("bad_name", ["../escape.pdf", "/escape.pdf", "..", "bad\\name.pdf"])
def test_manifest_rejects_destination_traversal(tmp_path, config, bad_name):
    manifest, _, _ = batch(tmp_path, config)
    data = load_manifest(manifest)
    data["items"][0]["artifacts"][0]["name"] = bad_name
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="artifact path"):
        export_batch(manifest, tmp_path / "out", config)


def test_normal_get_records_provenance_hashes_and_excludes_before_fetch(
    tmp_path, config, monkeypatch
):
    initial, pdf, _ = batch(tmp_path, config)
    article = Article(
        "Paper", doi="10.1000/example", source="zotero", source_key="ABC123", source_library="2"
    )
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (article, pdf))
    manifest = tmp_path / "new.json"
    assert cli.main(["10.1000/example", "--manifest", str(manifest), "--quiet"]) == 0
    row = load_manifest(manifest)["items"][0]
    assert row["article"]["source_key"] == "ABC123"
    assert row["artifacts"][0]["sha256"]
    assert row["compression"]["status"] == "disabled"
    monkeypatch.setattr(cli, "fetch_pdf", lambda *a, **k: pytest.fail("excluded before fetch"))
    assert (
        cli.main(
            [
                "10.1000/example",
                "--exclude-delivered",
                str(initial),
                "--manifest",
                str(tmp_path / "excluded.json"),
                "--quiet",
            ]
        )
        == 0
    )
    assert load_manifest(tmp_path / "excluded.json")["items"][0]["status"] == "excluded"


def test_get_records_failed_inputs_in_persistent_manifest(tmp_path, config):
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    manifest = tmp_path / "failed.json"
    assert cli.main([str(bad), "--manifest", str(manifest), "--quiet"]) == 1
    assert load_manifest(manifest)["items"][0]["failed_stage"] == "input"


def test_enrichment_preserves_delivered_identity_alias(tmp_path, config):
    old, pdf, metadata = batch(tmp_path, config, Article("Paper", pmid="12345"))
    richer = Article("Paper", doi="10.1000/example", pmid="12345")
    new = save_manifest(
        [{"status": "ok", **record_artifacts(richer, [pdf], config)}],
        config,
        tmp_path / "richer.json",
    )
    assert identities(richer) & delivered_identities([old]) == {"pmid:12345"}
    assert export_batch(new, tmp_path / "out", config, exclude=[old])[0]["status"] == "excluded"


def test_zotero_failure_keeps_successful_pdf_available_for_reexport(tmp_path, config, monkeypatch):
    _, pdf, _ = batch(tmp_path, config)
    article = Article("Paper", doi="10.1000/example")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (article, pdf))

    def fail(*args, **kwargs):
        raise RuntimeError("Zotero disabled")

    monkeypatch.setattr("papr.zotero.save", fail)
    manifest = tmp_path / "failed-zotero.json"
    assert cli.main(["10.1000/example", "--zotero", "--manifest", str(manifest), "--quiet"]) == 1
    row = load_manifest(manifest)["items"][0]
    assert row["status"] == "error" and row["export_status"] == "ok"
    assert "doi:10.1000/example" in delivered_identities([manifest])
    assert export_batch(manifest, tmp_path / "out", config)[0]["status"] == "ok"


def test_report_failure_still_records_manifest(tmp_path, config, monkeypatch):
    _, pdf, _ = batch(tmp_path, config)
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article("Paper"), pdf))
    manifest = tmp_path / "persistent.json"
    assert (
        cli.main(["Paper", "--report", str(tmp_path), "--manifest", str(manifest), "--quiet"]) == 1
    )
    assert load_manifest(manifest)["items"][0]["export_status"] == "ok"


def test_mid_copy_failure_records_partial_manifest_and_can_retry(tmp_path, config, monkeypatch):
    import shutil

    manifest, pdf, metadata = batch(tmp_path, config)
    original = shutil.copyfile

    def fail_metadata(source, destination, *args, **kwargs):
        if source == metadata:
            raise OSError("disk error")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", fail_metadata)
    partial = tmp_path / "partial.json"
    output = tmp_path / "out"
    assert (
        cli.main(["batch", "export", str(manifest), "-o", str(output), "--manifest", str(partial)])
        == 1
    )
    row = load_manifest(partial)["items"][0]
    assert row["export_status"] == "partial"
    assert len(row["artifacts"]) == len(row["pending_artifacts"]) == 1
    assert (output / "paper.pdf").read_bytes() == pdf.read_bytes()
    monkeypatch.setattr(shutil, "copyfile", original)
    rows = export_batch(partial, output, config)
    assert rows[0]["status"] == "ok"
    assert not rows[0].get("pending_artifacts")
    assert len(list(output.iterdir())) == 2


def test_dry_run_reserves_collisions_like_real_export(tmp_path, config):
    first, pdf, _ = batch(tmp_path, config)
    other = tmp_path / "other"
    other.mkdir()
    second = other / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(second)
    rows = load_manifest(first)["items"]
    rows.append({"status": "ok", **record_artifacts(Article("Other"), [second], config)})
    manifest = save_manifest(rows, config, tmp_path / "both.json")
    output = tmp_path / "out"
    planned = export_batch(manifest, output, config, dry_run=True)
    actual = export_batch(manifest, output, config)
    assert [r["outputs"] for r in planned] == [r["outputs"] for r in actual]
    assert actual[-1]["outputs"] == [str(output / "paper_2.pdf")]


def test_batch_check_includes_pending_artifacts(tmp_path, config):
    manifest, pdf, metadata = batch(tmp_path, config)
    data = load_manifest(manifest)
    row = data["items"][0]
    row.update(status="error", export_status="partial")
    row["pending_artifacts"] = [row["artifacts"].pop()]
    manifest.write_text(json.dumps(data))
    metadata.unlink()
    cache_path(row["pending_artifacts"][0]["sha256"], config).unlink()
    assert cli.main(["batch", "check", str(manifest)]) == 1
    assert pdf.exists()


def test_document_type_filter_excludes_before_pdf_fetch(tmp_path, config, monkeypatch):
    article = Article("Meeting abstract", doi="10.1000/example", item_type="paper-conference")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (article, None))
    monkeypatch.setattr(cli, "fetch_pdf", lambda *a, **k: pytest.fail("must not fetch"))
    manifest = tmp_path / "filtered.json"
    assert (
        cli.main(
            [
                "10.1000/example",
                "--item-type",
                "article-journal",
                "--manifest",
                str(manifest),
                "--quiet",
            ]
        )
        == 0
    )
    row = load_manifest(manifest)["items"][0]
    assert row["status"] == "excluded"
    assert row["exclusion_reason"] == "document type: paper-conference"


def test_explicit_enrichment_reaches_pdf_resolution(tmp_path, config, monkeypatch):
    from papr.input import materialize_article

    _, pdf, _ = batch(tmp_path, config)
    seen = []

    def resolve(query, cfg, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr("papr.local.resolve", resolve)
    article, _ = materialize_article(pdf, config, enrich=True)
    assert article.item_type == "document"
    assert seen and all(options["enrich"] is True for options in seen)
