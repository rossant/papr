"""Image optimization, preservation checks and cache reuse work as one pipeline."""

import json
import shutil
import subprocess
from dataclasses import replace

import pytest
from pdf_helpers import pdf_bytes

from papr import compression
from papr.config import Config


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from test_compression import make_pdf

    config = Config(pdf_compression_dpi=180)
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    monkeypatch.setattr(compression, "tool_version", lambda path: "test-version")
    source = make_pdf(tmp_path / "source.pdf", padding=3000, annotation=True)
    compact = make_pdf(tmp_path / "compact.pdf", annotation=True)
    info = {"backend": "pymupdf", "eligible": True, "library_version": "2.1",
            "engine_version": "1.28.6", "pillow_version": "test"}
    monkeypatch.setattr(compression, "backend_info", lambda: info.copy())
    calls = []

    def image_worker(pdf, output, policy, timeout):
        assert pdf == source and timeout == config.pdf_compression_timeout
        calls.append(policy)
        shutil.copyfile(compact, output)
        return {"changed_images": 1, "skipped_images": 2, "lossy_applied": True}

    monkeypatch.setattr(compression, "compress_images", image_worker)
    return config, source, calls, info


def test_links_do_not_disable_image_backend_and_cache_retains_evidence(setup):
    config, source, calls, _ = setup
    original = source.read_bytes()
    output, report = compression.compress_pdf(source, config)
    assert report["status"] == "compressed" and report["backend"] == "pymupdf"
    assert report["requested_profile"] == "balanced" and report["resolved_quality"] == 80
    assert report["lossy_applied"] and report["changed_images"] == 1
    assert report["validation"]["annotations_preserved"]
    cached, evidence = compression.compress_pdf(source, config)
    assert cached == output and len(calls) == 1
    for name in ("requested_profile", "backend_versions", "changed_images", "resolved_quality"):
        assert report[name] == evidence[name]
    assert evidence["status"] == "cached" and evidence["cached"]
    assert source.read_bytes() == original


def test_optimized_pdf_copied_out_is_not_reencoded(setup, tmp_path):
    config, source, calls, _ = setup
    output, _ = compression.compress_pdf(source, config)
    copy = tmp_path / "delivered.pdf"
    shutil.copyfile(output, copy)
    repeated, report = compression.compress_pdf(copy, config)
    assert repeated == copy and len(calls) == 1
    assert report["reason"] == "already optimized with these settings"


def test_quality_profile_and_backend_version_invalidate_cache(setup):
    config, source, calls, info = setup
    first, _ = compression.compress_pdf(source, config)
    second, _ = compression.compress_pdf(source, replace(config, pdf_compression_quality=85))
    third, _ = compression.compress_pdf(source, replace(config, pdf_compression_profile="compact"))
    info["engine_version"] = "next-tested-version"
    fourth, _ = compression.compress_pdf(source, config)
    assert len({first, second, third, fourth}) == len(calls) == 4


def test_damaged_but_parseable_cached_pdf_is_not_reused(setup):
    config, source, calls, _ = setup
    output, _ = compression.compress_pdf(source, config)
    output.write_bytes(pdf_bytes(text="A different document"))
    repaired, report = compression.compress_pdf(source, config)
    assert repaired == output and report["status"] == "compressed"
    assert len(calls) == 2


def test_semantic_loss_from_worker_falls_back_without_losing_source(setup, monkeypatch):
    config, source, _, _ = setup
    original = source.read_bytes()

    def lost_links(pdf, output, policy, timeout):
        output.write_bytes(pdf_bytes(text="The worker discarded the source content"))
        return {"changed_images": 1, "lossy_applied": True}

    monkeypatch.setattr(compression, "compress_images", lost_links)
    output, report = compression.compress_pdf(source, config)
    assert report["backend"] in {"qpdf", "pypdf"}
    assert report["lossy_applied"] is False
    assert "CompressionValidationError" in report["fallback_reason"]
    assert report["validation"]["annotations_preserved"]
    assert source.read_bytes() == original and output.is_file()


def test_transient_image_failure_does_not_permanently_cache_lossless_fallback(
    setup, monkeypatch,
):
    config, source, calls, _ = setup
    image_worker = compression.compress_images
    failures = []

    def timeout_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise subprocess.TimeoutExpired("worker", 1)
        return image_worker(*args, **kwargs)

    monkeypatch.setattr(compression, "compress_images", timeout_once)
    _, fallback = compression.compress_pdf(source, config)
    assert fallback["backend"] in {"qpdf", "pypdf"}
    assert "timed out" in fallback["fallback_reason"]
    _, recovered = compression.compress_pdf(source, config)
    assert recovered["backend"] == "pymupdf" and not recovered["cached"]
    assert len(calls) == 1


def test_lossless_profile_never_invokes_lossy_backend(setup, monkeypatch):
    config, source, _, _ = setup
    monkeypatch.setattr(
        compression, "backend_info", lambda: pytest.fail("Lossless means no images"),
    )
    _, report = compression.compress_pdf(
        source, replace(config, pdf_compression_profile="lossless"),
    )
    assert report["requested_profile"] == "lossless"
    assert report["backend"] in {"qpdf", "pypdf"} and report["lossy_applied"] is False


def test_unchanged_result_metadata_still_names_profile_and_fallback(setup, monkeypatch):
    config, source, _, info = setup
    info["eligible"] = False
    info["eligibility_note"] = "untested native engine"
    monkeypatch.setattr(compression.shutil, "which", lambda name: None)
    _, report = compression.compress_pdf(source, config)
    assert report["backend"] == "pypdf"
    assert report["fallback_reason"] == "untested native engine"
    assert report["requested_profile"] == "balanced"
    assert report["lossy_applied"] is False
    assert json.dumps(report)
