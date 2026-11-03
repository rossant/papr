"""Native image tests are optional; base installations still test the process boundary."""
from __future__ import annotations

import io
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from papr import compression_images
from papr.compression_worker import _effective_dpi

POLICY = {
    "profile": "balanced", "dpi": 180, "quality": 80,
    "downsample_threshold": 1.4, "min_savings_bytes": 4096, "min_savings_ratio": 0.01,
}


@pytest.fixture
def native():
    pdf = pytest.importorskip("pymupdf")
    image = pytest.importorskip("PIL.Image")
    info = compression_images.backend_info()
    if not info or not info["eligible"]:
        pytest.skip("requires the tested optional image engine")
    return pdf, image


def _photo(Image, mode="RGB"):
    gray = Image.effect_noise((900, 900), 55)
    if mode == "L":
        return gray
    return Image.merge("RGB", (gray, gray.transpose(Image.Transpose.ROTATE_90),
                               gray.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))


def _stream(image, format="PNG"):
    data = io.BytesIO()
    image.save(data, format=format)
    return data.getvalue()


def _document(native, path, image, *, rect=(30, 50, 174, 194), format="PNG"):
    pdf, _ = native
    doc = pdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((30, 30), "Text stays searchable")
    page.draw_line((20, 300), (400, 300))
    xref = page.insert_image(pdf.Rect(rect), stream=_stream(image, format))
    doc.save(path)
    doc.close()
    return xref


def _compress(source):
    output = source.with_name(source.stem + "-compressed.pdf")
    result = compression_images.compress_images(source, output, POLICY, 30)
    return output, result


def test_worker_protocol_and_process_failure(monkeypatch, tmp_path):
    def run(command, **kwargs):
        assert command[-2:] == ["-m", "papr.compression_worker"]
        request = json.loads(kwargs["input"])
        assert Path(request["source"]).is_absolute()
        assert kwargs["timeout"] == 3
        return subprocess.CompletedProcess(command, -11, "", "native crash")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="status -11"):
        compression_images.compress_images(tmp_path / "in.pdf", tmp_path / "out.pdf", POLICY, 3)


def test_worker_timeout_and_absent_backend(monkeypatch, tmp_path):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", run)
    assert compression_images.backend_info() is None
    with pytest.raises(subprocess.TimeoutExpired):
        compression_images.compress_images(tmp_path / "in.pdf", tmp_path / "out.pdf", POLICY, 1)


def test_source_cannot_be_output(tmp_path):
    with pytest.raises(ValueError, match="differ"):
        compression_images.compress_images(tmp_path / "a.pdf", tmp_path / "a.pdf", POLICY, 1)


