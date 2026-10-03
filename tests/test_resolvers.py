from papr.model import Article, Person
from papr.resolvers.common import deduplicate, extract_doi, extract_pmid, score_article
from papr.resolvers.crossref import from_item as crossref_item
from papr.resolvers.openalex import from_item as openalex_item


def test_extract_identifiers():
    assert extract_doi("doi:10.1000/XYZ.12") == "10.1000/xyz.12"
    assert extract_doi("https://doi.org/10.1000/xyz") == "10.1000/xyz"
    assert extract_pmid("PMID: 12345678") == "12345678"


def test_scoring_prefers_matching_author_year_title():
    a = Article("Retinal hemorrhages in infants", [Person(family="Jenny")], 2020)
    b = Article("Unrelated topic", [Person(family="Smith")], 1999)
    assert score_article("Jenny 2020 retinal hemorrhages", a) > score_article(
        "Jenny 2020 retinal hemorrhages", b
    )


def test_short_author_year_scores_as_strong_match():
    article = Article("Any title", [Person(family="Jenny")], 2006)
    assert score_article("Jenny 2006", article) > 0.9


def test_two_same_author_year_items_remain_equally_plausible():
    first = Article("Title one", [Person(family="Jenny")], 2006)
    second = Article("Title two", [Person(family="Jenny")], 2006)
    assert abs(score_article("Jenny 2006", first) - score_article("Jenny 2006", second)) < 0.02


def test_crossref_mapping():
    a = crossref_item(
        {
            "title": ["Example paper"],
            "author": [{"family": "Smith", "given": "Jane"}],
            "published": {"date-parts": [[2024, 1, 2]]},
            "container-title": ["Journal"],
            "DOI": "10.1234/ABC",
            "type": "journal-article",
        }
    )
    assert a.doi == "10.1234/abc"
    assert a.year == 2024
    assert a.first_creator == "Smith"


def test_openalex_mapping():
    a = openalex_item(
        {
            "title": "Example paper",
            "publication_year": 2025,
            "authorships": [{"author": {"display_name": "Jane Smith"}}],
            "ids": {"doi": "https://doi.org/10.1234/ABC"},
            "primary_location": {"source": {"display_name": "Journal"}},
            "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
            "biblio": {"volume": "2", "issue": "3", "first_page": "10", "last_page": "12"},
        }
    )
    assert a.doi == "10.1234/abc"
    assert a.oa_pdf_url.endswith("paper.pdf")
    assert a.pages == "10-12"


def test_deduplicate_merges_sources():
    a = Article("Title", doi="10.1/x", journal="J")
    b = Article("Title", doi="10.1/x", pmid="123")
    result = deduplicate([a, b])
    assert len(result) == 1
    assert result[0].pmid == "123"
