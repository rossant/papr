import pytest

from papr import resolvers
from papr.config import Config
from papr.model import Article, Person


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


def test_doi_uses_local_metadata_and_pdf(monkeypatch):
    local = _jenny("Known locally")
    local.local_pdf = "/tmp/paper.pdf"
    monkeypatch.setattr(resolvers, "_local_by_doi", lambda doi, config: [local])
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda doi, config: None)
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda doi, config: None)

    result = resolvers.resolve("10.1000/jenny", Config())

    assert result[0].local_pdf == "/tmp/paper.pdf"
    assert result[0].source == "zotero"


@pytest.mark.parametrize(
    "enrich,pdf_exists,expected_calls",
    [
        (False, True, 0),
        (True, True, 2),
        (False, False, 2),
    ],
)
def test_doi_remote_enrichment_policy(tmp_path, monkeypatch, enrich, pdf_exists, expected_calls):
    local = _jenny("Known locally")
    pdf = tmp_path / "paper.pdf"
    if pdf_exists:
        pdf.write_bytes(b"%PDF-1.7\n")
    local.local_pdf = str(pdf)
    monkeypatch.setattr(resolvers, "_local_by_doi", lambda doi, config: [local])
    calls = []

    def remote(doi, config):
        calls.append(doi)
        return None

    monkeypatch.setattr(resolvers.crossref, "by_doi", remote)
    monkeypatch.setattr(resolvers.openalex, "by_doi", remote)
    assert resolvers.resolve("10.1000/jenny", Config(), enrich=enrich) == [local]
    assert len(calls) == expected_calls


def test_doi_enrichment_preserves_local_conference_type(monkeypatch):
    local = _jenny("Known locally")
    local.item_type = "paper-conference"
    monkeypatch.setattr(resolvers, "_local_by_doi", lambda doi, config: [local])
    monkeypatch.setattr(
        resolvers.crossref,
        "by_doi",
        lambda doi, config: Article(
            "Known locally",
            doi=doi,
            item_type="article-journal",
            abstract="Enrichment",
        ),
    )
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda doi, config: None)
    result = resolvers.resolve("10.1000/jenny", Config(), enrich=True)
    assert result[0].item_type == "paper-conference"
    assert result[0].abstract == "Enrichment"


@pytest.mark.parametrize("enrich,use_local,use_remote,expected_calls", [
    (False, True, True, 0),
    (True, True, True, 2),
    (False, False, True, 2),
    (True, True, False, 0),
])
def test_pmid_local_pdf_policy(
    tmp_path, monkeypatch, enrich, use_local, use_remote, expected_calls
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    local = Article("Local title", pmid="123456", local_pdf=str(pdf), score=1.0)
    calls = []
    monkeypatch.setattr(resolvers, "_local_by_pmid", lambda *args: [local])

    def pubmed(*args):
        calls.append("pubmed")
        return Article("Remote title", pmid="123456", doi="10.1000/jenny")

    def openalex(*args):
        calls.append("openalex")
        return None

    monkeypatch.setattr(resolvers.pubmed, "by_pmid", pubmed)
    monkeypatch.setattr(resolvers.openalex, "by_doi", openalex)
    result = resolvers.resolve(
        "pmid:123456", Config(), enrich=enrich, use_local=use_local, use_remote=use_remote
    )
    assert len(calls) == expected_calls
    assert len(result) == 1
    if use_local:
        assert result[0].local_pdf == str(pdf)


def test_invalid_local_pdf_does_not_skip_doi_metadata(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_text("not a PDF")
    local = _jenny("Local")
    local.local_pdf = str(pdf)
    monkeypatch.setattr(resolvers, "_local_by_doi", lambda *args: [local])
    calls = []
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: calls.append("crossref"))
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: calls.append("openalex"))
    resolvers.resolve("10.1000/jenny", Config())
    assert calls == ["crossref", "openalex"]


@pytest.mark.parametrize("use_local", [False, True])
def test_supplied_article_pdf_avoids_online_resolution(tmp_path, monkeypatch, use_local):
    from papr.input import materialize_article

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    article = Article("Supplied", doi="10.1000/jenny", local_pdf=str(pdf))
    calls = []
    monkeypatch.setattr(resolvers, "resolve", lambda *args, **kwargs: calls.append(kwargs))
    assert materialize_article(article, Config(), use_local=use_local) == (article, None)
    assert calls == []


@pytest.mark.parametrize("enrich", [False, True])
def test_supplied_article_enrich_is_explicit(tmp_path, monkeypatch, enrich):
    from papr.input import materialize_article

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    article = Article("Supplied", doi="10.1000/jenny", local_pdf=str(pdf))
    calls = []
    monkeypatch.setattr(resolvers, "resolve", lambda *args, **kwargs: calls.append(kwargs) or [])
    materialize_article(article, Config(), enrich=enrich)
    assert len(calls) == int(enrich)
    if enrich:
        assert calls[0]["enrich"] is True


@pytest.mark.parametrize("enrich", [False, True])
def test_supplied_pdf_online_metadata_requires_enrich(tmp_path, monkeypatch, enrich):
    from pypdf import PdfWriter

    from papr.input import materialize_article

    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Subject": "doi:10.1000/jenny"})
    writer.write(pdf)
    calls = []
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: calls.append("crossref"))
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: calls.append("openalex"))
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: calls.append("search") or [])
    materialize_article(pdf, Config(), use_local=False, enrich=enrich)
    assert bool(calls) is enrich
