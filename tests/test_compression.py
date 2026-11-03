import random
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from papr import compression
from papr.config import Config


def make_pdf(path, *, pages=1, padding=0, annotation=False, signature=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=144, height=144)
        stream = DecodedStreamObject()
        stream.set_data(b"q Q\n" * (padding + 1))
        page[NameObject("/Contents")] = writer._add_object(stream)
        if annotation:
            note = DictionaryObject({NameObject("/Type"): NameObject("/Annot")})
            page[NameObject("/Annots")] = ArrayObject([writer._add_object(note)])
    if signature:
        field = DictionaryObject({NameObject("/FT"): NameObject("/Sig")})
        form = DictionaryObject({NameObject("/Fields"): ArrayObject([writer._add_object(field)])})
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(form)
    writer.write(path)
    return path


@pytest.fixture(autouse=True)
def external_backends(monkeypatch):
    monkeypatch.setattr(compression, "backend_info", lambda: None)
    monkeypatch.setattr(compression, "tool_version", lambda path: "test-version")


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config()


@pytest.fixture
def mocked_qpdf(monkeypatch):
    actual_run = subprocess.run
    monkeypatch.setattr(
        compression.shutil, "which", lambda name: "/mock/qpdf" if name == "qpdf" else None,
    )

    def no_lossless(*args):
        raise RuntimeError("lossless backend failed")

    monkeypatch.setattr(compression, "_lossless", no_lossless)

    def setup(action):
        calls = []

        def run(command, **kwargs):
            if command[-1] == "papr.compression_native_check":
                return actual_run(command, **kwargs)
            calls.append((command, kwargs))
            path = command[-1]
            action(path)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(compression.subprocess, "run", run)
        return calls

    return setup


def test_smaller_valid_copy_is_cached_and_original_untouched(tmp_path, config, mocked_qpdf):
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    original = source.read_bytes()
    smaller = make_pdf(tmp_path / "smaller.pdf")
    calls = mocked_qpdf(lambda output: shutil.copy2(smaller, output))
    output, report = compression.compress_pdf(source, config)
    assert output != source
    assert output.read_bytes() == smaller.read_bytes()
    assert source.read_bytes() == original
    assert report["status"] == "compressed"
    assert report["saved_bytes"] == len(original) - smaller.stat().st_size
    assert report["backend"] == "qpdf"
    assert calls[0][1]["timeout"] == 120
    assert "--recompress-flate" in calls[0][0]
    assert "--compression-level=9" in calls[0][0]
    cached, second = compression.compress_pdf(source, config)
    assert cached == output and second["status"] == "cached"
    assert len(calls) == 1
    assert not list((config.cache_dir / "compression").glob("*.tmp*"))


@pytest.mark.parametrize("result", ["invalid", "larger", "page_count"])
def test_bad_results_keep_original(tmp_path, config, mocked_qpdf, result):
    source = make_pdf(tmp_path / "source.pdf")
    original = source.read_bytes()

    def action(output):
        if result == "invalid":
            from pathlib import Path
            Path(output).write_bytes(b"not a PDF")
        else:
            make_pdf(output, pages=2 if result == "page_count" else 1, padding=100)

    mocked_qpdf(action)
    output, report = compression.compress_pdf(source, config)
    assert output == source and source.read_bytes() == original
    assert report["status"] == ("unchanged" if result == "larger" else "failed")
    assert report["output_bytes"] == len(original)
    assert not list((config.cache_dir / "compression").glob("*.pdf"))


