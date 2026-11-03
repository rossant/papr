from papr import pdf
from papr.fetchers import _is_local_pdf, oa
from papr.resolvers import common


def test_pdf_signature_is_shared_and_ignores_content_type():
    assert pdf.is_pdf(b"%PDF-1.7\n", "text/html")
    assert not pdf.is_pdf(b"<html>", "application/pdf")
    assert oa.is_pdf is pdf.is_pdf


def test_local_pdf_check_is_shared_across_import_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    valid = home / "paper.pdf"
    valid.write_bytes(b"%PDF-1.7\ncontents")
    expanduser = type(valid).expanduser
    monkeypatch.setattr(
        type(valid), "expanduser",
        lambda path: valid if str(path) == "~/paper.pdf" else expanduser(path),
    )
    invalid = tmp_path / "paper.html"
    invalid.write_bytes(b"<html>")

    assert pdf.is_local_pdf("~/paper.pdf")
    assert pdf.is_local_pdf(valid)
    assert not pdf.is_local_pdf(None)
    assert not pdf.is_local_pdf("")
    assert not pdf.is_local_pdf(invalid)
    assert not pdf.is_local_pdf(tmp_path)
    assert _is_local_pdf is pdf.is_local_pdf
    assert common.is_local_pdf is pdf.is_local_pdf


def test_local_pdf_check_returns_false_for_missing_and_broken_paths(tmp_path):
    missing = tmp_path / "missing.pdf"
    broken = tmp_path / "broken.pdf"
    broken.symlink_to(tmp_path / "target.pdf")

    assert not pdf.is_local_pdf(missing)
    assert not pdf.is_local_pdf(broken)


def test_full_validation_accepts_scans_and_text_without_identifiers(tmp_path):
    from pdf_helpers import pdf_bytes

    from papr.model import Article

    for content in (pdf_bytes(), pdf_bytes(text="Some readable article text")):
        path = tmp_path / "paper.pdf"
        path.write_bytes(content)
        pdf.validate_pdf(path, Article("Requested title", doi="10.1234/requested"))


def test_full_validation_rejects_header_only_and_truncated_pdf(tmp_path):
    import pytest
    from pdf_helpers import pdf_bytes

    path = tmp_path / "paper.pdf"
    for content in (b"%PDF-1.7\ncontents", pdf_bytes()[:-6], b"%PDF-1.7\nbad\n%%EOF"):
        path.write_bytes(content)
        with pytest.raises(pdf.PdfValidationError):
            pdf.validate_pdf(path)


def test_full_validation_rejects_empty_and_encrypted_pdf(tmp_path):
    import pytest
    from pypdf import PdfWriter

    path = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.write(path)
    with pytest.raises(pdf.PdfValidationError, match="no pages"):
        pdf.validate_pdf(path)
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("password")
    writer.write(path)
    with pytest.raises(pdf.PdfValidationError, match="encrypted"):
        pdf.validate_pdf(path)


def test_full_validation_reads_later_page_content_streams(tmp_path):
    import pytest
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, StreamObject

    path = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    later = writer.add_blank_page(width=200, height=200)
    broken = StreamObject()
    broken._data = b"not a compressed stream"
    broken.update(DictionaryObject({
        NameObject("/Filter"): NameObject("/LZWDecode"),
        NameObject("/Length"): NumberObject(len(broken._data)),
    }))
    later[NameObject("/Contents")] = writer._add_object(broken)
    writer.write(path)
    with pytest.raises(pdf.PdfValidationError, match="damaged"):
        pdf.validate_pdf(path)


def test_full_validation_rejects_conflicting_explicit_doi(tmp_path):
    import pytest
    from pdf_helpers import pdf_bytes

    from papr.model import Article

    path = tmp_path / "paper.pdf"
    article = Article("Requested title", doi="10.1234/requested")
    for text in ("doi: 10.1234/other", "https://doi.org/10.1234/requested+suffix"):
        path.write_bytes(pdf_bytes(text=text))
        with pytest.raises(pdf.PdfValidationError, match="different DOI"):
            pdf.validate_pdf(path, article)


def test_full_validation_ignores_reference_dois_and_prefers_matching_title(tmp_path):
    from pdf_helpers import pdf_bytes

    from papr.model import Article

    path = tmp_path / "paper.pdf"
    article = Article("Requested title", doi="10.1234/requested")
    for text in (
        "Some article text. References: Smith, 10.1234/other",
        "Requested title doi: 10.1234/other",
    ):
        path.write_bytes(pdf_bytes(text=text))
        pdf.validate_pdf(path, article)


def test_full_validation_checks_substantive_printed_metadata_title(tmp_path):
    import pytest
    from pdf_helpers import pdf_bytes

    from papr.model import Article

    path = tmp_path / "paper.pdf"
    other = "An unrelated experiment on agriculture"
    path.write_bytes(pdf_bytes(label=other, text=other))
    with pytest.raises(pdf.PdfValidationError, match="different paper"):
        pdf.validate_pdf(path, Article("Retinal hemorrhage after minor trauma"))
    # Publisher templates can carry an obsolete title that is not on the page.
    path.write_bytes(pdf_bytes(label=other, text="Some readable article text"))
    pdf.validate_pdf(path, Article("Retinal hemorrhage after minor trauma"))
