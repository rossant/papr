from pathlib import Path

from papr.config import Config


def test_config_file(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'download_dir = "~/Papers"\n'
        'openalex_api_key = "abc"\n'
        '[filename]\n'
        'max_length = 99\n'
        '[processors.md]\n'
        'backend = "native"\n'
        '[local]\n'
        'zotero_data_dir = "~/Zotero"\n'
        'zolit_repo = "~/src/zolit"\n'
    )
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    cfg = Config.load(cfg_file)
    assert cfg.download_dir == Path.home() / "Papers"
    assert cfg.openalex_api_key == "abc"
    assert cfg.filename_max_length == 99
    assert cfg.md_backend == "native"
    assert cfg.zotero_data_dir == Path.home() / "Zotero"
    assert cfg.zolit_repo == Path.home() / "src" / "zolit"
