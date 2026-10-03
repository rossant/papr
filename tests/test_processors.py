from papr import export
from papr.config import Config
from papr.processors import mistral


def test_auto_markdown_cache_tracks_selected_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncontent")
    config = Config()
    calls = []

    def markdown(pdf, config, backend):
        calls.append((backend, config.mistral_model))
        return f"{backend}:{config.mistral_model}", backend

    monkeypatch.setattr(export, "markdown", markdown)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert export._cached_markdown(pdf, config, "auto")[1] == "native"
    assert export._cached_markdown(pdf, config, "auto")[1] == "native"
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    assert mistral.available()
    assert export._cached_markdown(pdf, config, "auto")[1] == "mistral"
    assert export._cached_markdown(pdf, config, "auto")[1] == "mistral"
    config.mistral_model = "another/model"
    assert export._cached_markdown(pdf, config, "auto")[0] == "mistral:another/model"
    assert len(calls) == 3
    assert list((config.cache_dir / "processors").glob("*.tmp")) == []