@pytest.mark.parametrize("error", ["missing", "failure", "timeout"])
def test_subprocess_errors_are_best_effort(tmp_path, config, mocked_qpdf, error, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", padding=100)
    original = source.read_bytes()
    mocked_qpdf(lambda output: None)

    def fail(command, **kwargs):
        if error == "missing":
            raise FileNotFoundError("qpdf disappeared")
        if error == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(compression.subprocess, "run", fail)
    output, report = compression.compress_pdf(source, config)
    assert output == source and source.read_bytes() == original
    assert report["status"] == "failed"
    assert not list((config.cache_dir / "compression").glob("*.tmp*"))


def test_missing_external_tools_use_lossless_fallback(tmp_path, config, monkeypatch):
    monkeypatch.setattr(compression.shutil, "which", lambda name: None)
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    original = source.read_bytes()
    output, report = compression.compress_pdf(source, config)
    assert output != source and output.stat().st_size < source.stat().st_size
    assert source.read_bytes() == original
    assert report["backend"] == "pypdf"
    assert len(PdfReader(output).pages) == 1


def test_annotations_use_lossless_fallback(tmp_path, config, monkeypatch):
    monkeypatch.setattr(compression.shutil, "which", lambda name: None)
    source = make_pdf(tmp_path / "source.pdf", padding=3000, annotation=True)
    output, report = compression.compress_pdf(source, config)
    assert report["backend"] == "pypdf"
    assert PdfReader(output).pages[0]["/Annots"][0].get_object()["/Type"] == "/Annot"


def test_signed_pdf_is_never_rewritten(tmp_path, config, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", padding=3000, signature=True)
    original = source.read_bytes()

    def unexpected(*args):
        raise AssertionError("Signed PDFs must not be compressed")

    monkeypatch.setattr(compression.subprocess, "run", unexpected)
    output, report = compression.compress_pdf(source, config)
    assert output == source and source.read_bytes() == original
    assert report["status"] == "skipped" and "signature" in report["reason"]


def test_invalid_input_and_disabled_are_left_alone(tmp_path, config):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"invalid")
    assert compression.compress_pdf(source, config)[1]["status"] == "skipped"
    assert compression.compress_pdf(source, config, enabled=False)[1]["status"] == "disabled"
    assert source.read_bytes() == b"invalid"


def test_real_compression_shrinks_pdf_and_preserves_content(tmp_path, config):
    source = make_pdf(tmp_path / "source.pdf", padding=20000)
    original = source.read_bytes()
    output, report = compression.compress_pdf(source, config)
    assert report["backend"] in {"qpdf", "pypdf"}
    assert report["validation"]["graphics_preserved"] is True
    assert output.stat().st_size < source.stat().st_size
    assert len(PdfReader(output).pages) == len(PdfReader(source).pages)
    assert source.read_bytes() == original


def test_corrupt_cached_copy_is_rebuilt(tmp_path, config, mocked_qpdf):
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    smaller = make_pdf(tmp_path / "smaller.pdf")
    calls = mocked_qpdf(lambda output: shutil.copy2(smaller, output))
    output, _ = compression.compress_pdf(source, config)
    output.write_bytes(b"corrupt")
    repaired, report = compression.compress_pdf(source, config)
    assert repaired == output and repaired.read_bytes() == smaller.read_bytes()
    assert report["status"] == "compressed" and len(calls) == 2



def test_optimized_content_copied_out_of_cache_does_not_recompress(tmp_path, config, mocked_qpdf):
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    smaller = make_pdf(tmp_path / "smaller.pdf")
    calls = mocked_qpdf(lambda output: shutil.copy2(smaller, output))
    output, _ = compression.compress_pdf(source, config)
    copy = tmp_path / "exported.pdf"
    shutil.copy2(output, copy)
    reused, report = compression.compress_pdf(copy, config)
    assert reused == copy and report["cached"]
    assert report["reason"] == "already optimized with these settings"
    assert len(calls) == 1


def test_missing_image_backend_never_uses_installed_ghostscript(tmp_path, config, monkeypatch):
    """Missing optional dependencies must not bypass image-selection safeguards."""
    monkeypatch.setattr(
        compression.shutil, "which", lambda name: "/mock/gs" if name == "gs" else None,
    )
    padded = make_pdf(tmp_path / "padded.pdf", padding=3000)
    writer = PdfWriter(clone_from=padded)
    page = writer.pages[0]
    image = DecodedStreamObject()
    image.set_data(random.Random(0).randbytes(600 * 600 * 3))
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(600),
        NameObject("/Height"): NumberObject(600),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Scan"): writer._add_object(image)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"q 144 0 0 144 0 0 cm /Scan Do Q\n" + page.get_contents().get_data())
    page[NameObject("/Contents")] = writer._add_object(stream)
    source = tmp_path / "full-page-scan.pdf"
    writer.write(source)
    original = source.read_bytes()
    actual_run = subprocess.run

    def run(command, **kwargs):
        assert command[0] != "/mock/gs"
        return actual_run(command, **kwargs)

    monkeypatch.setattr(compression.subprocess, "run", run)
    output, report = compression.compress_pdf(source, config)
    assert report["backend"] == "pypdf" and report["lossy_applied"] is False
    assert "lossless fallback" in report["fallback_reason"]
    optimized_image = PdfReader(output).pages[0]["/Resources"]["/XObject"]["/Scan"]
    assert optimized_image["/Width"] == optimized_image["/Height"] == 600
    assert optimized_image.get_data() == image.get_data()
    assert output.stat().st_size < source.stat().st_size
    assert source.read_bytes() == original
