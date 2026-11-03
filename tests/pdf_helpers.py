"""Real, deterministic PDFs for acquisition tests that cross validation boundaries."""

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf_bytes(label="Paper", *, text="", pages=1, metadata=None):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=200, height=200)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
            })
            content = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.set_data(f"BT /F1 12 Tf 10 100 Td ({escaped}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_metadata({"/Title": label, **(metadata or {})})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
