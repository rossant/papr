import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from papr import cli, compression, history, zotero
from papr.config import Config
from papr.model import Article


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    monkeypatch.setattr(Config, "config_dir", property(lambda self: tmp_path / "settings"))
    config = Config(download_dir=tmp_path / "output", zotero_collection="SBS")
    monkeypatch.setattr(cli.Config, "load", lambda: config)
    return config


def make_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(path)
    return path


def test_auto_compression_exports_copy_records_original_metadata(config, tmp_path, monkeypatch):
    original = make_pdf(tmp_path / "source.pdf")
    prepared = make_pdf(tmp_path / "prepared.pdf")
    article = Article("A paper", year=2024, doi="10.1000/example")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (article, original))
    seen = []

    def compress(pdf, cfg, *, enabled=None):
        seen.append((pdf, enabled))
        return prepared, {"status": "compressed"}

    monkeypatch.setattr(compression, "compress_pdf", compress)
    before = original.read_bytes()
    args = cli._get_parser().parse_args(["reference", "--quiet"])
    assert cli._run_get(args, config) == 0
    assert seen == [(original, None)]
    assert original.read_bytes() == before
    remembered, exported = history.load_latest(config)
    assert remembered == article
    assert exported.parent == config.download_dir
    assert exported.read_bytes() == prepared.read_bytes()


def test_no_compress_passes_disabled_and_still_records_download(config, tmp_path, monkeypatch):
    original = make_pdf(tmp_path / "source.pdf")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article("Paper"), original))
    enabled_flags = []

    def compress(pdf, cfg, *, enabled=None):
        enabled_flags.append(enabled)
        return pdf, {"status": "disabled"}

    monkeypatch.setattr(compression, "compress_pdf", compress)
    assert (
        cli._run_get(cli._get_parser().parse_args(["paper", "--quiet", "--no-compress"]), config)
        == 0
    )
    assert enabled_flags == [False]
    assert history.load_latest(config)[1].exists()


def test_zotero_failure_preserves_export_history_and_reports_stage(config, tmp_path, monkeypatch):
    original = make_pdf(tmp_path / "source.pdf")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article("Paper"), original))

    def fail(article, pdf, cfg, **kwargs):
        cli.emit("zotero", "Saving paper")
        raise zotero.ZoteroError("API disabled")

    monkeypatch.setattr(zotero, "save", fail)
    report = tmp_path / "report.json"
    args = cli._get_parser().parse_args(["paper", "--zotero", "--quiet", "--report", str(report)])
    assert cli._run_get(args, config) == 1
    row = json.loads(report.read_text())[0]
    assert row["status"] == "error" and row["failed_stage"] == "zotero"
    assert all(Path(path).exists() for path in row["outputs"])
    assert history.load_latest(config)[1].exists()


def test_latest_shortcut_reuses_saved_metadata_and_personal_collection(
    config, tmp_path, monkeypatch
):
    pdf = make_pdf(tmp_path / "download.pdf")
    article = Article("The last paper", year=2024)
    history.record_download(article, pdf, config)
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: pytest.fail("must not resolve again"))
    calls = []

    def save(item, path, cfg, **kwargs):
        calls.append((item, path, kwargs))
        return {"status": "added", "collection": kwargs["collection"]}

    monkeypatch.setattr(zotero, "save", save)
    monkeypatch.setattr(compression, "compress_pdf", lambda pdf, cfg, **k: (pdf, {}))
    assert cli.main(["zotero", "last", "--quiet"]) == 0
    assert cli.main(["zotero", "--quiet", "--collection", "Other"]) == 0
    assert calls[0][0] == article and calls[0][1] == pdf.resolve()
    assert [call[2]["collection"] for call in calls] == ["SBS", "Other"]


def test_zotero_latest_dry_run_does_not_write_or_compress(config, tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "download.pdf")
    history.record_download(Article("Paper"), pdf, config)
    monkeypatch.setattr(zotero, "save", lambda *a, **k: pytest.fail("must not write"))
    monkeypatch.setattr(
        compression, "compress_pdf", lambda *a, **k: pytest.fail("must not compress")
    )
    assert cli.main(["zotero", "last", "--dry-run", "--quiet"]) == 0


def test_standalone_compression_default_preserves_original(config, tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "paper.pdf")
    prepared = make_pdf(tmp_path / "small.pdf")
    source.write_bytes(source.read_bytes() + b"\n" * 1000)
    original = source.read_bytes()
    result = {
        "status": "compressed",
        "saved_bytes": 1000,
        "original_bytes": len(original),
        "output_bytes": prepared.stat().st_size,
        "savings_percent": 50,
    }
    monkeypatch.setattr(compression, "compress_pdf", lambda *a, **k: (prepared, result))
    assert cli.main(["compress", str(source), "--quiet"]) == 0
    assert source.read_bytes() == original
    destination = source.with_name("paper.compressed.pdf")
    assert destination.read_bytes() == prepared.read_bytes()
    assert cli.main(["compress", str(source), "--quiet"]) == 1
    assert cli.main(["compress", str(source), "--overwrite", "--quiet"]) == 0


