from pathlib import Path

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


@pytest.mark.parametrize(
    "enrich,use_local,use_remote,expected_calls",
    [
        (False, True, True, 0),
        (True, True, True, 2),
        (False, False, True, 2),
        (True, True, False, 0),
    ],
)
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
    assert sorted(calls) == ["crossref", "openalex"]


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


@pytest.mark.parametrize("query,method", [("10.1000/example", "by_doi"), ("Some paper", "search")])
def test_remote_providers_overlap_with_stable_merge_order(monkeypatch, query, method):
    import threading

    barrier = threading.Barrier(2, timeout=3)
    openalex_finished = threading.Event()
    threads = []

    def crossref(*args):
        threads.append(threading.get_ident())
        barrier.wait()
        assert openalex_finished.wait(3)
        article = Article("Same paper", doi="10.1000/example", journal="Crossref journal")
        return article if method == "by_doi" else [article]

    def openalex(*args):
        threads.append(threading.get_ident())
        barrier.wait()
        openalex_finished.set()
        article = Article(
            "Same paper",
            doi="10.1000/example",
            journal="OpenAlex journal",
            abstract="OpenAlex abstract",
        )
        return article if method == "by_doi" else [article]

    monkeypatch.setattr(resolvers.crossref, method, crossref)
    monkeypatch.setattr(resolvers.openalex, method, openalex)
    result = resolvers.resolve(query, Config(), use_local=False)
    assert len(set(threads)) == 2
    assert len(result) == 1
    assert result[0].journal == "Crossref journal"
    assert result[0].abstract == "OpenAlex abstract"


def test_concurrent_providers_copy_context_without_sharing_reporter(monkeypatch):
    import threading
    from contextvars import ContextVar

    from papr.progress import Reporter, emit

    command_context = ContextVar("test_metadata_context", default=None)
    barrier = threading.Barrier(2, timeout=3)
    contexts = []
    events = []
    main_thread = threading.get_ident()

    def provider(*args):
        contexts.append(command_context.get())
        # Separate copied contexts may be changed independently by each worker.
        command_context.set("worker")
        barrier.wait()
        emit("resolve", "Worker event")
        return None

    monkeypatch.setattr(resolvers.crossref, "by_doi", provider)
    monkeypatch.setattr(resolvers.openalex, "by_doi", provider)
    reporter = Reporter(enabled=False)
    monkeypatch.setattr(reporter, "event", lambda *a, **k: events.append(threading.get_ident()))
    token = command_context.set("command")
    try:
        with reporter:
            assert resolvers.resolve("10.1000/example", Config(), use_local=False) == []
        assert command_context.get() == "command"
    finally:
        command_context.reset(token)
    assert contexts == ["command", "command"]
    assert events == [main_thread, main_thread]


def test_concurrent_provider_failure_keeps_successful_sibling(monkeypatch):
    calls = []

    def broken(*args):
        calls.append("crossref")
        raise ValueError("Unavailable")

    def available(*args):
        calls.append("openalex")
        return Article("Available", doi="10.1000/example")

    monkeypatch.setattr(resolvers.crossref, "by_doi", broken)
    monkeypatch.setattr(resolvers.openalex, "by_doi", available)
    result = resolvers.resolve("10.1000/example", Config(), use_local=False)
    assert sorted(calls) == ["crossref", "openalex"]
    assert [article.title for article in result] == ["Available"]


