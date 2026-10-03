from papr.cli import _formats, main


def test_format_parser():
    assert _formats("pdf,md,bib") == ["pdf", "md", "bib"]


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
