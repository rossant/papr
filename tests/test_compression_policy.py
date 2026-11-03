from dataclasses import replace

import pytest

from papr.compression_policy import resolve_policy
from papr.config import Config


def test_balanced_is_default_policy():
    assert resolve_policy(Config()).to_dict() == {
        "profile": "balanced", "dpi": 180, "quality": 80,
        "downsample_threshold": 1.4, "min_savings_bytes": 4096,
        "min_savings_ratio": 0.01,
    }


def test_legacy_dpi_and_quality_override_profile_defaults():
    cfg = replace(Config(), pdf_compression_dpi=200, pdf_compression_quality=91)
    policy = resolve_policy(cfg)
    assert (policy.dpi, policy.quality) == (200, 91)


def test_compact_and_lossless_policy():
    compact = replace(Config(), pdf_compression_profile="compact")
    assert (resolve_policy(compact).dpi, resolve_policy(compact).quality) == (120, 65)
    lossless = replace(compact, pdf_compression_dpi=200, pdf_compression_quality=90,
                       pdf_compression_profile="lossless")
    assert (resolve_policy(lossless).dpi, resolve_policy(lossless).quality) == (0, 0)


@pytest.mark.parametrize("kwargs", [
    {"pdf_compression_profile": "unknown"},
    {"pdf_compression_dpi": 0},
    {"pdf_compression_dpi": True},
    {"pdf_compression_quality": 96},
    {"pdf_compression_quality": False},
])
def test_invalid_policy_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        replace(Config(), **kwargs)


def test_pdf_settings_load_and_preserve_unrelated_toml(tmp_path, monkeypatch):
    from papr.config import set_setting

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '# keep me\n[pdf]\nprofile = "compact"\ndpi = 200\nquality = 88\n'
        'timeout = 45 # keep this comment\n'
    )
    cfg = Config.load(config_file)
    assert (cfg.pdf_compression_profile, cfg.pdf_compression_dpi,
            cfg.pdf_compression_quality, cfg.pdf_compression_timeout) == ("compact", 200, 88, 45)
    monkeypatch.setattr(Config, "config_dir", property(lambda self: tmp_path))
    set_setting(cfg, "pdf.quality", "")
    updated = (tmp_path / "config.toml").read_text()
    assert "# keep me" in updated
    assert "timeout = 45 # keep this comment" in updated
    assert "quality =" not in updated
    assert Config.load(config_file).pdf_compression_quality is None


def test_compress_cli_passes_profile_and_quality_overrides(tmp_path, monkeypatch):
    import sys
    import types

    from papr.compress_cli import _compress_parser, _run_compress

    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")
    observed = []
    monkeypatch.setitem(sys.modules, "papr.compression", types.SimpleNamespace(
        compress_pdf=lambda path, cfg, enabled: (path, observed.append(cfg) or {
            "status": "unchanged", "saved_bytes": 0, "reason": "already compact",
        })
    ))
    args = _compress_parser().parse_args([
        str(source), "--profile", "compact", "--quality", "70", "--dpi", "125", "--quiet",
    ])
    assert _run_compress(args, Config()) == 0
    assert (observed[0].pdf_compression_profile, observed[0].pdf_compression_quality,
            observed[0].pdf_compression_dpi) == ("compact", 70, 125)
