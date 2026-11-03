"""Publisher link identity checks run without the optional browser dependency."""

import pytest

from papr.fetchers.candidates import article_identity, rank_candidate_urls


@pytest.mark.parametrize(("landing", "pdf", "identity"), [
    (
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
        "https://pmc.ncbi.nlm.nih.gov/articles/instance/123/bin/paper.pdf",
        ("pmc", "123"),
    ),
    (
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/pdf/",
        ("pmc", "123"),
    ),
    (
        "https://publisher.test/doi/abs/10.1234/paper",
        "https://publisher.test/doi/pdf/10.1234/paper",
        ("doi", "10.1234/paper"),
    ),
    (
        "https://publisher.test/doi/10.1234/paper/",
        "https://publisher.test/doi/10.1234/paper/pdf",
        ("doi", "10.1234/paper"),
    ),
])
def test_article_identity_survives_pdf_routes(landing, pdf, identity):
    assert article_identity(landing) == article_identity(pdf) == identity
    assert rank_candidate_urls(landing, [pdf]) == [pdf]


def test_related_article_is_rejected_even_when_declared_in_citation_metadata():
    page = "https://publisher.test/articles/requested"
    related = "https://publisher.test/articles/related.pdf"
    full = "https://publisher.test/articles/requested.pdf"
    assert rank_candidate_urls(page, [related, full], related, allow_generic=True) == [full]


@pytest.mark.parametrize("supplement", [
    "/articles/requested/supplementary.pdf",
    "/articles/requested/supporting_information.pdf",
    "/articles/requested/MediaObjects/paper_MOESM1_ESM.pdf",
    "/articles/requested/paper.s001.pdf",
])
def test_explicit_supplements_are_rejected(supplement):
    page = "https://publisher.test/articles/requested"
    full = page + "/pdf/"
    assert rank_candidate_urls(page, [supplement, full], supplement) == [full]


def test_metadata_cdn_precedes_unverified_generic_links():
    page = "https://publisher.test/landing"
    citation = "https://cdn.test/opaque-file.pdf?token=abc"
    assert rank_candidate_urls(page, ["/unidentified.pdf"], citation, allow_generic=True) == [
        citation, "https://publisher.test/unidentified.pdf",
    ]


def test_generic_oa_links_require_same_origin_and_no_conflicting_article_identity():
    page = "https://publisher.test/landing"
    links = [
        "https://other.test/file.pdf", "/articles/related.pdf", "/valid.pdf", "file:///tmp/a.pdf",
    ]
    assert rank_candidate_urls(page, links, allow_generic=True) == [
        "https://publisher.test/valid.pdf",
    ]
    assert rank_candidate_urls(page, links) == []


def test_opaque_cdn_citation_is_allowed_from_identified_article():
    page = "https://publisher.test/articles/requested"
    citation = "https://cdn.test/opaque-file.pdf"
    assert rank_candidate_urls(page, ["/articles/related.pdf"], citation) == [citation]
