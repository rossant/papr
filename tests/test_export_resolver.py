"""Previously delivered originals remain available without publisher access."""

import json
from copy import deepcopy

import httpx
import pytest
from pdf_helpers import pdf_bytes

from papr import cli, resolvers
from papr.artifacts import remember
from papr.batch_catalog import register_manifest
from papr.batches import save_manifest
from papr.config import Config
from papr.model import Article, Person
from papr.resolvers import exports
from papr.resolvers.session import resolution_session
from papr.selection import select_candidate


@pytest.fixture
def config(tmp_path):
    return Config(download_dir=tmp_path / "output", pdf_compress=False, zotero_api_url="")


def record(tmp_path, config, *, article=None, name="paper", status="ok", pending=False):
    article = article or Article(
        "Previously recovered original paper", doi="10.1234/paper", pmid="12345678",
        authors=[Person(family="Example", given="Ada")], year=2020,
    )
    pdf = tmp_path / f"{name}.pdf"
    pdf.write_bytes(pdf_bytes(text=f"{article.title} doi: {article.doi or ''}"))
    artifact = remember(pdf, config)
    row = {"input": article.doi or article.title, "status": status, "article": article.to_dict(),
           "pending_artifacts" if pending else "artifacts": [artifact]}
    path = save_manifest([row], config, path=tmp_path / f"{name}.json")
    return article, pdf, artifact, path


def only_exports(monkeypatch):
    monkeypatch.setattr(resolvers, "LOCAL_DOI_LOOKUPS", (exports.by_doi,))
    monkeypatch.setattr(resolvers, "LOCAL_PMID_LOOKUPS", (exports.by_pmid,))
    monkeypatch.setattr(resolvers, "LOCAL_SEARCHERS", (exports.search,))


def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A recorded local PDF must not require network access")

    monkeypatch.setattr(httpx.Client, "get", forbidden)
    monkeypatch.setattr(resolvers.crossref, "by_doi", forbidden)
    monkeypatch.setattr(resolvers.openalex, "by_doi", forbidden)
    monkeypatch.setattr(resolvers.pubmed, "by_pmid", forbidden)
    monkeypatch.setattr(resolvers, "_remote_search", forbidden)


@pytest.mark.parametrize("query", ["10.1234/paper", "pmid:12345678", "Example 2020"])
@pytest.mark.parametrize("local_only", [False, True])
def test_cli_reuses_exported_pdf_without_remote_calls(
    tmp_path, config, monkeypatch, query, local_only,
):
    _, pdf, _, _ = record(tmp_path, config)
    only_exports(monkeypatch)
    forbid_network(monkeypatch)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    report = tmp_path / "report.json"
    options = [query, "--non-interactive", "--quiet", "--report", str(report)]
    if local_only:
        options.append("--local-only")
    assert cli.main(options) == 0
    row, = json.loads(report.read_text())
    assert row["status"] == "ok"
    assert row["resolver"] == "papr-exports"
    assert row["source"] == "local"
    assert len(row["outputs"]) == 1
    assert (config.download_dir / row["outputs"][0]).read_bytes() == pdf.read_bytes()


def test_default_offline_provider_order_includes_export_recovery_last():
    assert resolvers.LOCAL_DOI_LOOKUPS[-1] is exports.by_doi
    assert resolvers.LOCAL_PMID_LOOKUPS[-1] is exports.by_pmid
    assert resolvers.LOCAL_SEARCHERS[-1] is exports.search


def test_moved_original_is_recovered_from_its_content_cache(tmp_path, config):
    _, pdf, artifact, _ = record(tmp_path, config)
    original = pdf.read_bytes()
    pdf.unlink()
    result = exports.by_doi("10.1234/PAPER", config)
    assert result is not None
    assert result.local_pdf == str(config.cache_dir / "artifacts" / artifact["sha256"])
    assert (config.cache_dir / "artifacts" / artifact["sha256"]).read_bytes() == original


def test_changed_pdf_and_broken_manifest_do_not_hide_another_export(tmp_path, config):
    _, bad, _, _ = record(tmp_path, config, name="changed")
    bad.write_bytes(pdf_bytes(text="A changed document"))
    missing = tmp_path / "missing.json"
    register_manifest(missing, config, manifest_id="missing")
    malformed = tmp_path / "malformed.json"
    malformed.write_text("invalid")
    register_manifest(malformed, config, manifest_id="malformed")
    article, valid, _, _ = record(tmp_path, config, name="valid")
    result = exports.by_doi(article.doi, config)
    assert result is not None and result.local_pdf == str(valid)


