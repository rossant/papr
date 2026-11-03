from papr.model import Article, article_from_dict


def test_merge_preserves_singular_pdf_urls_and_stable_alternatives():
    primary = Article(
        title="Paper",
        oa_pdf_url="https://example.test/first.pdf",
        oa_pdf_urls=["https://example.test/shared.pdf", "https://example.test/second.pdf"],
    )
    incoming = Article(
        title="Paper",
        oa_pdf_url="https://example.test/third.pdf",
        oa_pdf_urls=["https://example.test/shared.pdf", "https://example.test/fourth.pdf"],
    )
    incoming_urls = incoming.oa_pdf_urls

    merged = primary.merge(incoming)

    assert merged.oa_pdf_url == "https://example.test/first.pdf"
    assert merged.oa_pdf_urls == [
        "https://example.test/first.pdf",
        "https://example.test/shared.pdf",
        "https://example.test/second.pdf",
        "https://example.test/third.pdf",
        "https://example.test/fourth.pdf",
    ]
    assert merged.oa_pdf_urls is not incoming_urls
    assert incoming.oa_pdf_urls is incoming_urls
    assert incoming.oa_pdf_urls == [
        "https://example.test/shared.pdf",
        "https://example.test/fourth.pdf",
    ]


def test_merge_singular_only_urls_round_trip_without_aliasing():
    primary = Article(title="Paper", oa_pdf_url="https://example.test/first.pdf")
    incoming = Article(title="Paper", oa_pdf_url="https://example.test/second.pdf")

    primary.merge(incoming)
    restored = article_from_dict(primary.to_dict())

    assert restored.oa_pdf_url == "https://example.test/first.pdf"
    assert restored.oa_pdf_urls == [
        "https://example.test/first.pdf",
        "https://example.test/second.pdf",
    ]
    assert restored.oa_pdf_urls is not primary.oa_pdf_urls
