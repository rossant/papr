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