def test_invalid_pdf_with_correct_recorded_checksum_is_not_reused(tmp_path, config):
    article, pdf, _, manifest = record(tmp_path, config)
    pdf.write_bytes(b"%PDF-1.7\ntruncated")
    artifact = remember(pdf, config)
    save_manifest([{"status": "ok", "input": article.doi,
                    "article": article.to_dict(), "artifacts": [artifact]}], config, manifest)
    assert exports.by_doi(article.doi, config) is None


@pytest.mark.parametrize("status,pending,expected", [
    ("error", False, True), ("dry-run", False, False), ("excluded", False, False),
    ("error", True, False), ("ok", True, False),
])
def test_only_published_pdfs_are_reused(tmp_path, config, status, pending, expected):
    article, _, _, _ = record(tmp_path, config, status=status, pending=pending)
    assert (exports.by_doi(article.doi, config) is not None) is expected


def test_metadata_only_export_and_unknown_identifier_do_not_match(tmp_path, config):
    article, _, _, _ = record(tmp_path, config)
    metadata = tmp_path / "metadata.bib"
    metadata.write_text("Bibliography only")
    other = Article("Another paper", doi="10.1234/other")
    save_manifest([{"status": "ok", "article": other.to_dict(),
                    "artifacts": [remember(metadata, config)]}], config)
    assert exports.by_doi(other.doi, config) is None
    assert exports.by_doi("10.1234/paper+suffix", config) is None
    assert exports.by_pmid("99999999", config) is None
    assert exports.by_pmid("", config) is None
    assert exports.by_doi(article.doi, config) is not None


def test_ambiguous_author_year_does_not_choose_an_arbitrary_pdf(tmp_path, config):
    first = Article("First paper", doi="10.1234/first", authors=[Person(family="Example")],
                    year=2020)
    second = Article("Second paper", doi="10.1234/second", authors=first.authors, year=2020)
    record(tmp_path, config, article=first, name="first")
    record(tmp_path, config, article=second, name="second")
    candidates = exports.search("Example 2020", config)
    assert len(candidates) == 2
    with pytest.raises(LookupError, match="Ambiguous"):
        select_candidate("Example 2020", candidates, config)


def test_library_reads_are_command_scoped_and_return_independent_objects(
    tmp_path, config, monkeypatch,
):
    article, _, _, _ = record(tmp_path, config)
    original = exports.known_manifest_paths
    reads = []

    def paths(cfg):
        reads.append(cfg)
        return original(cfg)

    monkeypatch.setattr(exports, "known_manifest_paths", paths)
    with resolution_session():
        first = exports.by_doi(article.doi, config)
        first.title = "Changed caller metadata"
        assert exports.by_pmid(article.pmid, config).title == article.title
        assert exports.search("Example 2020", config)[0].title == article.title
    assert len(reads) == 1
    assert exports.by_doi(article.doi, config).title == article.title
    assert len(reads) == 2


@pytest.mark.parametrize("disabled", ["flag", "config"])
def test_disabled_local_sources_skip_export_lookup(tmp_path, config, monkeypatch, disabled):
    record(tmp_path, config)
    only_exports(monkeypatch)
    calls = []
    monkeypatch.setattr(exports, "all_articles", lambda cfg: pytest.fail("Local lookup disabled"))
    monkeypatch.setattr(resolvers.crossref, "by_doi", lambda *args: calls.append("crossref"))
    monkeypatch.setattr(resolvers.openalex, "by_doi", lambda *args: calls.append("openalex"))
    if disabled == "config":
        config.local_sources = False
    assert resolvers.resolve("10.1234/paper", config, use_local=disabled != "flag") == []
    assert sorted(calls) == ["crossref", "openalex"]


def test_duplicate_manifests_do_not_create_false_ambiguity(tmp_path, config):
    article, _, _, manifest = record(tmp_path, config)
    row = json.loads(manifest.read_text())["items"][0]
    save_manifest([deepcopy(row)], config)
    candidates = exports.search("Example 2020", config)
    assert len(candidates) == 1
    assert select_candidate("Example 2020", candidates, config).doi == article.doi


def test_newer_verified_delivery_is_preferred_to_an_earlier_export(tmp_path, config):
    article, _, _, old_manifest = record(tmp_path, config, name="aaa-old")
    _, newer, _, new_manifest = record(tmp_path, config, name="zzz-new")
    for path, date in ((old_manifest, "2025-01-01T00:00:00+00:00"),
                       (new_manifest, "2026-01-01T00:00:00+00:00")):
        payload = json.loads(path.read_text())
        payload["created_at"] = date
        path.write_text(json.dumps(payload))
    assert exports.by_doi(article.doi, config).local_pdf == str(newer)
