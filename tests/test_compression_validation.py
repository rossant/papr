import pytest
from compression_fixtures import document, rewrite
from pypdf import PdfWriter
from pypdf.generic import NameObject, NumberObject

from papr.compression_validation import (
    VALIDATION_VERSION,
    CompressionValidationError,
    validate_compression,
)


def test_lossless_rewrite_preserves_every_feature(tmp_path):
    source = document(tmp_path / "source.pdf")
    target = rewrite(source, tmp_path / "target.pdf")
    result = validate_compression(source, target)
    assert result["validation_version"] == VALIDATION_VERSION
    assert result["pages"] == 2
    assert result["images_checked"] == 2
    assert all(
        result[k]
        for k in (
            "text_preserved",
            "links_preserved",
            "annotations_preserved",
            "forms_preserved",
            "bookmarks_preserved",
            "graphics_preserved",
        )
    )


@pytest.mark.parametrize(
    "loss",
    [
        "text",
        "links",
        "highlight",
        "diagram",
        "images",
        "geometry",
        "rotation",
        "form",
        "bookmarks",
    ],
)
def test_rejects_loss_with_unchanged_page_count(tmp_path, loss):
    source = document(tmp_path / "source.pdf")
    candidate = document(tmp_path / "candidate.pdf", loss=loss)
    with pytest.raises(CompressionValidationError):
        validate_compression(source, candidate)


@pytest.mark.parametrize(
    ("components", "space"),
    [(1, "/DeviceRGB"), (4, "/DeviceRGB"), (3, "/DeviceGray"), (3, "/DeviceCMYK")],
)
def test_rejects_jpeg_pdf_colorspace_mismatch(tmp_path, components, space):
    source = document(tmp_path / "source.pdf")
    candidate = document(tmp_path / "candidate.pdf", jpeg_components=components, jpeg_space=space)
    with pytest.raises(CompressionValidationError, match="components disagree"):
        validate_compression(source, candidate)


def test_image_downsampling_can_change_dimensions_and_pixels(tmp_path):
    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    image = writer.pages[0]["/Resources"]["/XObject"]["/RGB"]
    image[NameObject("/Width")] = NumberObject(1)
    image[NameObject("/Height")] = NumberObject(1)
    image.set_data(bytes([230, 10, 0]))
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    assert validate_compression(source, target)["graphics_preserved"]


def test_rejects_encrypted_candidate(tmp_path):
    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    writer.encrypt("password")
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    with pytest.raises(CompressionValidationError, match="[Ee]ncrypted"):
        validate_compression(source, target)


def test_rejects_invalid_candidate(tmp_path):
    source = document(tmp_path / "source.pdf")
    target = tmp_path / "candidate.pdf"
    target.write_bytes(b"not a pdf")
    with pytest.raises(CompressionValidationError):
        validate_compression(source, target)


@pytest.mark.parametrize("property_name", ["/QuadPoints", "/C", "/F", "/Contents"])
def test_preserves_highlight_properties(tmp_path, property_name):
    from pypdf.generic import ArrayObject, TextStringObject

    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    highlight = writer.pages[0]["/Annots"][2].get_object()
    if property_name == "/F":
        value = NumberObject(2)
    elif property_name == "/Contents":
        value = TextStringObject("changed note")
    else:
        value = ArrayObject([NumberObject(0)])
    highlight[NameObject(property_name)] = value
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    with pytest.raises(CompressionValidationError, match="annotations"):
        validate_compression(source, target)


def test_rejects_named_destination_change(tmp_path):
    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    writer._root_object["/Names"]["/Dests"]["/Names"][1].get_object()["/D"][0] = writer.pages[
        0
    ].indirect_reference
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    with pytest.raises(CompressionValidationError, match="named destinations"):
        validate_compression(source, target)


