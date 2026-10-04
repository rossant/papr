import pytest

from papr import cli
from papr.config import Config, set_setting


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "config_dir", property(lambda self: tmp_path))
    monkeypatch.delenv("PAPR_ZOTERO_COLLECTION", raising=False)
    return tmp_path / "config.toml"


def test_personal_collection_and_compression_settings(settings):
    settings.write_text('[zotero]\ncollection = "SBS"\n[pdf]\ncompress = true\ndpi = 200\n')
    config = Config.load(settings)
    assert config.zotero_collection == "SBS"
    assert config.pdf_compress is True
    assert config.pdf_compression_dpi == 200
    # A user's collection is never hard-coded as a project default.
    assert Config().zotero_collection is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        '# Personal settings\nformats = ["pdf", "md"]\n[local]\nenabled = true\n',
        '[zotero]\ncollection = "Old" # original\n\n[local]\nenabled = true\n',
        '[zotero] # personal\n\n# default collection\ncollection = "Old"\n',
    ],
)
def test_setting_preserves_other_sections(settings, text):
    settings.write_text(text)
    set_setting(Config.load(settings), "zotero.collection", "SBS")
    assert Config.load(settings).zotero_collection == "SBS"
    if "[local]" in text:
        assert "[local]\nenabled = true" in settings.read_text()
    if "# Personal settings" in text:
        assert "# Personal settings" in settings.read_text()
    set_setting(Config.load(settings), "pdf.compress", "false")
    assert Config.load(settings).pdf_compress is False
    assert Config.load(settings).zotero_collection == "SBS"


@pytest.mark.parametrize(
    "key,value",
    [
        ("zotero.collection", ""),
        ("pdf.dpi", "0"),
        ("pdf.timeout", "abc"),
        ("pdf.compress", "maybe"),
        ("unknown.option", "value"),
    ],
)
def test_bad_setting_preserves_config(settings, key, value):
    original = '# Existing\nformats = ["pdf"]\n'
    settings.write_text(original)
    with pytest.raises(ValueError):
        set_setting(Config.load(settings), key, value)
    assert settings.read_text() == original


def test_collection_environment_override(settings, monkeypatch):
    settings.write_text('[zotero]\ncollection = "SBS"\n')
    monkeypatch.setenv("PAPR_ZOTERO_COLLECTION", "Other")
    assert Config.load(settings).zotero_collection == "Other"


def test_config_set_command(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli.Config, "load", lambda: Config())
    assert cli.main(["config", "set", "zotero.collection", "SBS"]) == 0
    assert "zotero.collection = SBS" in capsys.readouterr().out
    assert 'collection = "SBS"' in settings.read_text()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pdf_compress": 1},
        {"pdf_compression_dpi": True},
        {"pdf_compression_timeout": -1},
        {"zotero_collection": 42},
    ],
)
def test_invalid_pdf_or_zotero_config(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)
