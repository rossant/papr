from papr.cli import _formats, main
from papr.sources import SourceStatus


def test_format_parser():
    assert _formats("pdf,md,bib") == ["pdf", "md", "bib"]


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_sources_command(monkeypatch, capsys):
    monkeypatch.setattr(
        "papr.cli.source_statuses",
        lambda config: [SourceStatus("zotero", True, "local API, 12 top-level items")],
    )
    assert main(["sources"]) == 0
    output = capsys.readouterr().out
    assert "zotero" in output
    assert "12 top-level items" in output