@pytest.mark.parametrize("query", ["10.1000/jenny", "pmid:123456", "Jenny 2006"])
def test_later_valid_pdf_survives_local_deduplication(tmp_path, monkeypatch, query):
    invalid = tmp_path / "invalid.pdf"
    invalid.write_text("not a PDF")
    valid = tmp_path / "valid.pdf"
    valid.write_bytes(b"%PDF-1.7\n")
    earlier = _jenny("Known locally")
    earlier.pmid = "123456"
    earlier.local_pdf = str(invalid)
    later = _jenny("Known locally")
    later.pmid = "123456"
    later.local_pdf = str(valid)
    calls = []
    lookups = (lambda *args: earlier, lambda *args: later)
    monkeypatch.setattr(resolvers, "LOCAL_DOI_LOOKUPS", lookups)
    monkeypatch.setattr(resolvers, "LOCAL_PMID_LOOKUPS", lookups)
    monkeypatch.setattr(
        resolvers,
        "LOCAL_SEARCHERS",
        (
            lambda *args: [earlier],
            lambda *args: [later],
        ),
    )
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: calls.append("crossref"))
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: calls.append("openalex"))
    monkeypatch.setattr(resolvers.pubmed, "by_pmid", lambda *args: calls.append("pubmed"))
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: calls.append("search") or [])
    result = resolvers.resolve(query, Config())
    assert len(result) == 1
    assert result[0] is earlier
    assert result[0].local_pdf == str(valid)
    assert calls == []


@pytest.mark.parametrize(
    "api_url,enrich,expect_api",
    [
        ("https://zotero.example.org/api", False, False),
        ("https://zotero.example.org/api", True, True),
        ("http://localhost:23119/api", False, True),
        ("http://127.0.0.1:23119/api", False, True),
        ("http://[::1]:23119/api", False, True),
    ],
)
def test_supplied_pdf_limits_zotero_to_desktop_unless_enrich(
    tmp_path, monkeypatch, api_url, enrich, expect_api
):
    import httpx
    from pypdf import PdfWriter

    from papr.input import materialize_article
    from papr.resolvers import zotero

    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Subject": "doi:10.1000/jenny"})
    writer.write(pdf)
    calls = []

    def get(client, url, **kwargs):
        calls.append(url)
        if not expect_api:
            raise AssertionError("Hosted Zotero API must not be contacted for a supplied PDF")
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(resolvers, "LOCAL_DOI_LOOKUPS", (zotero.by_doi,))
    monkeypatch.setattr(resolvers, "LOCAL_SEARCHERS", (zotero.search,))
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: None)
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: None)
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: [])
    # Nonexistent explicit directory prevents discovering a developer's real library.
    config = Config(zotero_api_url=api_url, zotero_data_dir=tmp_path / "no-library")
    article, source = materialize_article(pdf, config, enrich=enrich)
    assert article.doi == "10.1000/jenny"
    assert source == pdf
    assert bool(calls) is expect_api
    assert config.zotero_api_url == api_url


