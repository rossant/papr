"""Small generated documents exercising preservation, with no external PDF assets."""

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def numbers(*values):
    return ArrayObject([FloatObject(v) for v in values])


def jpeg_header(components):
    """Minimal SOF codestream for color-space validation (not a renderable image)."""
    payload = bytes([8, 0, 2, 0, 2, components]) + b"".join(
        bytes([i + 1, 0x11, 0]) for i in range(components)
    )
    return b"\xff\xd8\xff\xc0" + (len(payload) + 2).to_bytes(2, "big") + payload + b"\xff\xd9"


def document(path: Path, *, loss=None, jpeg_components=None, jpeg_space="/DeviceRGB"):
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    writer.add_blank_page(300, 300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
            NameObject("/XObject"): DictionaryObject(),
        }
    )
    page[NameObject("/Resources")] = resources
    for name, space, data in (
        ("RGB", "/DeviceRGB", bytes([255, 0, 0] * 4)),
        ("Gray", "/DeviceGray", bytes([0, 70, 140, 255])),
    ):
        image = DecodedStreamObject()
        image.set_data(data)
        image.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(2),
                NameObject("/Height"): NumberObject(2),
                NameObject("/BitsPerComponent"): NumberObject(8),
                NameObject("/ColorSpace"): NameObject(space),
            }
        )
        if name == "RGB" and jpeg_components:
            image.set_data(jpeg_header(jpeg_components))
            image[NameObject("/Filter")] = NameObject("/DCTDecode")
            image[NameObject("/ColorSpace")] = NameObject(jpeg_space)
        resources["/XObject"][NameObject("/" + name)] = writer._add_object(image)
    text = "Changed text" if loss == "text" else "Searchable chart"
    operations = f"BT /F1 12 Tf 20 260 Td ({text}) Tj ET\n"
    if loss != "diagram":
        operations += "0 0 1 RG 1 w 20 40 m 20 180 l 220 180 l S\n"
        operations += "1 0 0 rg 30 50 30 90 re f 80 50 30 120 re f\n"
    if loss != "images":
        operations += "q 50 0 0 50 20 190 cm /RGB Do Q\n"
        operations += "q 50 0 0 50 90 190 cm /Gray Do Q\n"
    stream = DecodedStreamObject()
    stream.set_data(operations.encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    if loss == "geometry":
        page.cropbox.lower_left = (10, 10)
    if loss == "rotation":
        page.rotate(90)
    annotations = ArrayObject()
    if loss != "links":
        for action in (
            DictionaryObject(
                {
                    NameObject("/A"): DictionaryObject(
                        {
                            NameObject("/S"): NameObject("/URI"),
                            NameObject("/URI"): TextStringObject("https://example.org/paper"),
                        }
                    )
                }
            ),
            DictionaryObject(
                {
                    NameObject("/Dest"): ArrayObject(
                        [writer.pages[1].indirect_reference, NameObject("/Fit")]
                    )
                }
            ),
        ):
            action.update(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Link"),
                    NameObject("/Rect"): numbers(10, 10, 100, 30),
                }
            )
            annotations.append(writer._add_object(action))
    if loss != "highlight":
        highlight = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Highlight"),
                NameObject("/Rect"): numbers(20, 250, 150, 270),
                NameObject("/QuadPoints"): numbers(20, 270, 150, 270, 20, 250, 150, 250),
                NameObject("/C"): numbers(1, 1, 0),
                NameObject("/F"): NumberObject(4),
                NameObject("/Contents"): TextStringObject("Check this figure"),
            }
        )
        annotations.append(writer._add_object(highlight))
    field = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("reviewer"),
            NameObject("/V"): TextStringObject("Altered" if loss == "form" else "Alice"),
            NameObject("/DV"): TextStringObject("Anonymous"),
            NameObject("/Ff"): NumberObject(0),
            NameObject("/Rect"): numbers(180, 10, 280, 30),
        }
    )
    field_ref = writer._add_object(field)
    annotations.append(field_ref)
    page[NameObject("/Annots")] = annotations
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(
        DictionaryObject({NameObject("/Fields"): ArrayObject([field_ref])})
    )
    if loss != "bookmarks":
        writer.add_outline_item("Results", 1)
    writer.add_named_destination("results", 1)
    writer.write(path)
    return path


def rewrite(source: Path, target: Path):
    writer = PdfWriter(clone_from=source)
    for page in writer.pages:
        page.compress_content_streams()
    writer.write(target)
    return target
