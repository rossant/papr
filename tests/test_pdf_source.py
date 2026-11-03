
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
