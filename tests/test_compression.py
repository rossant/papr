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


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "cache_dir", property(lambda self: tmp_path / "cache"))
    return Config()


@pytest.fixture
def mocked_gs(monkeypatch):
    monkeypatch.setattr(
        compression.shutil, "which", lambda name: "/mock/gs" if name == "gs" else None,
    )

    def no_lossless(*args):
        raise RuntimeError("lossless backend failed")

    monkeypatch.setattr(compression, "_lossless", no_lossless)

    def setup(action):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            path = next(
                item.split("=", 1)[1] for item in command if item.startswith("-sOutputFile=")
            )
            action(path)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(compression.subprocess, "run", run)
        return calls

    return setup


def test_smaller_valid_copy_is_cached_and_original_untouched(tmp_path, config, mocked_gs):
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    original = source.read_bytes()
    smaller = make_pdf(tmp_path / "smaller.pdf")
    calls = mocked_gs(lambda output: shutil.copy2(smaller, output))
    output, report = compression.compress_pdf(source, config)
    assert output != source
    assert output.read_bytes() == smaller.read_bytes()
    assert source.read_bytes() == original
    assert report["status"] == "compressed"
    assert report["saved_bytes"] == len(original) - smaller.stat().st_size
    assert report["backend"] == "ghostscript"
    assert calls[0][1]["timeout"] == 120
    assert "-dColorImageResolution=200" in calls[0][0]
    assert "-dMonoImageResolution=600" in calls[0][0]
    cached, second = compression.compress_pdf(source, config)
    assert cached == output and second["status"] == "cached"
    assert len(calls) == 1
    assert not list((config.cache_dir / "compression").glob("*.tmp*"))


@pytest.mark.parametrize("result", ["invalid", "larger", "page_count"])
def test_bad_results_keep_original(tmp_path, config, mocked_gs, result):
    source = make_pdf(tmp_path / "source.pdf")
    original = source.read_bytes()

    def action(output):
        if result == "invalid":
            from pathlib import Path
            Path(output).write_bytes(b"not a PDF")
        else:
            make_pdf(output, pages=2 if result == "page_count" else 1, padding=100)

    mocked_gs(action)
    output, report = compression.compress_pdf(source, config)
    assert output == source and source.read_bytes() == original
    assert report["status"] == ("unchanged" if result == "larger" else "failed")
    assert report["output_bytes"] == len(original)
    assert not list((config.cache_dir / "compression").glob("*.pdf"))


@pytest.mark.parametrize("error", ["missing", "failure", "timeout"])
def test_subprocess_errors_are_best_effort(tmp_path, config, mocked_gs, error, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", padding=100)
    original = source.read_bytes()
    mocked_gs(lambda output: None)

    def fail(command, **kwargs):
        if error == "missing":
            raise FileNotFoundError("gs disappeared")
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


def test_real_ghostscript_shrinks_pdf(tmp_path, config):
    if shutil.which("gs") is None:
        pytest.skip("Ghostscript is not installed")
    source = make_pdf(tmp_path / "source.pdf", padding=20000)
    original = source.read_bytes()
    output, report = compression.compress_pdf(source, config)
    assert report["backend"] == "ghostscript"
    assert output.stat().st_size < source.stat().st_size
    assert len(PdfReader(output).pages) == len(PdfReader(source).pages)
    assert source.read_bytes() == original


@pytest.mark.parametrize("pixels", [144, 600])
def test_real_ghostscript_preserves_text_and_only_downsamples_high_dpi(tmp_path, config, pixels):
    if shutil.which("gs") is None:
        pytest.skip("Ghostscript is not installed")
    source = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=144, height=144)
    image = DecodedStreamObject()
    image.set_data(random.Random(0).randbytes(pixels * pixels * 3))
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(pixels),
        NameObject("/Height"): NumberObject(pixels),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)}),
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(
        b"q 72 0 0 72 0 0 cm /Im0 Do Q\n"
        b"BT /F1 10 Tf 10 100 Td (Searchable text) Tj ET\n"
        b"0 0 m 50 50 l S\n"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(source)
    original = source.read_bytes()
    output = tmp_path / "gs-output.pdf"
    result = subprocess.run(
        compression._command("ghostscript", shutil.which("gs"), source, output, 200),
        capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0
    optimized = PdfReader(output).pages[0]
    assert "Searchable text" in optimized.extract_text()
    images = optimized["/Resources"]["/XObject"]
    width = next(obj.get_object()["/Width"] for obj in images.values())
    if pixels < 200:
        assert width == pixels
    else:
        assert width < pixels
    assert source.read_bytes() == original


def test_corrupt_cached_copy_is_rebuilt(tmp_path, config, mocked_gs):
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    smaller = make_pdf(tmp_path / "smaller.pdf")
    calls = mocked_gs(lambda output: shutil.copy2(smaller, output))
    output, _ = compression.compress_pdf(source, config)
    output.write_bytes(b"corrupt")
    repaired, report = compression.compress_pdf(source, config)
    assert repaired == output and repaired.read_bytes() == smaller.read_bytes()
    assert report["status"] == "compressed" and len(calls) == 2



def test_optimized_content_copied_out_of_cache_does_not_recompress(tmp_path, config, mocked_gs):
    source = make_pdf(tmp_path / "source.pdf", padding=3000)
    smaller = make_pdf(tmp_path / "smaller.pdf")
    calls = mocked_gs(lambda output: shutil.copy2(smaller, output))
    output, _ = compression.compress_pdf(source, config)
    copy = tmp_path / "exported.pdf"
    shutil.copy2(output, copy)
    reused, report = compression.compress_pdf(copy, config)
    assert reused == copy and report["cached"]
    assert report["reason"] == "already optimized with these settings"
    assert len(calls) == 1