def test_supplied_pdf_config_copy_preserves_local_paths_and_subclass_state(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    from papr import local

    class CustomConfig(Config):
        @property
        def custom_property(self):
            return "available"

    config = CustomConfig(
        zotero_api_url="https://zotero.example.org/api",
        zotero_data_dir=Path("relative-zotero"),
        zolit_db=Path("relative-zolit.sqlite"),
    )
    config.extra_state = object()
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    seen = []
    monkeypatch.setattr(local, "resolve", lambda query, cfg, **opts: seen.append(cfg) or [])
    local.article_from_pdf(pdf, config)
    assert seen
    for copied in seen:
        assert copied is not config
        assert isinstance(copied, CustomConfig)
        assert copied.custom_property == "available"
        assert copied.extra_state is config.extra_state
        assert copied.zotero_api_url == ""
        assert copied.zotero_data_dir == Path("relative-zotero")
        assert copied.zolit_db == Path("relative-zolit.sqlite")
    assert config.zotero_api_url == "https://zotero.example.org/api"


@pytest.mark.parametrize("other_pdf_valid", [False, True])
def test_supplied_article_keeps_its_pdf_after_enrichment(tmp_path, monkeypatch, other_pdf_valid):
    from papr.input import materialize_article

    supplied_pdf = tmp_path / "supplied.pdf"
    supplied_pdf.write_bytes(b"%PDF-1.7\n")
    other_pdf = tmp_path / "other.pdf"
    other_pdf.write_bytes(b"%PDF-1.7\n" if other_pdf_valid else b"not a PDF")
    supplied = Article("Supplied", doi="10.1000/jenny", local_pdf=str(supplied_pdf))
    enriched = Article("Enriched", doi=supplied.doi, local_pdf=str(other_pdf), abstract="Metadata")
    monkeypatch.setattr(resolvers, "resolve", lambda *args, **kwargs: [enriched])
    result, _ = materialize_article(supplied, Config(), enrich=True)
    assert result.abstract == "Metadata"
    assert result.local_pdf == str(supplied_pdf)


@pytest.mark.parametrize("query", ["10.1000/jenny", "pmid:123456", "Jenny 2006"])
def test_all_offline_sources_precede_hosted_zotero(tmp_path, monkeypatch, query):
    import httpx

    from papr.resolvers import zotero

    pdf = tmp_path / "zolit.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    zolit_article = _jenny("Known locally")
    zolit_article.pmid = "123456"
    zolit_article.source = "zolit"
    zolit_article.local_pdf = str(pdf)
    requests = []

    def get(client, url, **kwargs):
        requests.append(url)
        raise AssertionError("Offline sources must be checked before the hosted Zotero API")

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(zotero, "_sqlite_all", lambda config: [])
    monkeypatch.setattr(
        resolvers, "LOCAL_DOI_LOOKUPS", (zotero.by_doi, lambda *args: zolit_article)
    )
    monkeypatch.setattr(
        resolvers, "LOCAL_PMID_LOOKUPS", (zotero.by_pmid, lambda *args: zolit_article)
    )
    monkeypatch.setattr(
        resolvers, "LOCAL_SEARCHERS", (zotero.search, lambda *args: [zolit_article])
    )
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: requests.append("crossref"))
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: requests.append("openalex"))
    monkeypatch.setattr(resolvers.pubmed, "by_pmid", lambda *args: requests.append("pubmed"))
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: requests.append("search") or [])
    config = Config(zotero_api_url="https://zotero.example.org/api")
    result = resolvers.resolve(query, config)
    assert result == [zolit_article]
    assert result[0].local_pdf == str(pdf)
    assert requests == []
    assert config.zotero_api_url == "https://zotero.example.org/api"


@pytest.mark.parametrize("query", ["10.1000/jenny", "Jenny 2006"])
@pytest.mark.parametrize(
    "pdf_valid,enrich,use_remote,expect_hosted",
    [
        (False, False, True, True),
        (True, True, True, True),
        (False, False, False, False),
        (False, True, False, False),
        (True, True, False, False),
    ],
)
def test_hosted_zotero_fallback_is_available_after_offline_preflight(
    tmp_path, monkeypatch, query, pdf_valid, enrich, use_remote, expect_hosted
):
    import httpx

    from papr.resolvers import zotero

    pdf = tmp_path / "zolit.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" if pdf_valid else b"not a PDF")
    local_article = _jenny("Known locally")
    local_article.local_pdf = str(pdf)
    requests = []

    def get(client, url, **kwargs):
        requests.append(url)
        data = [
            {
                "data": {
                    "title": "Known locally",
                    "DOI": "10.1000/jenny",
                    "date": "2006",
                    "creators": [{"creatorType": "author", "lastName": "Jenny"}],
                    "abstractNote": "Hosted metadata",
                }
            }
        ]
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(zotero, "_sqlite_all", lambda config: [])
    monkeypatch.setattr(
        resolvers, "LOCAL_DOI_LOOKUPS", (zotero.by_doi, lambda *args: local_article)
    )
    monkeypatch.setattr(
        resolvers, "LOCAL_SEARCHERS", (zotero.search, lambda *args: [local_article])
    )
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: None)
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: None)
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: [])
    config = Config(zotero_api_url="https://zotero.example.org/api")
    result = resolvers.resolve(query, config, enrich=enrich, use_remote=use_remote)
    assert bool(requests) is expect_hosted
    assert len(result) == 1
    assert result[0].abstract == ("Hosted metadata" if expect_hosted else None)
    assert config.zotero_api_url == "https://zotero.example.org/api"


