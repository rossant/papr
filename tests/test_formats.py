import json

import pytest

from papr.formats import bib, csl
from papr.model import Article, Person


def sample():
    return Article(
        title="Example paper",
        authors=[Person(family="Smith", given="Jane"), Person(family="Doe", given="John")],
        year=2024,
        journal="Journal of Examples",
        volume="12",
        issue="3",
        pages="10-20",
        doi="10.1234/example",
        pmid="123456",
        url="https://doi.org/10.1234/example",
    )


def test_csl_roundtrip_core_fields():
    encoded = csl.encode(sample())
    decoded = csl.decode(encoded)
    assert decoded.title == "Example paper"
    assert decoded.year == 2024
    assert decoded.doi == "10.1234/example"
    assert decoded.authors[0].family == "Smith"
    json.dumps(encoded)


def test_biblatex_export_and_parse():
    text = bib.encode(sample(), biblatex=True)
    assert "journaltitle" in text
    assert "doi = {10.1234/example}" in text
    parsed = bib.decode_many(text)
    assert len(parsed) == 1
    assert parsed[0].doi == "10.1234/example"
    assert parsed[0].year == 2024


def test_bibtex_uses_legacy_fields():
    text = bib.encode(sample(), biblatex=False)
    assert "journal = {Journal of Examples}" in text
    assert "year = {2024}" in text


@pytest.mark.parametrize(
    "item_type", ["book", "chapter", "paper-conference", "article", "document"]
)
def test_csl_preserves_nonjournal_types(item_type):
    article = sample()
    article.item_type = item_type
    assert csl.decode(csl.encode(article)).item_type == item_type


@pytest.mark.parametrize(
    "item_type,entry_type",
    [
        ("book", "book"),
        ("chapter", "incollection"),
        ("paper-conference", "inproceedings"),
        ("document", "misc"),
    ],
)
def test_bib_preserves_supported_types(item_type, entry_type):
    article = sample()
    article.item_type = item_type
    encoded = bib.encode(article)
    assert encoded.startswith(f"@{entry_type}{{")
    assert bib.decode_many(encoded)[0].item_type == item_type


def test_merge_fills_unknown_type_without_overriding_known_type():
    article = Article(title="Unknown", item_type="document")
    article.merge(Article(title="Book", item_type="book"))
    assert article.item_type == "book"
    article.merge(Article(title="Conflicting remote", item_type="article-journal"))
    assert article.item_type == "book"