def test_in_place_alias_and_dpi_override(config, tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "paper.pdf")
    source.write_bytes(source.read_bytes() + b"\n" * 1000)
    prepared = make_pdf(tmp_path / "small.pdf")
    seen = []

    def compress(pdf, cfg, **kwargs):
        seen.append((cfg.pdf_compression_dpi, kwargs["enabled"]))
        return prepared, {
            "status": "compressed",
            "saved_bytes": 1000,
            "original_bytes": source.stat().st_size,
            "output_bytes": prepared.stat().st_size,
            "savings_percent": 50,
        }

    monkeypatch.setattr(compression, "compress_pdf", compress)
    assert cli.main(["compress", "-i", str(source), "--dpi", "150", "--quiet"]) == 0
    assert source.read_bytes() == prepared.read_bytes()
    assert seen == [(150, True)]
    assert not source.with_name("paper.compressed.pdf").exists()


def test_in_place_compressor_failure_preserves_original_and_continues(
    config, tmp_path, monkeypatch
):
    first = make_pdf(tmp_path / "first.pdf")
    second = make_pdf(tmp_path / "second.pdf")
    originals = [first.read_bytes(), second.read_bytes()]
    calls = []

    def compress(pdf, cfg, **kwargs):
        calls.append(pdf)
        return pdf, {"status": "failed", "reason": "timeout", "saved_bytes": 0}

    monkeypatch.setattr(compression, "compress_pdf", compress)
    assert cli.main(["compress", "-i", str(first), str(second), "--quiet"]) == 1
    assert calls == [first, second]
    assert [first.read_bytes(), second.read_bytes()] == originals


def test_conflicting_in_place_output_and_invalid_dpi_do_not_compress(config, tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        compression, "compress_pdf", lambda *a, **k: pytest.fail("must not compress")
    )
    assert cli.main(["compress", "-i", str(pdf), "-o", str(tmp_path), "--quiet"]) == 1
    assert cli.main(["compress", str(pdf), "--dpi", "0", "--quiet"]) == 1


def test_export_processing_uses_original_while_pdf_copy_is_compressed(
    config, tmp_path, monkeypatch
):
    from papr import export

    original = make_pdf(tmp_path / "source.pdf")
    prepared = make_pdf(tmp_path / "compressed.pdf")
    prepared.write_bytes(prepared.read_bytes() + b"\n% compressed variant\n")
    seen = []

    def markdown(pdf, cfg, backend):
        seen.append(pdf)
        return "Original OCR text\n", "native"

    monkeypatch.setattr(export, "_cached_markdown", markdown)
    outputs, _ = export.export_outputs(
        Article("Paper"),
        original,
        ["pdf", "md"],
        config.download_dir,
        config,
        pdf_output=prepared,
    )
    assert seen == [original]
    assert outputs[0].read_bytes() == prepared.read_bytes()
    assert outputs[1].read_text() == "Original OCR text\n"


def test_atomic_in_place_copy_failure_preserves_destination(config, tmp_path, monkeypatch):
    import shutil

    original = make_pdf(tmp_path / "paper.pdf")
    prepared = make_pdf(tmp_path / "smaller.pdf")
    before = original.read_bytes()

    def fail(source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        cli._atomic_pdf_copy(prepared, original, overwrite=True)
    assert original.read_bytes() == before
    assert set(tmp_path.iterdir()) == {original, prepared}


def test_compress_corrupt_pdf_errors_without_creating_output(config, tmp_path):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"not a pdf")
    assert cli.main(["compress", "-i", str(pdf), "--quiet"]) == 1
    assert pdf.read_bytes() == b"not a pdf"
    assert not pdf.with_name("broken.compressed.pdf").exists()


def test_group_dry_run_checks_library_identity_without_saving(
    config, tmp_path, monkeypatch, capsys
):
    pdf = make_pdf(tmp_path / "download.pdf")
    history.record_download(Article("Paper"), pdf, config)
    config.zotero_group_id = 5593385
    config.zotero_collection = None
    inspected = []

    def info(cfg):
        inspected.append(cfg.zotero_group_id)
        return {"library": "SBS", "group_id": 5593385}

    monkeypatch.setattr(zotero, "library_info", info)
    monkeypatch.setattr(zotero, "save", lambda *a, **k: pytest.fail("must not save"))
    monkeypatch.setattr(
        compression, "compress_pdf", lambda *a, **k: pytest.fail("must not compress")
    )
    assert cli.main(["zotero", "last", "--dry-run", "--quiet"]) == 0
    output = capsys.readouterr().out
    assert "Library: SBS (group 5593385)" in output
    assert "Collection:" not in output
    assert inspected == [5593385]
