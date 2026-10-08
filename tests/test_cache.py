import hashlib
import json
import os
import time

import pytest

from papr.cache import prune, status
from papr.cache_cli import run
from papr.config import Config
from papr.model import Article


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config(download_dir=tmp_path / "Downloads")


def orphan(config, data=b"old orphan"):
    digest = hashlib.sha256(data).hexdigest()
    path = config.cache_dir / "artifacts" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    return path


def manifest(path, digests):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "papr-export-batch",
                "version": 1,
                "items": [
                    {
                        "status": "error",
                        "article": {"title": "Paper"},
                        "artifacts": [
                            {"path": "/exports/paper.pdf", "name": "paper.pdf", "sha256": d}
                            for d in digests[:1]
                        ],
                        "pending_artifacts": [
                            {"path": "/exports/pending.pdf", "name": "pending.pdf", "sha256": d}
                            for d in digests[1:]
                        ],
                    }
                ],
            }
        )
    )
    return path


def test_status_reports_only_known_cache_categories(config):
    (config.cache_dir / "artifacts").mkdir(parents=True)
    (config.cache_dir / "artifacts" / ("a" * 64)).write_bytes(b"123")
    (config.cache_dir / "pdf").mkdir()
    (config.cache_dir / "pdf" / "fetch.pdf").write_bytes(b"4567")
    (config.cache_dir / "private").mkdir()
    (config.cache_dir / "private" / "ignored").write_bytes(b"do not count")
    result = status(config)
    assert result["categories"] == {
        "artifacts": 3,
        "pdf": 4,
        "processors": 0,
        "recovered": 0,
    }
    assert result["total_bytes"] == 7


def test_prune_protects_history_manifest_artifacts_and_pending(config, monkeypatch):
    keep_history = orphan(config, b"history")
    keep_artifact = orphan(config, b"manifest artifact")
    keep_pending = orphan(config, b"pending artifact")
    remove = orphan(config, b"unreferenced")
    history = config.data_dir / "latest-download.json"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "pdf": "/exports/latest.pdf",
                "sha256": keep_history.name,
                "article": Article("Latest").to_dict(),
            }
        )
    )
    custom = manifest(
        config.data_dir.parent / "old-external" / "batch.json",
        [keep_artifact.name, keep_pending.name],
    )
    monkeypatch.setattr("papr.cache._manifest_paths", lambda _config: [custom])
    assert prune(config, older_than_days=1) == [remove]
    assert keep_history.exists()
    assert keep_artifact.exists()
    assert keep_pending.exists()
    assert not remove.exists()


def test_explicit_manifest_protects_old_unregistered_manifest(config):
    protected = orphan(config, b"external explicit")
    remove = orphan(config, b"other")
    custom = manifest(config.data_dir.parent / "legacy.json", [protected.name])
    assert prune(config, manifests=[custom], older_than_days=1) == [remove]
    assert protected.exists()
    assert not remove.exists()


def test_corrupt_history_or_manifest_blocks_all_pruning(config, monkeypatch):
    candidate = orphan(config)
    config.data_dir.mkdir(parents=True)
    (config.data_dir / "latest-download.json").write_text("{")
    with pytest.raises(ValueError, match="latest-download history"):
        prune(config, older_than_days=1)
    assert candidate.exists()

    (config.data_dir / "latest-download.json").unlink()
    broken = config.data_dir / "broken.json"
    broken.write_text("not json")
    monkeypatch.setattr("papr.cache._manifest_paths", lambda _config: [broken])
    with pytest.raises(ValueError, match="batch manifest"):
        prune(config, older_than_days=1)
    assert candidate.exists()


def test_symlink_escape_is_ignored_and_prune_does_not_delete_target(config, tmp_path):
    outside = tmp_path / "external"
    outside.write_bytes(b"external data")
    digest = hashlib.sha256(b"external data").hexdigest()
    directory = config.cache_dir / "artifacts"
    directory.mkdir(parents=True)
    link = directory / digest
    link.symlink_to(outside)
    old = time.time() - 10 * 86400
    os.utime(outside, (old, old))
    assert prune(config, older_than_days=1) == []
    assert link.is_symlink()
    assert outside.read_bytes() == b"external data"


def test_dry_run_reports_without_mutation_and_cli_json(config, capsys):
    candidate = orphan(config)
    selected = prune(config, dry_run=True, older_than_days=1)
    assert selected == [candidate]
    assert candidate.exists()
    assert run(["status", "--json"], config) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["categories"]["artifacts"] == len(b"old orphan")
    assert candidate.exists()


def test_recent_orphan_uses_age_grace(config):
    candidate = config.cache_dir / "artifacts" / ("b" * 64)
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"recent")
    assert prune(config, older_than_days=7) == []
    assert candidate.exists()
