from pathlib import Path

import pytest

from papr import __version__
from papr.config import Config
from papr.http import USER_AGENT


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"formats": []}, "non-empty list"),
        ({"formats": "pdf"}, "non-empty list"),
        ({"formats": ["pdf", 3]}, "unsupported format"),
        ({"md_backend": "unknown"}, "processors.md.backend"),
        ({"auto_accept_score": True}, "between 0 and 1"),
        ({"auto_accept_margin": 1.1}, "between 0 and 1"),
        ({"max_candidates": 0}, "positive integer"),
        ({"filename_max_length": 2.5}, "positive integer"),
        ({"filename_ascii": 1}, "filename.ascii"),
        ({"filename_template": None}, "filename_template"),
        ({"download_dir": "~/Papers"}, "download_dir"),
    ],
)
def test_config_rejects_invalid_values(kwargs: dict, message: str):
    with pytest.raises(ValueError, match=message):
        Config(**kwargs)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('formats = "pdf"', "formats must be a non-empty list"),
        ("formats = []", "formats must be a non-empty list"),
        ('formats = ["pdf", "nope"]', "unsupported format"),
        ('[filename]\nascii = "false"', "filename.ascii must be true or false"),
        ("[matching]\nauto_accept_score = true", "must be a number"),
        ('[matching]\nmax_candidates = "5"', "positive integer"),
        ('[processors.md]\nbackend = "bad"', "processors.md.backend"),
        ("[local]\nenabled = 0", "local.enabled must be true or false"),
        ("[filename]\nmax_length = 10\n\n[matching]\nauto_accept_margin = 2", "between 0 and 1"),
    ],
)
def test_config_load_rejects_invalid_values(tmp_path: Path, toml: str, message: str):
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml)
    with pytest.raises(ValueError, match=message):
        Config.load(config_file)


def test_config_load_rejects_non_table_sections(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('filename = "oops"')
    with pytest.raises(ValueError, match="filename must be a TOML table"):
        Config.load(config_file)


def test_http_user_agent_uses_package_version():
    assert USER_AGENT.startswith(f"papr/{__version__} ")


@pytest.mark.parametrize("name", ["zotero_data_dir", "zolit_repo", "zolit_db", "zolit_sbs_repo"])
def test_empty_optional_path_environment_preserves_config(tmp_path, monkeypatch, name):
    configured = tmp_path / "library"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[local]\n{name} = "{configured}"\n')
    monkeypatch.setenv(f"PAPR_{name.upper()}", "")
    assert getattr(Config.load(config_file), name) == configured


def test_mistral_key_file_is_loaded_without_overriding_explicit_key(tmp_path, monkeypatch):
    monkeypatch.setattr("papr.config.user_config_dir", lambda name: str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY_FILE", raising=False)
    (tmp_path / "mistral.key").write_text("  dummy-key\n")
    Config.load()
    import os

    assert os.environ["MISTRAL_API_KEY"] == "dummy-key"
    monkeypatch.setenv("MISTRAL_API_KEY", "explicit-key")
    Config.load()
    assert os.environ["MISTRAL_API_KEY"] == "explicit-key"


def test_mistral_key_file_can_be_configured(tmp_path, monkeypatch):
    monkeypatch.setattr("papr.config.user_config_dir", lambda name: str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    key_file = tmp_path / "custom.key"
    key_file.write_text("dummy-custom-key\n")
    monkeypatch.setenv("MISTRAL_API_KEY_FILE", str(key_file))
    Config.load()
    import os

    assert os.environ["MISTRAL_API_KEY"] == "dummy-custom-key"