def test_output_hard_link_cannot_replace_source(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "alias.pdf"
    source.write_bytes(b"original")
    output.hardlink_to(source)
    with pytest.raises(ValueError, match="differ"):
        compression_images.compress_images(source, output, POLICY, 1)
    assert source.read_bytes() == b"original"


def test_effective_dpi_rotation_and_shear():
    assert _effective_dpi(600, 600, [(0, 144, -144, 0, 0)]) == 300
    sheared = _effective_dpi(600, 600, [(144, 0, 144, 144, 0)])
    assert sheared == pytest.approx(300 / ((1 + math.sqrt(5)) / 2))


@pytest.mark.parametrize("mode", ["RGB", "L"])
@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_photo_stream_components_and_independent_render(native, tmp_path, mode, format):
    pdf, Image = native
    source = tmp_path / f"{mode}.pdf"
    xref = _document(native, source, _photo(Image, mode), format=format)
    original = source.read_bytes()
    output, result = _compress(source)
    assert result["changed_images"] == 1
    assert result["lossy_applied"] is True
    assert source.read_bytes() == original
    with pdf.open(source) as before, pdf.open(output) as after:
        assert after[0].get_text() == before[0].get_text()
        assert after[0].get_drawings() == before[0].get_drawings()
        assert after.xref_get_key(xref, "ColorSpace") == before.xref_get_key(xref, "ColorSpace")
        jpeg = Image.open(io.BytesIO(after.xref_stream_raw(xref)))
        assert jpeg.mode == mode
        assert jpeg.width == pytest.approx(360, abs=1)
        assert len(after[0].get_image_info()) == len(before[0].get_image_info())
    if shutil.which("pdftoppm") is None:
        pytest.skip("Poppler is needed for independent rendering")
    renders = []
    for path in (source, output):
        dest = tmp_path / (path.stem + "-render")
        subprocess.run(["pdftoppm", "-r", "45", "-singlefile", "-png", str(path), str(dest)],
                       check=True, capture_output=True, timeout=15)
        with Image.open(str(dest) + ".png") as im:
            renders.append(im.convert("RGB").copy())
    # An RGB JPEG falsely declared Gray produces the severe darkness in #5164.
    histograms = [im.crop((20, 34, 105, 118)).convert("L").histogram() for im in renders]
    means = [sum(i * n for i, n in enumerate(histogram)) / (85 * 84)
             for histogram in histograms]
    assert abs(means[0] - means[1]) < 8


@pytest.mark.parametrize("kind", ["cmyk", "alpha", "bitonal", "lineart", "scan", "decode"])
def test_sensitive_images_preserved(native, tmp_path, kind):
    pdf, Image = native
    image = _photo(Image)
    rect = (30, 50, 174, 194)
    format = "PNG"
    if kind == "cmyk":
        image, format = image.convert("CMYK"), "JPEG"
    elif kind == "alpha":
        image = image.convert("RGBA")
        image.putalpha(128)
    elif kind == "bitonal":
        image = image.convert("1")
    elif kind == "lineart":
        image = Image.new("RGB", (900, 900), "white")
        for x in range(0, 900, 30):
            image.paste("black", (x, 0, x + 1, 900))
    elif kind == "scan":
        image = image.resize((900, 1200))
        rect = (0, 0, 600, 800)
    source = tmp_path / f"{kind}.pdf"
    xref = _document(native, source, image, rect=rect, format=format)
    if kind == "decode":
        with pdf.open(source) as doc:
            doc.xref_set_key(xref, "Decode", "[1 0 1 0 1 0]")
            doc.save(source.with_suffix(".changed.pdf"))
        source.with_suffix(".changed.pdf").replace(source)
    original = source.read_bytes()
    output, result = _compress(source)
    assert result["changed_images"] == 0
    assert result["lossy_applied"] is False
    assert output.read_bytes() == original


def test_largest_shared_image_placement_prevents_downsample(native, tmp_path):
    pdf, Image = native
    source = tmp_path / "shared.pdf"
    xref = _document(native, source, _photo(Image))
    with pdf.open(source) as doc:
        doc.new_page(width=600, height=800).insert_image(pdf.Rect(20, 20, 420, 420), xref=xref)
        doc.save(tmp_path / "placed.pdf")
    _, result = _compress(tmp_path / "placed.pdf")
    assert result["changed_images"] == 0
    assert result["reasons"]["resolution below downsample threshold"] == 1


def test_nested_form_and_userunit(native, tmp_path):
    pdf, Image = native
    inner = tmp_path / "inner.pdf"
    _document(native, inner, _photo(Image))
    with pdf.open(inner) as source:
        doc = pdf.open()
        page = doc.new_page(width=600, height=800)
        page.show_pdf_page(page.rect, source, 0)
        doc.xref_set_key(page.xref, "UserUnit", "2")
        doc.save(tmp_path / "nested.pdf")
        doc.close()
    output, result = _compress(tmp_path / "nested.pdf")
    # 900 pixels / (144pt * UserUnit2) = 225 dpi, below the 252 dpi threshold.
    assert result["changed_images"] == 0
    assert result["reasons"]["resolution below downsample threshold"] == 1
    assert output.read_bytes() == (tmp_path / "nested.pdf").read_bytes()


def test_eligible_photo_does_not_change_cmyk_alpha_or_bitonal(native, tmp_path):
    pdf, Image = native
    source = tmp_path / "mixed.pdf"
    doc = pdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(pdf.Rect(20, 20, 164, 164), stream=_stream(_photo(Image)))
    alpha = _photo(Image).convert("RGBA")
    alpha.putalpha(100)
    sensitive = [(_photo(Image).convert("CMYK"), "JPEG"),
                 (alpha, "PNG"), (_photo(Image).convert("1"), "PNG")]
    protected = []
    for index, (image, format) in enumerate(sensitive):
        rect = pdf.Rect(200, 20 + index * 180, 344, 164 + index * 180)
        protected.append(page.insert_image(rect, stream=_stream(image, format)))
    doc.save(source)
    doc.close()
    output, result = _compress(source)
    assert result["changed_images"] == 1
    with pdf.open(source) as before, pdf.open(output) as after:
        assert len(after[0].get_image_info()) == 4
        for xref in protected:
            assert before.xref_stream(xref) == after.xref_stream(xref)
            for key in ("Width", "Height", "ColorSpace", "SMask", "Mask", "BitsPerComponent"):
                assert before.xref_get_key(xref, key) == after.xref_get_key(xref, key)
        # Both the alpha stream and its base image must survive saving a changed PDF.
        mask = before.xref_get_key(protected[1], "SMask")[1]
        mask_xref = int(mask.split()[0])
        assert before.xref_stream(mask_xref) == after.xref_stream(mask_xref)
    if shutil.which("pdftoppm") is None:
        pytest.skip("Poppler is needed for independent CMYK/alpha rendering")
    renders = []
    for path in (source, output):
        dest = tmp_path / (path.stem + "-mixed-render")
        subprocess.run(["pdftoppm", "-r", "36", "-singlefile", "-png", str(path), str(dest)],
                       check=True, capture_output=True, timeout=15)
        with Image.open(str(dest) + ".png") as image:
            renders.append(image.convert("RGB").copy())
    for top in (15, 105, 195):
        rect = (105, top, 165, top + 60)
        assert renders[0].crop(rect).tobytes() == renders[1].crop(rect).tobytes()
