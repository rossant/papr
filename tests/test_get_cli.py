"""Exercise dependencies across sequential and prefetched CLI pipelines."""

import json
from pathlib import Path

import pytest

from papr import cli, history
from papr.batches import load_manifest
from papr.config import Config
from papr.export import ExportError
from papr.model import Article


@pytest.mark.parametrize("jobs", [1, 2])
def test_injected_services_continue_after_partial_export(tmp_path, monkeypatch, jobs):
    config = Config(download_dir=tmp_path / "output", pdf_compress=False)
    resolved, fetched, exported = [], [], []

    def resolve(item, cfg, interactive, **options):
        assert cfg is config and not interactive
        assert options == {"use_local": False, "use_remote": True, "enrich": True}
        resolved.append(item)
        return Article(item, doi=f"10.1000/{item}"), None

    def fetch(article, destination, cfg, **options):
        assert cfg is config
        assert options["allow_ucl"] is False and options["refresh"] is True
        fetched.append(article.title)
        destination.write_bytes(b"%PDF-1.7\n" + article.title.encode())
        return "injected-source"

    def export(article, pdf, formats, output_dir, cfg, **options):
        assert cfg is config and formats == ["pdf", "md"]
        assert output_dir == config.download_dir
        assert options == {
            "filename_template": "{{title}}",
            "processor": "native",
            "overwrite": True,
            "pdf_output": pdf,
        }
        exported.append(article.title)
        output_dir.mkdir(parents=True, exist_ok=True)
        saved = output_dir / f"{article.title}.pdf"
        saved.write_bytes(pdf.read_bytes())
        if article.title == "first":
            cli.emit("process", "Injected converter")
            raise ExportError("conversion failed", [saved], None)
        markdown = saved.with_suffix(".md")
        markdown.write_text("Converted text\n")
        return [saved, markdown], "native"

    monkeypatch.setattr(cli, "_resolve_item", resolve)
    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    monkeypatch.setattr(cli, "export_outputs", export)
    report, manifest = tmp_path / "report.json", tmp_path / "manifest.json"
    args = cli._get_parser().parse_args(
        [
            "first",
            "second",
            "-f",
            "pdf,md",
            "--jobs",
            str(jobs),
            "--no-local",
            "--enrich",
            "--non-interactive",
            "--oa-only",
            "--refresh",
            "--overwrite",
            "--quiet",
            "--filename",
            "{{title}}",
            "--processor",
            "native",
            "--report",
            str(report),
            "--manifest",
            str(manifest),
        ]
    )
    assert cli._run_get(args, config) == 1
    assert resolved == exported == ["first", "second"]
    assert sorted(fetched) == ["first", "second"]
    rows = json.loads(report.read_text())
    assert [row["status"] for row in rows] == ["error", "ok"]
    assert [row["export_status"] for row in rows] == ["partial", "ok"]
    assert rows[0]["failed_stage"] == "process"
    assert all(row["source"] == "injected-source" for row in rows)
    assert [len(row["artifacts"]) for row in rows] == [1, 2]
    assert all(Path(path).is_file() for row in rows for path in row["outputs"])
    assert load_manifest(manifest)["items"] == rows
    article, pdf = history.load_latest(config)
    assert article.title == "second"
    assert pdf == (config.download_dir / "second.pdf").resolve()
