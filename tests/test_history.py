import json
from pathlib import Path

import pytest

from papr.config import Config
from papr.history import load_latest, record_download
from papr.model import Article, Person


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config(download_dir=tmp_path / "Downloads")


def test_round_trip_preserves_metadata_and_export_path(tmp_path, config):
    pdf = tmp_path / "exported.pdf"
    pdf.write_bytes(b"%PDF-1.7\nexported paper")
    article = Article(
        "A paper",
        authors=[Person("Smith", "Jane", "0000-0001")],
        year=2024,
        doi="10.1234/example",
        journal="A journal",
        abstract="Details",
        local_pdf="/old/cache/path.pdf",
        score=0.95,
        source="crossref",
    )
    record_download(article, pdf, config)
    loaded, path = load_latest(config)
    assert loaded == article
    assert loaded.authors[0].orcid == "0000-0001"
    assert path == pdf.resolve()
    assert not list(config.data_dir.glob("*.tmp"))
    assert (config.data_dir / "latest-download.json").stat().st_mode & 0o777 == 0o600


def test_absent_history_does_not_scan_downloads(config):
    config.download_dir.mkdir()
    (config.download_dir / "newest.pdf").write_bytes(b"%PDF-1.7\nother paper")
    with pytest.raises(ValueError, match="Download a paper with papr first"):
        load_latest(config)


@pytest.mark.parametrize("value", ["{", "null", "[]", "{}", '{"version": true}', b"\xff"])
def test_malformed_history_has_clear_error(config, value):
    config.data_dir.mkdir()
    path = config.data_dir / "latest-download.json"
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value)
    with pytest.raises(ValueError, match="Invalid latest-download history"):
        load_latest(config)


@pytest.mark.parametrize("change", ["modified", "not-pdf"])
def test_recorded_file_must_still_match(tmp_path, config, change):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfirst")
    record_download(Article("First"), pdf, config)
    if change == "missing":
        pdf.unlink()
    elif change == "modified":
        pdf.write_bytes(b"%PDF-1.7\nother")
    else:
        pdf.write_text("HTML error page")
    with pytest.raises(ValueError, match="missing|changed|no longer a PDF"):
        load_latest(config)


def test_missing_export_recovers_exact_content_from_cache(tmp_path, config):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfirst")
    record_download(Article("First"), pdf, config)
    pdf.rename(tmp_path / "renamed.pdf")
    article, recovered = load_latest(config)
    assert article.title == "First"
    assert recovered.read_bytes() == b"%PDF-1.7\nfirst"
    assert recovered.name == "paper.pdf"
    assert recovered.is_relative_to(config.cache_dir / "recovered")


def test_legacy_history_recovers_move_to_downloads_by_checksum(tmp_path, config):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfirst")
    record_download(Article("First"), pdf, config)
    for cached in (config.cache_dir / "artifacts").iterdir():
        cached.unlink()
    config.download_dir.mkdir()
    destination = config.download_dir / pdf.name
    pdf.rename(destination)
    assert load_latest(config)[1] == destination
    destination.write_bytes(b"%PDF-1.7\nwrong")
    with pytest.raises(ValueError, match="missing"):
        load_latest(config)


def test_latest_is_last_recorded_export_independent_of_mtime(tmp_path, config):
    first, last, unrelated = (tmp_path / name for name in ("first.pdf", "last.pdf", "other.pdf"))
    for pdf in (first, last):
        pdf.write_bytes(b"%PDF-1.7\npaper")
        record_download(Article(pdf.stem), pdf, config)
    unrelated.write_bytes(b"%PDF-1.7\nnewer unrelated file")
    loaded, pdf = load_latest(config)
    assert loaded.title == "last"
    assert pdf == last


@pytest.mark.parametrize("field,value", [("authors", [42]), ("year", "2024"), ("title", 42)])
def test_malformed_metadata_is_rejected(tmp_path, config, field, value):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\npaper")
    record_download(Article("Paper"), pdf, config)
    history = config.data_dir / "latest-download.json"
    data = json.loads(history.read_text())
    data["article"][field] = value
    history.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid latest-download history"):
        load_latest(config)


def test_failed_atomic_replace_preserves_previous_history(tmp_path, config, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\npaper")
    record_download(Article("Previous"), pdf, config)

    def fail_replace(self, destination):
        raise OSError("disk error")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        record_download(Article("New"), pdf, config)
    assert load_latest(config)[0].title == "Previous"
    assert not list(config.data_dir.glob("*.tmp"))


def test_unreadable_pdf_has_clear_error(tmp_path, config, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\npaper")
    record_download(Article("Paper"), pdf, config)
    original = Path.open

    def unreadable(self, *args, **kwargs):
        if self == pdf:
            raise PermissionError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(ValueError, match="PDF cannot be read"):
        load_latest(config)


def test_invalid_export_cannot_replace_previous_history(tmp_path, config):
    valid = tmp_path / "valid.pdf"
    valid.write_bytes(b"%PDF-1.7\npaper")
    record_download(Article("Previous"), valid, config)
    invalid = tmp_path / "invalid.pdf"
    invalid.write_text("an error response")
    with pytest.raises(ValueError, match="no longer a PDF"):
        record_download(Article("Wrong"), invalid, config)
    assert load_latest(config) == (Article("Previous"), valid)
