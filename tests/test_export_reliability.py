import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from pypdf import PdfWriter

from papr import artifacts, cli, export, history
from papr.batches import delivered_identities, export_batch, load_manifest
from papr.config import Config
from papr.model import Article


@pytest.mark.parametrize("format_name", ["pdf", "json"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_interrupted_export_preserves_existing_output_and_cleans_temporary(
    tmp_path, monkeypatch, format_name, overwrite
):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-original")
    output = tmp_path / "output"
    output.mkdir()
    destination = output / f"Paper.{format_name}"
    destination.write_bytes(b"previous export")

    if format_name == "pdf":
        def fail_copy(source, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError("interrupted copy")

        monkeypatch.setattr(artifacts.shutil, "copy2", fail_copy)
    else:
        temporary_file = artifacts.tempfile.NamedTemporaryFile

        @contextmanager
        def fail_write(*args, **kwargs):
            with temporary_file(*args, **kwargs) as stream:
                class InterruptedWriter:
                    name = stream.name

                    def write(self, text):
                        stream.write(text[:5])
                        raise OSError("interrupted write")

                yield InterruptedWriter()

        monkeypatch.setattr(artifacts.tempfile, "NamedTemporaryFile", fail_write)

    with pytest.raises(OSError, match="interrupted"):
        export.export_outputs(
            Article("Paper"), source, [format_name], output, Config(),
            filename_template="{{title}}", overwrite=overwrite,
        )
    assert destination.read_bytes() == b"previous export"
    assert source.read_bytes() == b"%PDF-original"
    assert list(output.iterdir()) == [destination]


@pytest.mark.parametrize("format_name", ["pdf", "json"])
def test_competing_export_is_preserved_and_collision_name_is_retried(
    tmp_path, monkeypatch, format_name
):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-original")
    output = tmp_path / "output"
    destination = output / f"Paper.{format_name}"
    hardlink = Path.hardlink_to
    competed = False

    def competing_link(path, target):
        nonlocal competed
        if path == destination and not competed:
            competed = True
            path.write_bytes(b"competing export")
        return hardlink(path, target)

    monkeypatch.setattr(Path, "hardlink_to", competing_link)
    outputs, backend = export.export_outputs(
        Article("Paper"), source, [format_name], output, Config(),
        filename_template="{{title}}",
    )
    assert competed
    assert destination.read_bytes() == b"competing export"
    assert outputs == [output / f"Paper_2.{format_name}"]
    assert backend is None
    if format_name == "pdf":
        assert outputs[0].read_bytes() == source.read_bytes()
    else:
        assert json.loads(outputs[0].read_text())["title"] == "Paper"
    assert set(output.iterdir()) == {destination, outputs[0]}


@pytest.mark.parametrize("format_name", ["pdf", "json"])
def test_dangling_symlink_collisions_are_preserved(tmp_path, format_name):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-original")
    output = tmp_path / "output"
    output.mkdir()
    first = output / f"Paper.{format_name}"
    second = output / f"Paper_2.{format_name}"
    missing = tmp_path / "missing"
    first.symlink_to(missing)
    second.symlink_to(missing)

    outputs, _ = export.export_outputs(
        Article("Paper"), source, [format_name], output, Config(),
        filename_template="{{title}}",
    )
    assert outputs == [output / f"Paper_3.{format_name}"]
    assert first.is_symlink() and second.is_symlink()
    assert first.readlink() == second.readlink() == missing
    assert not missing.exists()


@pytest.mark.parametrize("failed_format", ["md", "txt"])
def test_conversion_failure_records_published_outputs_and_recoverable_manifest(
    tmp_path, monkeypatch, capsys, failed_format
):
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(source)
    article = Article("Paper", doi="10.1000/example")
    config = Config(download_dir=tmp_path / "output", pdf_compress=False)
    monkeypatch.setattr(cli, "_resolve_item", lambda *args, **kwargs: (article, source))

    def fail(*args, **kwargs):
        export.emit("process", "Converting PDF")
        raise RuntimeError("conversion failed")

    if failed_format == "md":
        monkeypatch.setattr(export, "markdown", fail)
        formats = "pdf,md"
        expected_names = ["Paper.pdf"]
        expected_backend = None
    else:
        monkeypatch.setattr(export, "markdown", lambda *args: ("Markdown text\n", "native"))
        monkeypatch.setattr(export, "extract_text", fail)
        formats = "pdf,md,txt"
        expected_names = ["Paper.pdf", "Paper.md"]
        expected_backend = "native"
    report_path = tmp_path / "report.json"
    manifest = tmp_path / "manifest.json"
    args = cli._get_parser().parse_args([
        "reference", "-f", formats, "--filename", "{{title}}", "--processor", "native",
        "--jobs", "1", "--quiet", "--report", str(report_path), "--manifest", str(manifest),
    ])
    assert cli._run_get(args, config) == 1
    row = json.loads(report_path.read_text())[0]
    assert row["status"] == "error"
    assert row["export_status"] == "partial"
    assert row["error"] == "conversion failed"
    assert row["failed_stage"] == "process"
    assert row["processor"] == expected_backend
    assert row["source"] == "local"
    assert [Path(path).name for path in row["outputs"]] == expected_names
    assert [artifact["name"] for artifact in row["artifacts"]] == expected_names
    assert load_manifest(manifest)["items"] == [row]
    assert delivered_identities([manifest]) == {"doi:10.1000/example"}
    remembered, pdf = history.load_latest(config)
    assert remembered == article
    assert pdf == (config.download_dir / "Paper.pdf").resolve()
    assert pdf.read_bytes() == source.read_bytes()
    stderr = capsys.readouterr().err
    assert "conversion failed" in stderr
    assert all(f"Saved: {path}" in stderr for path in row["outputs"])

    for path in row["outputs"]:
        Path(path).unlink()
    recovered = tmp_path / "recovered"
    replay = export_batch(manifest, recovered, config)
    assert replay[0]["status"] == "ok"
    assert {path.name for path in recovered.iterdir()} == set(expected_names)
    assert (recovered / "Paper.pdf").read_bytes() == source.read_bytes()
