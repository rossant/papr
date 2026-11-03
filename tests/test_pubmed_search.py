import httpx
import pytest

from papr import resolvers
from papr.config import Config
from papr.model import Article
from papr.resolvers import pubmed
from papr.selection import select_candidate

QUERY = "Suara Trouth Collins 2001 Benign subarachnoid space enlargement of infancy"
RECORD = """<PubmedArticleSet><PubmedArticle>
<MedlineCitation><PMID>12653385</PMID><Article>
<ArticleTitle>Benign <i>subarachnoid</i> space enlargement of infancy.</ArticleTitle>
<AuthorList><Author><LastName>Suara</LastName></Author></AuthorList>
<Journal><JournalIssue><PubDate><Year>2001</Year></PubDate></JournalIssue></Journal>
</Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pmc">PMC2640637</ArticleId>
</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""


def test_pubmed_fallback_resolves_work_missing_from_general_indexes(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["term"] == QUERY
            return httpx.Response(200, json={"esearchresult": {"idlist": ["12653385"]}})
        assert request.url.params["id"] == "12653385"
        return httpx.Response(200, text=RECORD)

    monkeypatch.setattr(
        pubmed, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(
        resolvers.crossref, "search", lambda *args: [Article("Unrelated", score=0.5)]
    )
    monkeypatch.setattr(resolvers.openalex, "search", lambda *args: [])
    config = Config()
    candidates = resolvers.resolve(QUERY, config, use_local=False)
    chosen = select_candidate(QUERY, candidates, config)
    assert chosen.title == "Benign subarachnoid space enlargement of infancy."
    assert chosen.pmid == "12653385"
    assert chosen.pmcid == "PMC2640637"
    assert len(calls) == 2


def test_confident_search_skips_pubmed(monkeypatch):
    monkeypatch.setattr(resolvers.crossref, "search", lambda *args: [Article("Paper", score=0.95)])
    monkeypatch.setattr(resolvers.openalex, "search", lambda *args: [])
    monkeypatch.setattr(pubmed, "search", lambda *args: pytest.fail("unnecessary fallback"))
    assert resolvers.resolve("Paper", Config(), use_local=False)[0].score == 0.95


def test_pubmed_failure_preserves_other_candidates(monkeypatch):
    monkeypatch.setattr(resolvers.crossref, "search", lambda *args: [Article("Paper", score=0.5)])
    monkeypatch.setattr(resolvers.openalex, "search", lambda *args: [])

    def fail(*args):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(pubmed, "search", fail)
    assert resolvers.resolve("Paper", Config(), use_local=False)[0].title == "Paper"
