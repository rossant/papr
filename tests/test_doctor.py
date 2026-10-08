import json
import subprocess

import pytest

from papr.config import Config
from papr.doctor_cli import run
from papr.sources import SourceStatus


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "config_dir", property(lambda self: tmp_path / "config"))
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config(download_dir=tmp_path / "downloads", pdf_compress=True)


def _source_statuses(*, ucl=False, mistral=False):
    return [
        SourceStatus("zotero", False, "not detected", "unavailable"),
        SourceStatus("zolit", False, "not detected", "unavailable"),
        SourceStatus("zolit-sbs", False, "not detected", "unavailable"),
        SourceStatus("crossref", True, "remote"),
        SourceStatus("openalex", True, "remote"),
        SourceStatus("ucl", ucl, "saved browser profile" if ucl else "not logged in"),
        SourceStatus("mistral", mistral, "key configured" if mistral else "key not configured"),
    ]


def test_doctor_json_and_human_output_do_not_write_or_reveal_secrets(
    config, tmp_path, monkeypatch, capsys
):
    from papr import doctor

    monkeypatch.setattr("papr.sources.statuses", lambda *a, **k: _source_statuses(mistral=True))
    monkeypatch.setenv("MISTRAL_API_KEY", "never-print-this-secret")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert run(["--json"], config) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["status"] == "warning"
    assert any(check["name"] == "PDF text extraction" for check in report["checks"])
    assert "never-print-this-secret" not in output
    assert run([], config) == 0
    assert "Overall: warning" in capsys.readouterr().out
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before


def test_optional_services_warn_and_only_strict_mode_fails(config, monkeypatch, capsys):
    from papr import doctor

    monkeypatch.setattr("papr.sources.statuses", lambda *a, **k: _source_statuses())
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert run([], config) == 0
    assert run(["--strict"], config) == 1
    assert "optional OCR service" in capsys.readouterr().out


def test_compression_tool_version_timeout_is_optional_warning(config, monkeypatch):
    from papr import doctor

    monkeypatch.setattr("papr.sources.statuses", lambda *a, **k: [])
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/usr/bin/gs" if name == "gs" else None
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(doctor.subprocess, "run", timeout)
    report = doctor.diagnose(config)
    ghostscript = next(check for check in report["checks"] if check["name"] == "Ghostscript")
    assert ghostscript["status"] == "warning"
    assert ghostscript["detail"] == "version check timed out"
    assert report["status"] == "warning"


def test_inaccessible_output_and_config_are_required_errors(config, monkeypatch):
    from papr import doctor

    monkeypatch.setattr("papr.sources.statuses", lambda *a, **k: [])
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.os, "access", lambda path, mode: False)
    report = doctor.diagnose(config)
    assert report["status"] == "error"
    required = {check["name"]: check for check in report["checks"]}
    assert required["output directory"]["status"] == "error"
    assert required["config directory"]["status"] == "error"
    assert not config.download_dir.exists()
