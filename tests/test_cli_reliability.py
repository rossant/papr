import json

import pytest
from pypdf import PdfWriter

from papr import cli, input, local
from papr.config import Config
from papr.formats import csl
from papr.model import Article, Person
from papr.selection import select_candidate


def test_native_json_round_trip(tmp_path):
    article = Article(
        title="A paper",
        authors=[Person("Smith", "Jane", "orcid")],
        year=2020,
        doi="10.1000/test",
        journal="Journal",
        source="zotero",
        local_pdf="/paper.pdf",
        score=0.9,
        item_type="book",
    )
    path = tmp_path / "article.json"
    path.write_text(json.dumps(article.to_dict()))
    assert input.load_input(str(path), Config()) == [article]
    path.write_text(json.dumps(csl.encode(article)))
    decoded = input.load_input(str(path), Config())[0]
    assert decoded.authors == article.authors
    assert decoded.doi == article.doi
    assert decoded.year == 2020


@pytest.mark.parametrize("value", ["42", "[42]", "null"])
def test_invalid_json_shape(tmp_path, value):
    path = tmp_path / "bad.json"
    path.write_text(value)
    with pytest.raises(ValueError, match="article object"):
        input.load_input(str(path), Config())


def test_low_confidence_single_candidate_needs_confirmation():
    candidate = Article(title="Unrelated", score=0.01)
    with pytest.raises(LookupError, match="low-confidence"):
        cli._choose("Requested paper", [candidate], Config(), False)


def test_verified_identifier_does_not_require_search_confidence():
    candidate = Article(title="Paper", doi="10.1000/example", score=0.01)
    assert select_candidate("https://doi.org/10.1000/example", [candidate], Config()) is candidate
    candidate.doi = "10.1000/wrong"
    with pytest.raises(LookupError, match="verified"):
        select_candidate("10.1000/example", [candidate], Config())


@pytest.mark.parametrize("use_local,use_remote", [(True, False), (False, True)])
def test_pdf_enrichment_respects_source_policy(tmp_path, monkeypatch, use_local, use_remote):
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "A paper"})
    writer.write(pdf)
    seen = []

    def resolve(query, config, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(local, "resolve", resolve)
    article, source = input.materialize_article(
        pdf,
        Config(),
        use_local=use_local,
        use_remote=use_remote,
    )
    assert source == pdf
    assert article.title == "A paper"
    assert seen
    assert all(options == {"use_local": use_local, "use_remote": use_remote} for options in seen)


@pytest.mark.parametrize("overwrite", [True, False])
def test_dry_run_rename_preserves_source_and_destination(tmp_path, monkeypatch, overwrite):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "canonical.pdf"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article(title="Paper"), source))
    monkeypatch.setattr(cli, "basename", lambda *a, **k: "canonical")
    args = cli._get_parser().parse_args(
        [
            str(source),
            "--dry-run",
            "--rename",
            "--report",
            str(tmp_path / "report.json"),
            *(["--overwrite"] if overwrite else []),
        ]
    )
    assert cli._run_get(args, Config()) == 0
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"
    report = json.loads((tmp_path / "report.json").read_text())
    assert report[0]["status"] == "dry-run"


def test_bad_inputs_continue_batch_and_write_report(tmp_path, monkeypatch):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{")
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"corrupt")
    good = tmp_path / "good.json"
    good.write_text(json.dumps(Article(title="Good", year=2020).to_dict()))
    report_path = tmp_path / "report.json"
    args = cli._get_parser().parse_args(
        [
            str(bad_json),
            str(bad_pdf),
            str(good),
            "--dry-run",
            "--no-local",
            "--report",
            str(report_path),
        ]
    )
    assert cli._run_get(args, Config()) == 1
    report = json.loads(report_path.read_text())
    assert [row["status"] for row in report] == ["error", "error", "dry-run"]
    assert report[-1]["title"] == "Good"


def test_materialize_search_uses_same_confidence_policy(monkeypatch):
    monkeypatch.setattr(
        "papr.resolvers.resolve", lambda *a, **k: [Article(title="Wrong", score=0.1)]
    )
    with pytest.raises(LookupError, match="low-confidence"):
        input.materialize_article("Requested paper", Config())


def test_mismatched_identifier_cannot_be_interactively_selected(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    with pytest.raises(LookupError, match="verified"):
        cli._choose(
            "10.1000/requested", [Article(title="Other", doi="10.1000/other")], Config(), True
        )


def test_empty_formats_are_rejected():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError, match="at least one"):
        cli._formats(" , ")


