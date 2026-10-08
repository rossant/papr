import json

import pytest

from papr import cli
from papr.batch_catalog import known_manifest_paths, list_manifests, resolve_manifest
from papr.batches import save_manifest
from papr.config import Config


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
    assert listed[0]["id"] == "custom"
    assert listed[0]["missing"] == 1
    assert resolve_manifest("reading", config) == external.resolve()
    assert resolve_manifest("custom", config) == external.resolve()
    assert external.resolve() in known_manifest_paths(config)


def test_automatic_manifests_and_catalog_metadata_glob(config, tmp_path):
    path = save_manifest([], config)
    paths = known_manifest_paths(config)
    assert path in paths
    assert config.data_dir / "batches" / ".catalog.json" not in paths
    assert len(list_manifests(config)) == 1


def test_duplicate_selector_is_rejected_as_ambiguous(config, tmp_path):
    save_manifest([], config, tmp_path / "a" / "same.json", name="shared")
    save_manifest([], config, tmp_path / "b" / "same.json", name="shared")
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
