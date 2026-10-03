from papr.config import Config
from papr.model import Article, Person
from papr import resolvers


def _jenny(title: str, doi: str | None = "10.1000/jenny") -> Article:
    return Article(
        title=title,
        authors=[Person(family="Jenny", given="Carole")],
        year=2006,
        doi=doi,
        source="zotero",
        score=0.95,
    )


def test_strong_local_match_skips_remote(monkeypatch):
    local = _jenny("Known locally")
    monkeypatch.setattr(resolvers, "_local_search", lambda query, config: [local])

    def fail_remote(query, config):
        raise AssertionError("remote search should not run")

    monkeypatch.setattr(resolvers, "_remote_search", fail_remote)
    result = resolvers.resolve("Jenny 2006", Config())

    assert result == [local]


def test_local_only_never_calls_remote(monkeypatch):
    local = _jenny("Known locally")
    monkeypatch.setattr(resolvers, "_local_search", lambda query, config: [local])

    def fail_remote(query, config):
        raise AssertionError("remote search should not run")

    monkeypatch.setattr(resolvers, "_remote_search", fail_remote)
    result = resolvers.resolve("Jenny 2006", Config(), use_remote=False)

    assert result == [local]


def test_sbs_seed_without_identifier_expands_remote_query(monkeypatch):
    seed = _jenny("Specific known title", doi=None)
    seed.source = "zolit-sbs"
    seen = {}

    monkeypatch.setattr(resolvers, "_local_search", lambda query, config: [seed])

    def remote(query, config):
        seen["query"] = query
        return [
            Article(
                title="Specific known title",
                authors=[Person(family="Jenny")],
                year=2006,
                doi="10.1000/found",
                score=0.98,
            )
        ]

    monkeypatch.setattr(resolvers, "_remote_search", remote)
    result = resolvers.resolve("Jenny 2006", Config())

    assert "Specific known title" in seen["query"]
    assert result[0].doi == "10.1000/found"
