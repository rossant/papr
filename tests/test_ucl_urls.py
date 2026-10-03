from papr.fetchers.ucl import _proxy_url, _rank_candidate_urls
from papr.model import Article


def test_publisher_landing_precedes_doi():
    article = Article(title="Paper", doi="10.1234/paper", url="https://publisher.test/paper")
    assert _proxy_url(article).endswith("url=https://publisher.test/paper")
    article.url = "https://doi.org/10.1234/paper"
    assert _proxy_url(article).endswith("url=https://doi.org/10.1234/paper")


def test_sciencedirect_filters_support_and_cited_article_pdfs():
    page = "https://www-sciencedirect-com.libproxy.ucl.ac.uk/science/article/pii/S2950193826000203"
    full = page + "/pdfft?isDTMRedir=true&download=true"
    unrelated = "https://www-sciencedirect-com.libproxy.ucl.ac.uk/science/article/pii/S0000000000000000/pdfft"
    assert _rank_candidate_urls(page, ["https://publisher.test/faq.pdf", unrelated, full]) == [full]
    assert _rank_candidate_urls(page, [full], unrelated) == [full]


def test_journal_full_pdf_precedes_preview_and_rejects_other_articles():
    page = "https://publisher.test/view/journals/abc/1/2/article-p409.xml"
    full = "https://publisher.test/downloadpdf/journals/abc/1/2/article-p409.xml"
    preview = "https://publisher.test/previewpdf/journals/abc/1/2/article-p409.xml"
    other = "https://publisher.test/downloadpdf/journals/abc/1/2/article-p400.xml"
    assert _rank_candidate_urls(page, [preview, other, "/help/faq.pdf", full], preview) == [
        full,
        preview,
    ]


def test_trusted_citation_metadata_allows_unidentified_cdn_url():
    page = "https://publisher.test/science/article/pii/S123456789"
    citation = "https://cdn.test/opaque-file.pdf?token=abc"
    assert _rank_candidate_urls(page, ["https://publisher.test/faq.pdf"], citation) == [citation]


def test_doi_path_and_generic_article_identity():
    page = "https://publisher.test/doi/abs/10.1234/paper"
    full = "https://publisher.test/doi/pdf/10.1234/paper"
    assert _rank_candidate_urls(page, [full, "https://publisher.test/doi/pdf/10.1234/cited"]) == [
        full
    ]
    page = "https://publisher.test/articles/paper.html"
    assert _rank_candidate_urls(page, ["/articles/paper.pdf", "/articles/other.pdf"]) == [
        "https://publisher.test/articles/paper.pdf",
    ]


def test_unknown_article_path_requires_matching_path_or_metadata():
    page = "https://publisher.test/content/paper.html"
    assert _rank_candidate_urls(page, ["/help/faq.pdf", "/content/paper.pdf"]) == [
        "https://publisher.test/content/paper.pdf",
    ]
