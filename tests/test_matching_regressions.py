import pytest

from papr.config import Config
from papr.model import Article, Person
from papr.resolvers import resolve
from papr.resolvers.common import deduplicate, normalize_doi, score_article
from papr.resolvers.session import cached_library, resolution_session


@pytest.mark.parametrize(
    "value",
    [
        "10.1000/example(abc)",
        "https://doi.org/10.1000/example(abc)",
        "(doi:10.1000/example(abc)).",
    ],
)
def test_balanced_doi_parentheses_are_preserved(value):
    assert normalize_doi(value) == "10.1000/example(abc)"


def test_scoring_missing_author_family():
    article = Article("Target", authors=[Person(given="Jane")])
    assert 0 <= score_article("Target", article) <= 1


def test_exact_title_can_pass_confidence_threshold():
    article = Article("A specific paper title", year=2020)
    config = Config()
    assert score_article("A specific paper title", article) >= config.auto_accept_score
    assert score_article("A specific paper title 2020", article) >= config.auto_accept_score
    assert score_article("A specific paper title 1999", article) < config.auto_accept_score


@pytest.mark.parametrize("reverse", [False, True])
def test_seed_merges_with_identified_work(reverse):
    seed = Article("Same: title", year=2020, local_pdf="/paper.pdf", score=0.95)
    remote = Article("Same title", year=2020, doi="10.1000/x", score=0.98)
    result = deduplicate([remote, seed] if reverse else [seed, remote])
    assert len(result) == 1
    assert result[0].doi == "10.1000/x"
    assert result[0].local_pdf == "/paper.pdf"
    assert result[0].score == 0.98


def test_different_dois_not_merged_by_title():
    articles = [Article("Same", year=2020, doi=doi) for doi in ["10.1000/a", "10.1000/b"]]
    assert len(deduplicate(articles)) == 2


def test_expanded_seed_is_one_enriched_candidate(monkeypatch):
    seed = Article("Same title", authors=[Person(family="Jenny")], year=2006, score=0.95)
    monkeypatch.setattr("papr.resolvers._local_search", lambda *args: [seed])
    monkeypatch.setattr(
        "papr.resolvers._remote_search",
        lambda *args: [Article("Same title", year=2006, doi="10.1000/found", score=0.98)],
    )
    result = resolve("Jenny 2006", Config())
    assert len(result) == 1
    assert result[0].doi == "10.1000/found"


def test_library_reused_without_query_mutations_and_reset_between_batches():
    calls = []

    def load():
        calls.append(1)
        return [Article("Title", doi="10.1000/a")]

    with resolution_session():
        first = cached_library(("library",), load)
        first[0].score = 0.99
        first[0].pmid = "123456"
        second = cached_library(("library",), load)
        assert second[0].score is None
        assert second[0].pmid is None
        assert len(calls) == 1
    with resolution_session():
        cached_library(("library",), load)
    assert len(calls) == 2


def test_resolver_failure_visible_in_debug_log(monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("papr.resolvers.LOCAL_SEARCHERS", (broken,))
    with caplog.at_level("DEBUG", logger="papr"):
        assert resolve("Title", Config(), use_remote=False) == []
    assert "provider unavailable" in caplog.text
