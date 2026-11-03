"""Conservative semantic checks for disposable PDF compression candidates.

These checks detect structural loss; they do not establish perceptual or clinical fidelity.
Unknown or unparseable content fails closed so callers can retain the original PDF.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    IndirectObject,
    StreamObject,
)

from .compression_native_check import check_native as _mupdf_check
from .pdf import PdfValidationError, validate_pdf

VALIDATION_VERSION = 1


class CompressionValidationError(ValueError):
    """The candidate cannot be shown to preserve the source document."""


def _same(left, right):
    # Native writers round PDF coordinates differently. A tolerance of 0.0001pt
    # accepts serialization noise while preserving observable geometry.
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(left - right) <= 0.0001
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _resolve(value):
    return value.get_object() if isinstance(value, IndirectObject) else value


def _jpeg_components(data: bytes) -> int:
    """Read bounded JPEG marker headers, without decoding or requiring Pillow."""
    if not data.startswith(b"\xff\xd8"):
        raise CompressionValidationError("Invalid JPEG image stream")
    position = 2
    while position < len(data):
        if data[position] != 255:
            raise CompressionValidationError("Invalid JPEG marker")
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker in (0xD9, 0xDA) or position + 2 > len(data):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if length < 8:
                break
            return data[position + 7]
        position += length
    raise CompressionValidationError("JPEG image has no readable frame header")


def _components(space, resources=None):
    space = _resolve(space)
    basic = {"/DeviceGray": 1, "/G": 1, "/DeviceRGB": 3, "/RGB": 3, "/DeviceCMYK": 4, "/CMYK": 4}
    if isinstance(space, str):
        if space in basic:
            return basic[space]
        colors = _resolve((resources or {}).get("/ColorSpace", {}))
        if space in colors:
            return _components(colors[space])
        raise CompressionValidationError(f"Unknown JPEG color space {space}")
    if isinstance(space, (list, ArrayObject)) and space:
        kind = str(space[0])
        if kind == "/ICCBased":
            return int(_resolve(space[1])["/N"])
        if kind == "/Indexed":
            return 1
        if kind in ("/CalGray", "/Separation"):
            return 1
        if kind in ("/CalRGB", "/Lab"):
            return 3
        if kind == "/DeviceN":
            return len(_resolve(space[1]))
    raise CompressionValidationError("Unreadable JPEG color space")


class _Document:
    def __init__(self, path: Path):
        self.reader = PdfReader(path)
        if self.reader.is_encrypted:
            raise CompressionValidationError("Encrypted PDF cannot be validated")
        self.pages = {
            (page.indirect_reference.idnum, page.indirect_reference.generation): i
            for i, page in enumerate(self.reader.pages)
            if page.indirect_reference
        }
        self.images_checked = 0
        self.images = []

    def canonical(self, value, *, depth=0, seen=frozenset()):
        if depth > 80:
            raise CompressionValidationError("PDF object nesting exceeds validation limit")
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in self.pages:
                return ("page", self.pages[key])
            value = value.get_object()
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                raise CompressionValidationError("Cyclic PDF structure cannot be validated")
            seen = seen | {id(value)}
        if isinstance(value, dict):
            # Parent and page backreferences are identity links, not document semantics.
            result = tuple(
                (str(k), self.canonical(v, depth=depth + 1, seen=seen))
                for k, v in sorted(value.items())
                if k not in ("/P", "/Parent", "/Length", "/Filter", "/DecodeParms")
            )
            if isinstance(value, StreamObject):
                result += (("stream", hashlib.sha256(value.get_data()).hexdigest()),)
            return result
        if isinstance(value, (list, ArrayObject)):
            return tuple(self.canonical(v, depth=depth + 1, seen=seen) for v in value)
        if isinstance(value, (int, float)):
            return round(float(value), 6)
        if isinstance(value, bytes):
            return bytes(value)
        return str(value) if value is not None else None

    def image(self, obj, resources):
        filters = _resolve(obj.get("/Filter", []))
        filters = [filters] if isinstance(filters, str) else list(filters)
        if "/DCTDecode" in filters:
            # pypdf removes preceding lossless filters, leaving the JPEG codestream.
            count = _jpeg_components(obj.get_data())
            if count != _components(obj.get("/ColorSpace"), resources):
                raise CompressionValidationError("JPEG components disagree with PDF color space")
        self.images.append((obj, resources))
        self.images_checked += 1
        if int(obj.get("/Width", 0)) <= 0 or int(obj.get("/Height", 0)) <= 0:
            raise CompressionValidationError("Invalid image dimensions")
        return (
            "image",
            self.canonical(obj.get("/ColorSpace")),
            self.canonical(obj.get("/ImageMask", False)),
            self.canonical(obj.get("/Decode")),
            self.canonical(obj.get("/SMask")),
            self.canonical(obj.get("/Mask")),
        )

    def operations(self, stream, resources, stack=frozenset()):
        if stream is None:
            return ()
        resources = _resolve(resources or {})
        content = ContentStream(stream, self.reader)
        output = []
        resource_ops = {
            b"Tf": "/Font",
            b"gs": "/ExtGState",
            b"CS": "/ColorSpace",
            b"cs": "/ColorSpace",
            b"sh": "/Shading",
        }
        for operands, operator in content.operations:
            if operator == b"Q" and output and output[-1] == (b"q", ()):
                output.pop()
                continue
            if operator == b"INLINE IMAGE":
                # Inline image rewrites cannot be proven lossless by this lightweight path.
                output.append((operator, self.canonical(operands)))
                continue
            operands = list(operands)
            if operator == b"Do":
                obj = _resolve(_resolve(resources.get("/XObject", {}))[operands[0]])
                if obj.get("/Subtype") == "/Image":
                    signature = self.image(obj, resources)
                elif obj.get("/Subtype") == "/Form":
                    if id(obj) in stack:
                        raise CompressionValidationError("Recursive form XObject")
                    signature = (
                        "form",
                        self.canonical(obj.get("/BBox")),
                        self.canonical(obj.get("/Matrix")),
                        self.canonical(obj.get("/Group")),
                        self.operations(obj, obj.get("/Resources", resources), stack | {id(obj)}),
                    )
                else:
                    raise CompressionValidationError("Unknown painted XObject")
                output.append((operator, signature))
                continue
            if operator in resource_ops and operands:
                category = _resolve(resources.get(resource_ops[operator], {}))
                if operands[0] in category:
                    operands[0] = ("resource", self.canonical(category[operands[0]]))
            output.append(
                (
                    operator,
                    tuple(self.canonical(v) if not isinstance(v, tuple) else v for v in operands),
                )
            )
        return tuple(output)

    def page(self, page):
        geometry = tuple(
            self.canonical(value)
            for value in (
                page.mediabox,
                page.cropbox,
                page.get("/Rotate", 0),
                page.get("/UserUnit", 1),
                page.get("/TrimBox"),
                page.get("/BleedBox"),
                page.get("/ArtBox"),
            )
        )
        # Preserve spacing and Unicode, normalizing only platform line endings.
        try:
            text = page.extract_text().replace("\r\n", "\n").replace("\r", "\n")
        except Exception:
            text = None
        resources = _resolve(page.get("/Resources", {}))
        operations = (
            self.operations(page.get("/Contents"), resources),
            self.canonical({k: v for k, v in resources.items() if k != "/XObject"}),
        )
        annotations = tuple(self.canonical(a) for a in _resolve(page.get("/Annots", [])))
        return geometry, text, operations, annotations

    def interactive(self):
        root = self.reader.trailer["/Root"]
        form = _resolve(root.get("/AcroForm", {}))
        # The field tree retains inherited properties, flags, choices and radio states.
        fields = (self.canonical(form), self.canonical(root.get("/OCProperties")))
        outlines = self.canonical(self.reader.outline)
        destinations = self.canonical(self.reader.named_destinations)
        return fields, outlines, destinations


def _validate_changed_images(before: _Document, after: _Document) -> None:
    if len(before.images) != len(after.images):
        raise CompressionValidationError("Painted image count changed")
    for (old, _), (new, resources) in zip(before.images, after.images, strict=True):
        old_data, data = old.get_data(), new.get_data()
        geometry = ("/Width", "/Height", "/BitsPerComponent")
        if old_data == data and all(old.get(k) == new.get(k) for k in geometry):
            continue
        width, height = int(new["/Width"]), int(new["/Height"])
        bpc = int(new.get("/BitsPerComponent", 1 if new.get("/ImageMask") else 8))
        components = 1 if new.get("/ImageMask") else _components(new.get("/ColorSpace"), resources)
        filters = _resolve(new.get("/Filter", []))
        filters = [filters] if isinstance(filters, str) else list(filters)
        compressed = any(
            f in filters for f in ("/DCTDecode", "/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode")
        )
        if not compressed:
            # get_data() decoded all lossless filters. Each row has byte padding.
            expected = ((width * components * bpc + 7) // 8) * height
            if bpc not in (1, 2, 4, 8, 16) or len(data) != expected:
                raise CompressionValidationError("Changed image has invalid sample data")
            continue
        try:
            from PIL import Image
        except ImportError as exc:
            raise CompressionValidationError(
                "Changed compressed image requires Pillow for independent decoding"
            ) from exc
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.size != (width, height) or len(image.getbands()) != components:
                    raise CompressionValidationError(
                        "Changed image dimensions or components disagree with PDF"
                    )
                if "/DCTDecode" in filters and bpc != 8:
                    raise CompressionValidationError("JPEG bit depth disagrees with PDF")
        except CompressionValidationError:
            raise
        except Exception as exc:
            raise CompressionValidationError(
                "Changed image cannot be independently decoded"
            ) from exc


def validate_compression(source: Path, candidate: Path) -> dict:
    """Return preservation evidence or raise if the candidate cannot be trusted.

    Image sample data may change, but all painted operations and interactive structures
    must retain their semantics. This deliberately rejects ambiguous rewrites.
    """
    try:
        validate_pdf(source)
        validate_pdf(candidate)
        before, after = _Document(source), _Document(candidate)
        if len(before.reader.pages) != len(after.reader.pages):
            raise CompressionValidationError("Page count changed")
        for index, (left, right) in enumerate(
            zip(before.reader.pages, after.reader.pages, strict=True)
        ):
            a, b = before.page(left), after.page(right)
            for field, old, new in zip(
                ("page geometry", "text", "graphical content", "annotations or links"),
                a,
                b,
                strict=True,
            ):
                if not _same(old, new):
                    raise CompressionValidationError(f"Page {index + 1}: {field} changed")
            if a[1] is None:
                # Identical parsed operations alone may miss a broken font decoder.
                if not _same(before.canonical(left.get("/Resources")),
                             after.canonical(right.get("/Resources"))):
                    raise CompressionValidationError("Unextractable text resources changed")
        old, new = before.interactive(), after.interactive()
        for label, left, right in zip(
            ("forms", "bookmarks", "named destinations"), old, new, strict=True
        ):
            if not _same(left, right):
                raise CompressionValidationError(f"Document {label} changed")
        _validate_changed_images(before, after)
        independent = _mupdf_check(source, candidate)
        return {
            "mupdf_checked": independent,
            "validation_version": VALIDATION_VERSION,
            "pages": len(before.reader.pages),
            "text_preserved": True,
            "links_preserved": True,
            "annotations_preserved": True,
            "forms_preserved": True,
            "bookmarks_preserved": True,
            "named_destinations_preserved": True,
            "graphics_preserved": True,
            "jpeg_colorspaces_checked": True,
            "images_checked": after.images_checked,
        }
    except CompressionValidationError:
        raise
    except PdfValidationError as exc:
        raise CompressionValidationError(str(exc)) from exc
    except Exception as exc:
        raise CompressionValidationError(
            f"PDF preservation check failed: {type(exc).__name__}"
        ) from exc
