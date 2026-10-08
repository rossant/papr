import json
import uuid

import pytest

from papr import cli
from papr.batch_catalog import (
    known_manifest_paths,
    list_manifests,
    manifest_summary,
    resolve_manifest,
)
from papr.batches import record_artifacts, save_manifest
from papr.config import Config
from papr.model import Article


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config(download_dir=tmp_path / "Downloads", pdf_compress=False)


def test_named_and_external_manifests_are_listed_and_resolvable(config, tmp_path):
    external = tmp_path / "outside" / "custom.json"
    external.parent.mkdir()
    save_manifest([{"status": "error", "input": "unresolved"}], config, external, name="reading")

    listed = list_manifests(config)
    assert len(listed) == 1
    assert listed[0]["name"] == "reading"
    assert uuid.UUID(listed[0]["id"]).hex == listed[0]["id"]
    assert listed[0]["missing"] == 1
    assert resolve_manifest("reading", config) == external.resolve()
    assert resolve_manifest(listed[0]["id"], config) == external.resolve()
    assert external.resolve() in known_manifest_paths(config)


def test_automatic_manifests_and_catalog_metadata_glob(config, tmp_path):
    path = save_manifest([], config)
    paths = known_manifest_paths(config)
    assert path in paths
    assert config.data_dir / "batches" / ".catalog.json" not in paths
    assert len(list_manifests(config)) == 1


def test_duplicate_selector_is_rejected_as_ambiguous(config, tmp_path):
    save_manifest([], config, tmp_path / "a" / "same.json", name="shared")
    second = save_manifest([], config, tmp_path / "b" / "same.json", name="shared")
    listed = list_manifests(config)
    assert len({row["id"] for row in listed}) == 2
    assert resolve_manifest(listed[1]["id"], config) in {
        (tmp_path / "a" / "same.json").resolve(),
        second.resolve(),
    }
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_manifest("shared", config)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_manifest("same", config)


def test_malformed_catalog_fails_closed(config):
    path = config.data_dir / "batches" / ".catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": "papr-batch-catalog",
                "version": 1,
                "manifests": [{"path": "/external/private.json", "id": "x"}],
            }
        )
    )
    with pytest.raises(ValueError, match="Cannot read batch catalog"):
        known_manifest_paths(config)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "papr-batch-catalog", "version": True, "manifests": []},
        {
            "schema": "papr-batch-catalog",
            "version": 1,
            "manifests": [{"id": "a", "name": None, "path": "/a\x00b"}],
        },
    ],
)
def test_malformed_catalog_records_fail_closed(config, payload):
    path = config.data_dir / "batches" / ".catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Cannot read batch catalog"):
        known_manifest_paths(config)


def test_dangling_catalog_symlink_fails_closed(config, tmp_path):
    path = config.data_dir / "batches" / ".catalog.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "absent-catalog.json")
    with pytest.raises(ValueError, match="Cannot read batch catalog"):
        known_manifest_paths(config)


def test_missing_registered_manifest_remains_visible(config, tmp_path):
    missing = tmp_path / "deleted.json"
    # Register an existing file, then remove it to simulate a dangling catalog record.
    save_manifest([], config, missing, name="archived")
    missing.unlink()
    result = list_manifests(config)
    assert len(result) == 1
    assert result[0]["name"] == "archived"
    assert result[0]["manifest_status"] == "missing"
    assert missing.resolve() in known_manifest_paths(config)


def test_availability_checks_exact_hash_and_cache_fallback(config, tmp_path):
    from papr.artifacts import cache_path

    artifact = tmp_path / "paper.txt"
    artifact.write_text("verified content")
    row = {"status": "ok", **record_artifacts(Article("Paper"), [artifact], config)}
    save_manifest([row], config, tmp_path / "availability.json")
    assert list_manifests(config)[0]["available"] == 1

    digest = row["artifacts"][0]["sha256"]
    artifact.unlink()
    result = list_manifests(config)[0]
    assert result["available"] == 1  # exact-hash cache copy is usable

    cache_path(digest, config).unlink()
    result = list_manifests(config)[0]
    assert result["missing"] == 1
    assert result["items"][0]["availability"] == "missing"

    artifact.write_text("changed content")
    result = list_manifests(config)[0]
    assert result["changed"] == 1
    assert result["items"][0]["availability"] == "changed"


def test_unreadable_artifact_status_is_reported(config, tmp_path, monkeypatch):
    from papr import artifacts

    monkeypatch.setattr(
        artifacts,
        "locate",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    data = {
        "items": [
            {
                "status": "ok",
                "article": {"title": "Paper"},
                "artifacts": [{"path": "/paper", "sha256": "x"}],
            }
        ]
    }
    assert manifest_summary(data, config)["unreadable"] == 1


def test_batch_list_summary_and_show_selector_compatibility(config, tmp_path, capsys):
    save_manifest(
        [{"status": "error", "input": "query"}], config, tmp_path / "outside.json", name="weekly"
    )
    assert cli.main(["batch", "list"]) == 0
    assert "weekly" in capsys.readouterr().out
    assert cli.main(["batch", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "weekly"
    assert cli.main(["batch", "summary", "weekly"]) == 0
    assert "1 missing" in capsys.readouterr().out
    assert cli.main(["batch", "show", "weekly"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["input"] == "query"


def test_get_names_batch_and_reuses_it_offline(config, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Config, "load", lambda: config)
    article = Article("Synthetic paper", year=2024)
    source = tmp_path / "source.json"
    source.write_text(json.dumps(article.to_dict()))
    assert cli.main(
        [str(source), "--local-only", "-f", "json", "--batch-name", "weekly", "--quiet"]
    ) == 0
    first_output = next(config.download_dir.glob("*.json"))
    assert json.loads(first_output.read_text())["title"] == article.title
    assert list_manifests(config)[0]["name"] == "weekly"
    destination = tmp_path / "export"
    assert cli.main(["batch", "export", "weekly", "-o", str(destination), "--name", "copy"]) == 0
    assert next(destination.glob("*.json")).read_bytes() == first_output.read_bytes()
    assert resolve_manifest("copy", config).is_file()
    capsys.readouterr()


def test_get_accepts_named_delivered_exclusion(config, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Config, "load", lambda: config)
    article = Article("Synthetic paper", doi="10.1000/example")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\nsynthetic")
    save_manifest(
        [{"status": "ok", **record_artifacts(article, [pdf], config)}],
        config,
        name="delivered",
    )
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **k: (article, None))
    assert cli.main(["query", "--exclude-delivered", "delivered", "--batch-name", "next"]) == 0
    report = json.loads(resolve_manifest("next", config).read_text())
    assert report["items"][0]["status"] == "excluded"
    assert not config.download_dir.exists()
    assert "0 succeeded, 0 failed, 1 excluded" in capsys.readouterr().err