def test_preserves_widget_default_and_flags(tmp_path):
    from pypdf.generic import TextStringObject

    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    field = writer._root_object["/AcroForm"]["/Fields"][0].get_object()
    field[NameObject("/DV")] = TextStringObject("new default")
    field[NameObject("/Ff")] = NumberObject(1)
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    with pytest.raises(CompressionValidationError):
        validate_compression(source, target)


def test_poppler_renders_lossless_fixture_identically(tmp_path):
    """Release opt-in exercises an independent renderer without a runtime dependency."""
    import os
    import shutil
    import subprocess

    if os.environ.get("PAPR_RELEASE_VISUALS") != "1":
        pytest.skip("set PAPR_RELEASE_VISUALS=1 for independent Poppler rendering")
    executable = shutil.which("pdftoppm")
    if not executable:
        pytest.skip("Poppler is not installed")
    source = document(tmp_path / "source.pdf")
    target = rewrite(source, tmp_path / "candidate.pdf")
    for pdf, prefix in ((source, "before"), (target, "after")):
        subprocess.run(
            [executable, "-r", "72", "-f", "1", "-singlefile", str(pdf), str(tmp_path / prefix)],
            check=True,
            capture_output=True,
            timeout=30,
        )
    assert (tmp_path / "before.ppm").read_bytes() == (tmp_path / "after.ppm").read_bytes()


def test_rejects_candidate_missing_eof(tmp_path):
    source = document(tmp_path / "source.pdf")
    target = tmp_path / "candidate.pdf"
    target.write_bytes(source.read_bytes().replace(b"%%EOF", b""))
    with pytest.raises(CompressionValidationError, match="end-of-file"):
        validate_compression(source, target)


def test_rejects_removed_image_sample_data(tmp_path):
    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    writer.pages[0]["/Resources"]["/XObject"]["/RGB"].set_data(b"")
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    with pytest.raises(CompressionValidationError, match="sample data"):
        validate_compression(source, target)


def test_rejects_truncated_jpeg_even_with_valid_frame_header(tmp_path):
    source = document(tmp_path / "source.pdf")
    target = document(tmp_path / "candidate.pdf", jpeg_components=3)
    with pytest.raises(CompressionValidationError, match="image|Image"):
        validate_compression(source, target)


def test_nested_empty_graphics_states_are_semantic_noops(tmp_path):
    from pypdf.generic import DecodedStreamObject

    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(b"q q Q Q\n" + page.get_contents().get_data() + b"\nq Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    assert validate_compression(source, target)["graphics_preserved"]


def test_changed_real_jpeg_decodes_and_preserves_pdf_model(tmp_path):
    from io import BytesIO

    image_module = pytest.importorskip("PIL.Image")
    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    image = writer.pages[0]["/Resources"]["/XObject"]["/RGB"]
    data = BytesIO()
    image_module.new("RGB", (1, 1), (230, 10, 0)).save(data, format="JPEG")
    image.set_data(data.getvalue())
    image[NameObject("/Filter")] = NameObject("/DCTDecode")
    image[NameObject("/Width")] = NumberObject(1)
    image[NameObject("/Height")] = NumberObject(1)
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    assert validate_compression(source, target)["graphics_preserved"]


def test_rejects_jpeg_dimensions_that_disagree_with_pdf(tmp_path):
    from io import BytesIO

    image_module = pytest.importorskip("PIL.Image")
    source = document(tmp_path / "source.pdf")
    writer = PdfWriter(clone_from=source)
    image = writer.pages[0]["/Resources"]["/XObject"]["/RGB"]
    data = BytesIO()
    image_module.new("RGB", (1, 1), (230, 10, 0)).save(data, format="JPEG")
    image.set_data(data.getvalue())
    image[NameObject("/Filter")] = NameObject("/DCTDecode")
    target = tmp_path / "candidate.pdf"
    writer.write(target)
    with pytest.raises(CompressionValidationError, match="dimensions"):
        validate_compression(source, target)
