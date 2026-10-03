from papr.fetchers.oa import is_pdf


def test_pdf_validation():
    assert is_pdf(b"%PDF-1.7\n...")
    assert is_pdf(b"anything", "application/pdf")
    assert not is_pdf(b"<html>no</html>", "text/html")
