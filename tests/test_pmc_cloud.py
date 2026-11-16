import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr.config import Config
from papr.fetchers import oa, pmc
from papr.model import Article

PMCID = "PMC7256766"
DOI = "10.1155/2020/4714927"


def listing(versions=(1,), token=None):
    prefixes = "".join(
        f"<CommonPrefixes><Prefix>{PMCID}.{v}/</Prefix></CommonPrefixes>" for v in versions
    )
    continuation = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{prefixes}<IsTruncated>{'true' if token else 'false'}</IsTruncated>"
        f"{continuation}</ListBucketResult>"
    )


def metadata(version=1, **changes):
    data = {
        "pmcid": PMCID, "doi": DOI, "is_manuscript": False,
        "pdf_url": f"s3://pmc-oa-opendata/{PMCID}.{version}/{PMCID}.{version}.pdf?md5=abc",
    }
    return data | changes


def test_cloud_fetch_works_without_publisher_browser_or_institution(tmp_path, monkeypatch):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        assert request.url.host == "pmc-oa-opendata.s3.amazonaws.com"
        if request.url.path == "/":
            assert request.url.params["prefix"] == f"{PMCID}."
            assert request.url.params["delimiter"] == "/"
            return httpx.Response(200, text=listing())
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=metadata())
        assert request.url.path == f"/{PMCID}.1/{PMCID}.1.pdf"
        assert not request.url.query
        return httpx.Response(200, content=pdf_bytes("article"))

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    dest = tmp_path / "article.pdf"
    source = oa.fetch(Article("Paper", doi=DOI, pmcid=PMCID), dest, Config())
    assert source == f"{pmc.BASE}{PMCID}.1/{PMCID}.1.pdf"
    assert dest.read_bytes() == pdf_bytes("article")
    assert len(requested) == 3


def test_cloud_checks_all_versions_instead_of_assuming_latest_is_published():
    def respond(request):
        if request.url.path == "/":
            return httpx.Response(200, text=listing((1, 2, 3)))
        version = int(request.url.path.split("/")[1].split(".")[1])
        return httpx.Response(200, json=metadata(version, is_manuscript=version != 2))

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert pmc.pdf_urls(c, PMCID, Article("Paper", doi=DOI)) == [
            f"{pmc.BASE}{PMCID}.2/{PMCID}.2.pdf"
        ]


@pytest.mark.parametrize("changes", [
    {"pmcid": "PMC123"}, {"doi": "10.1234/other"}, {"doi": None}, {"doi": 123},
    {"is_manuscript": True}, {"is_manuscript": "yes"}, {"is_manuscript": None},
    {"pdf_url": None}, {"pdf_url": "https://unrelated.example/paper.pdf"},
    {"pdf_url": f"s3://other-bucket/{PMCID}.1/{PMCID}.1.pdf"},
    {"pdf_url": f"s3://pmc-oa-opendata/{PMCID}.1/supplement.pdf"},
])
def test_cloud_rejects_mismatches_manuscripts_and_untrusted_pdf_keys(changes):
    def respond(request):
        return httpx.Response(200, text=listing()) if request.url.path == "/" else (
            httpx.Response(200, json=metadata(**changes))
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert pmc.pdf_urls(c, PMCID, Article("Paper", doi=DOI)) == []


def test_cloud_handles_pagination_missing_version_one_and_malformed_metadata():
    def respond(request):
        if request.url.path == "/":
            if "continuation-token" not in request.url.params:
                return httpx.Response(200, text=listing((2,), token="next-page"))
            assert request.url.params["continuation-token"] == "next-page"
            return httpx.Response(200, text=listing((3,)))
        if ".2/" in request.url.path:
            return httpx.Response(200, text="invalid JSON")
        return httpx.Response(200, json=metadata(3))

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert pmc.pdf_urls(c, PMCID, Article("Paper", doi=DOI)) == [
            f"{pmc.BASE}{PMCID}.3/{PMCID}.3.pdf"
        ]


def test_cloud_outage_falls_back_to_known_pdf(tmp_path, monkeypatch):
    def respond(request):
        if request.url.host == "pmc-oa-opendata.s3.amazonaws.com":
            return httpx.Response(503)
        assert str(request.url) == "https://publisher.example/article.pdf"
        return httpx.Response(200, content=pdf_bytes("article"))

    monkeypatch.setattr(oa, "client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    source = oa.fetch(
        Article("Paper", pmcid=PMCID, oa_pdf_url="https://publisher.example/article.pdf"),
        tmp_path / "article.pdf", Config(),
    )
    assert source == "https://publisher.example/article.pdf"
