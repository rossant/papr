import json

import pytest

from papr import cli
from papr.config import Config
from papr.model import Article


def test_plain_progress_and_report_timings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article("A paper"), None))
    report = tmp_path / "report.json"
    args = cli._get_parser().parse_args(
        ["A paper", "--dry-run", "--report", str(report), "-o", str(tmp_path)]
    )
    assert cli._run_get(args, Config()) == 0
    output = capsys.readouterr()
    assert "✓" in output.out
    assert "Resolve:" in output.err
    assert "Export:" in output.err
    assert "Done:" in output.err
    row = json.loads(report.read_text())[0]
    assert row["duration"] >= 0
    assert set(row["timings"]) == {"resolve", "export"}


def test_quiet_keeps_results_and_errors(tmp_path, monkeypatch, capsys):
    def resolve(item, *args, **kwargs):
        if item == "bad":
            raise LookupError("No match")
        return Article("A paper"), None

    monkeypatch.setattr(cli, "_resolve_item", resolve)
    args = cli._get_parser().parse_args(
        ["good", "bad", "--quiet", "--dry-run", "-o", str(tmp_path)]
    )
    assert cli._run_get(args, Config()) == 1
    output = capsys.readouterr()
    assert "✓" in output.out
    assert "✗ bad: No match" in output.err
    assert "Done:" not in output.err
    assert "Resolve:" not in output.err


def test_fetch_failure_records_stage_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article("A paper"), None))
    calls = []

    def fetch(*args, **kwargs):
        calls.append(args[0])
        raise OSError("Download failed")

    monkeypatch.setattr(cli, "fetch_pdf", fetch)
    report = tmp_path / "report.json"
    args = cli._get_parser().parse_args(["one", "two", "--report", str(report), "--quiet"])
    assert cli._run_get(args, Config()) == 1
    rows = json.loads(report.read_text())
    assert len(calls) == len(rows) == 2
    assert all(row["failed_stage"] == "fetch" for row in rows)
    assert all(set(row["timings"]) == {"resolve", "fetch"} for row in rows)


@pytest.mark.parametrize("rename", [False, True])
def test_dry_run_finishes_once(tmp_path, monkeypatch, rename):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"source")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article("A paper"), pdf))
    finished = []
    original = cli.Reporter.finish_item

    def finish(self, status):
        finished.append(status)
        return original(self, status)

    monkeypatch.setattr(cli.Reporter, "finish_item", finish)
    args = cli._get_parser().parse_args(
        [str(pdf), "--dry-run", "--quiet", *(["--rename"] if rename else [])]
    )
    assert cli._run_get(args, Config()) == 0
    assert finished == ["dry-run"]


@pytest.mark.parametrize("quiet", [False, True])
def test_resolve_json_stdout_is_clean(monkeypatch, capsys, quiet):
    monkeypatch.setattr(cli, "resolve", lambda *a, **k: [Article("A paper")])
    assert cli.main(["resolve", "paper", "--json", *(["--quiet"] if quiet else [])]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)[0]["title"] == "A paper"
    assert ("Resolve:" in output.err) is not quiet