def test_verbose_logging_is_scoped_and_restored(capsys):
    import logging

    logger = logging.getLogger("papr")
    previous = (logger.level, logger.propagate, list(logger.handlers))
    for _ in range(2):
        with cli._diagnostics(True):
            logging.getLogger("papr.resolvers").debug("provider failure")
            logging.getLogger("httpx").debug("private transport details")
        assert (logger.level, logger.propagate, logger.handlers) == previous
    output = capsys.readouterr().err
    assert output.count("provider failure") == 2
    assert "private transport details" not in output


def test_rename_failure_preserves_existing_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "canonical.pdf"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (Article(title="Paper"), source))
    monkeypatch.setattr(cli, "basename", lambda *a, **k: "canonical")

    def fail_replace(*args):
        raise OSError("rename failed")

    monkeypatch.setattr(type(source), "replace", fail_replace)
    args = cli._get_parser().parse_args([str(source), "--rename", "--overwrite"])
    assert cli._run_get(args, Config()) == 1
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


def test_dry_run_distinguishes_biblatex_and_bibtex_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_item", lambda *args, **kwargs: (Article("Paper"), None))
    report = tmp_path / "report.json"
    args = cli._get_parser().parse_args(
        ["Paper", "--dry-run", "-f", "bib,bibtex", "-o", str(tmp_path), "--report", str(report)]
    )
    assert cli._run_get(args, Config()) == 0
    paths = json.loads(report.read_text())[0]["outputs"]
    assert len(paths) == len(set(paths)) == 2
    assert paths[0].endswith(".bib")
    assert paths[1].endswith(".bibtex.bib")


def test_pdf_uses_canonical_filename_for_enrichment(tmp_path, monkeypatch):
    pdf = tmp_path / "Duhaime_1987_The_shaken_baby_syndrome.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/CreationDate": "D:20200101000000"})
    writer.write(pdf)
    found = Article("The shaken baby syndrome", year=1987, doi="10.1000/test", score=0.95)
    seen = []

    def resolve(query, config, **options):
        seen.append((query, options))
        return [found]

    monkeypatch.setattr(local, "resolve", resolve)
    assert local.article_from_pdf(pdf, Config(), use_remote=False) is found
    assert seen == [
        ("Duhaime 1987 The shaken baby syndrome", {"use_local": True, "use_remote": False})
    ]


def test_pdf_creation_date_is_not_publication_year(tmp_path, monkeypatch):
    pdf = tmp_path / "unknown.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/CreationDate": "D:20200101000000"})
    writer.write(pdf)
    monkeypatch.setattr(local, "resolve", lambda *args, **kwargs: [])
    assert local.article_from_pdf(pdf, Config()).year is None


def test_pdf_reads_doi_from_metadata(tmp_path, monkeypatch):
    pdf = tmp_path / "unknown.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Subject": "doi:10.1000/example"})
    writer.write(pdf)
    found = Article("Paper", doi="10.1000/example")
    monkeypatch.setattr(local, "resolve", lambda *args, **kwargs: [found])
    assert local.article_from_pdf(pdf, Config()) is found


@pytest.mark.parametrize(
    "field,value",
    [
        ("year", True),
        ("year", "2024"),
        ("score", float("nan")),
        ("score", float("inf")),
        ("authors", [42]),
        ("authors", [{"family": 42}]),
        ("source_key", 42),
        ("source_library", []),
    ],
)
def test_native_input_rejects_invalid_metadata_like_history(tmp_path, field, value):
    from papr.history import _article

    data = Article("Paper", year=2024).to_dict()
    data[field] = value
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        input.load_input(str(path), Config())
    with pytest.raises(ValueError):
        _article(data)


def test_native_input_allows_extension_fields_but_private_history_is_strict(tmp_path):
    from papr.history import _article

    data = {"title": "Paper", "year": 2024, "custom": "external annotation"}
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(data))
    assert input.load_input(str(path), Config()) == [Article("Paper", year=2024)]
    with pytest.raises(ValueError, match="unknown article fields"):
        _article(data)


def test_doctor_invalid_config_still_produces_json(monkeypatch, capsys):
    def invalid():
        raise ValueError("formats contains unsupported format")

    monkeypatch.setattr(Config, "load", invalid)
    assert cli.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "error"
    assert report["checks"][0]["name"] == "configuration"
