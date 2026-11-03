
import json
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from papr import pdf_source
from papr.config import Config
from papr.fetchers import _cache_path
from papr.model import Article


@pytest.fixture
def archive(tmp_path, monkeypatch):
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in [
        "Unrelated cover", "Paper title 10.1234/original", "Second journal page", "Next paper",
    ]:
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        })
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    path = tmp_path / "thesis.pdf"
    writer.write(path)
    article = Article("Paper title", doi="10.1234/original", pages="53-54")
    monkeypatch.setattr(pdf_source, "resolve", lambda *args: [article])
    return path, article, Config()


def test_archive_import_verifies_original_pages_and_keeps_provenance(archive):
    path, article, config = archive
    original = path.read_bytes()
    result = pdf_source.import_pdf(
        article.doi, path, config, pages="2-3",
        source_url="https://archive.test/thesis?temporary_token=secret",
    )
    assert path.read_bytes() == original
    reader = PdfReader(result["cache_path"])
    assert len(reader.pages) == 2
    assert "Paper title" in reader.pages[0].extract_text()
    assert "Second journal page" in reader.pages[1].extract_text()
    assert result["source_url"] == "https://archive.test/thesis"
    assert result["method"] == "original-pages"
    assert len(list((config.data_dir / "pdf-sources").glob("*.json"))) == 1


@pytest.mark.parametrize(("pages", "error"), [
    ("1-2", "first selected page"), ("2-2", "page count"),
    ("2-9", "outside"), ("0-2", "inclusive"), ("3-2", "inclusive"),
])
def test_invalid_archive_selection_preserves_existing_cache(archive, pages, error):
    path, article, config = archive
    cache = _cache_path(article, config)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"%PDF-existing")
    with pytest.raises(ValueError, match=error):
        pdf_source.import_pdf(article.doi, path, config, pages=pages)
    assert cache.read_bytes() == b"%PDF-existing"
    assert not (config.data_dir / "pdf-sources").exists()


def test_wrong_doi_in_selected_page_is_rejected(archive, monkeypatch):
    path, _, config = archive
    monkeypatch.setattr(pdf_source, "resolve", lambda *args: [
        Article("Paper title", doi="10.1234/different", pages="53-54")
    ])
    with pytest.raises(ValueError, match="first selected page"):
        pdf_source.import_pdf("10.1234/different", path, config, pages="2-3")


def test_cache_import_command_uses_verified_page_selection(archive, capsys):
    from papr.cache_cli import run

    path, article, config = archive
    assert run(["import", article.doi, str(path), "--pages", "2-3"], config) == 0
    assert "Verified original PDF" in capsys.readouterr().out
    assert len(PdfReader(_cache_path(article, config)).pages) == 2


@pytest.mark.parametrize(("doi_on_page", "accepted"), [
    ("10.1234/original-wrong", False),
    ("10.1234/original/extra", False),
    ("10.1234/original+wrong", False),
    ("10.1234/original?wrong", False),
    ("10.1234/original#wrong", False),
    ("10.1234/original@wrong", False),
    ("10.1234/original~wrong", False),
    ("10.1234/original<wrong>", False),
    ('10.1234/original"wrong', False),
    ("10.1234/original'wrong", False),
    ("https://doi.org/10.1234/ORIGINAL.", True),
    ("(doi:10.1234/original).", True),
    ('"https://doi.org/10.1234/original"', True),
    ("<https://doi.org/10.1234/original>", True),
])
def test_archive_import_compares_complete_normalized_dois(
    archive, monkeypatch, doi_on_page, accepted,
):
    path, article, config = archive
    real_reader = pdf_source.PdfReader

    class Page:
        def extract_text(self):
            return f"Paper title {doi_on_page}"

    reader = real_reader(path)
    monkeypatch.setattr(reader.pages[1], "extract_text", Page().extract_text)
    monkeypatch.setattr(pdf_source, "PdfReader", lambda *args: reader)
    if accepted:
        assert pdf_source.import_pdf(article.doi, path, config, pages="2-3")
    else:
        with pytest.raises(ValueError, match="first selected page"):
            pdf_source.import_pdf(article.doi, path, config, pages="2-3")
        assert not _cache_path(article, config).exists()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["serialization", "cache", "provenance"])
def test_import_publication_failures_preserve_cache_and_provenance(
    archive, monkeypatch, existing, failure,
):
    path, article, config = archive
    cache = _cache_path(article, config)
    provenance = config.data_dir / "pdf-sources" / f"{cache.stem}.json"
    previous_cache = b"%PDF-prior-verified-copy"
    previous_record = {"sha256": "previous", "title": "Previous source"}
    if existing:
        cache.parent.mkdir(parents=True)
        cache.write_bytes(previous_cache)
        provenance.parent.mkdir(parents=True)
        provenance.write_text(json.dumps(previous_record))
    replace = Path.replace
    atomic_json = pdf_source.atomic_json

    def failing_replace(source, destination):
        if (failure == "cache" and source.name == "verified.pdf") or (
            failure == "provenance" and Path(destination) == provenance
        ):
            raise OSError("publication failed")
        return replace(source, destination)

    def failing_json(target, record):
        if failure == "serialization":
            raise OSError("publication failed")
        return atomic_json(target, record)

    monkeypatch.setattr(Path, "replace", failing_replace)
    monkeypatch.setattr(pdf_source, "atomic_json", failing_json)
    with pytest.raises(OSError, match="publication failed"):
        pdf_source.import_pdf(article.doi, path, config, pages="2-3")
    if existing:
        assert cache.read_bytes() == previous_cache
        assert json.loads(provenance.read_text()) == previous_record
    else:
        assert not cache.exists()
        assert not provenance.exists()
    assert list(cache.parent.glob("import-*")) == []
    assert list(provenance.parent.glob("*.tmp")) == []