def test_ambiguous_offline_pdf_does_not_bypass_hosted_metadata_lookup(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    first = _jenny("First paper", "10.1000/first")
    first.local_pdf = str(pdf)
    second = _jenny("Second paper", "10.1000/second")
    second.score = first.score - 0.01
    seen = []

    def search(query, config):
        seen.append(config.zotero_api_url)
        return [first, second]

    monkeypatch.setattr(resolvers, "_local_search", search)
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: [])
    config = Config(zotero_api_url="https://zotero.example.org/api")
    result = resolvers.resolve("Jenny 2006", config)
    assert result == [first, second]
    assert seen == ["", config.zotero_api_url]


@pytest.mark.parametrize("identifier", ["pmid", "none"])
def test_imported_article_finds_local_pdf_without_external_calls(tmp_path, monkeypatch, identifier):
    import httpx

    from papr.input import materialize_article
    from papr.resolvers import zotero

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    supplied = Article("Known locally", authors=[Person(family="Jenny")], year=2006)
    if identifier == "pmid":
        supplied.pmid = "123456"
    local_article = _jenny("Known locally")
    local_article.pmid = "123456"
    local_article.local_pdf = str(pdf)
    requests = []

    def get(client, url, **kwargs):
        requests.append(url)
        raise AssertionError("Imported article's local PDF must precede online metadata")

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(zotero, "_sqlite_all", lambda config: [local_article])
    monkeypatch.setattr(resolvers, "LOCAL_PMID_LOOKUPS", (zotero.by_pmid,))
    monkeypatch.setattr(resolvers, "LOCAL_SEARCHERS", (zotero.search,))
    monkeypatch.setattr(resolvers.pubmed, "by_pmid", lambda *args: requests.append("pubmed"))
    monkeypatch.setattr(resolvers, "_remote_search", lambda *args: requests.append("remote") or [])
    config = Config(zotero_api_url="https://zotero.example.org/api")
    result, _ = materialize_article(supplied, config)
    assert result.local_pdf == str(pdf)
    assert result.doi == "10.1000/jenny"
    assert requests == []


@pytest.mark.parametrize("candidates", ["missing", "ambiguous", "weak"])
def test_identifierless_import_preserves_input_without_confident_local_match(
    monkeypatch, candidates
):
    from papr.input import materialize_article

    supplied = Article("Known locally", authors=[Person(family="Jenny")], year=2006)
    first = _jenny("Known locally")
    second = _jenny("Another paper", doi="10.1000/other")
    second.score = first.score
    result = [] if candidates == "missing" else [first, second]
    if candidates == "weak":
        first.score = 0.1
        result = [first]
    calls = []

    def resolve(query, config, **options):
        calls.append(options)
        return result

    monkeypatch.setattr(resolvers, "resolve", resolve)
    assert materialize_article(supplied, Config(), enrich=True) == (supplied, None)
    assert calls == [{"use_local": True, "use_remote": False, "enrich": True}]


@pytest.mark.parametrize(
    "use_local,use_remote,enrich",
    [
        (True, False, False),
        (False, True, False),
        (True, True, True),
    ],
)
def test_imported_pmid_preserves_resolution_flags(monkeypatch, use_local, use_remote, enrich):
    from papr.input import materialize_article

    supplied = Article("Supplied", pmid="123456")
    calls = []

    def resolve(query, config, **options):
        calls.append((query, options))
        return []

    monkeypatch.setattr(resolvers, "resolve", resolve)
    assert materialize_article(
        supplied, Config(), use_local=use_local, use_remote=use_remote, enrich=enrich
    ) == (supplied, None)
    expected = {"use_local": use_local, "use_remote": use_remote}
    if enrich:
        expected["enrich"] = True
    assert calls == [("pmid:123456", expected)]


def test_identifierless_import_respects_disabled_local_sources(monkeypatch):
    from papr.input import materialize_article

    supplied = Article("Supplied")
    calls = []
    monkeypatch.setattr(resolvers, "resolve", lambda *args, **kwargs: calls.append(kwargs))
    assert materialize_article(supplied, Config(), use_local=False) == (supplied, None)
    assert materialize_article(supplied, Config(local_sources=False)) == (supplied, None)
    assert calls == []
