"""Private subprocess entry point; never import native PDF code in the parent.

Only existing photographic image streams are changed. Page content, fonts, vector
paths, image placements and original color spaces (including ICC) stay in place.
Document.rewrite_images is deliberately avoided: see PyMuPDF issue #5164.
"""
from __future__ import annotations

import io
import json
import math
import platform
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

# Changes to this list require the independent-renderer regression fixtures.
_TESTED_ENGINES = {("2.1", "1.28.6")}


def _info(pdf, pillow) -> dict:
    eligible = (pdf.VersionBind, pdf.VersionFitz) in _TESTED_ENGINES
    return {
        "backend": "pymupdf", "python_version": platform.python_version(),
        "library_version": pdf.VersionBind, "engine_version": pdf.VersionFitz,
        "pillow_version": pillow.__version__, "eligible": eligible,
        "eligibility_note": (
            "tested per-image stream replacement; global rewrite_images is not used"
            if eligible else "untested PyMuPDF/MuPDF version; use lossless fallback"
        ),
    }


def _resolved(doc, kind: str, value: str) -> str:
    seen = set()
    while kind == "xref":
        xref = int(value.split()[0])
        if xref in seen:
            return ""
        seen.add(xref)
        value = doc.xref_object(xref)
        kind = "array" if value.lstrip().startswith("[") else "other"
        match = re.fullmatch(r"\s*(\d+)\s+0\s+R\s*", value)
        if match:
            kind = "xref"
    return value.strip()


def _components(doc, xref: int) -> int | None:
    value = _resolved(doc, *doc.xref_get_key(xref, "ColorSpace"))
    if value in {"/DeviceRGB", "/DeviceGray"}:
        return 3 if value == "/DeviceRGB" else 1
    match = re.fullmatch(r"\[\s*/ICCBased\s+(\d+)\s+0\s+R\s*\]", value)
    if match:
        kind, number = doc.xref_get_key(int(match[1]), "N")
        if kind == "int" and number in {"1", "3"}:
            return int(number)
    return None


def _present(doc, xref: int, key: str) -> bool:
    return doc.xref_get_key(xref, key)[0] != "null"


def _inventory(doc) -> tuple[dict, int]:
    images = {}
    inline = 0
    for page in doc:
        for resource in page.get_images(full=True):
            images.setdefault(resource[0], {"page": page.number, "placements": []})
        # Includes nested Form XObjects. Matrices are in physical points and
        # already include page UserUnit; axis lengths avoid rotation/skew bbox errors.
        for image in page.get_image_info(xrefs=True):
            xref = image["xref"]
            if not xref:
                inline += 1
                continue
            item = images.setdefault(xref, {"page": page.number, "placements": []})
            a, b, c, d, _, _ = image["transform"]
            area = abs(a * d - b * c)
            page_area = page.rect.width * page.rect.height
            item["placements"].append((a, b, c, d, area / page_area if page_area else 1))
    return images, inline


def _photographic(image) -> bool:
    """Reject limited-palette diagrams, flat backgrounds and black/white line work.

    This heuristic intentionally misses some photographs. It cannot establish
    clinical fidelity, and profiles remain opt-in for lossy image changes.
    """
    sample = image.copy()
    sample.thumbnail((256, 256))
    palette_limit = 32 if image.mode == "L" else 1024
    if sample.getcolors(maxcolors=palette_limit) is not None:
        return False
    histogram = sample.convert("L").histogram()
    count = sum(histogram)
    extremes = sum(histogram[:16]) + sum(histogram[240:])
    return bool(count) and extremes / count < 0.5 and sample.entropy() > 5


def _effective_dpi(width, height, placements):
    # Largest singular value of the pixel-to-page matrix also covers shear.
    values = []
    for a, b, c, d, _ in placements:
        xx, yy = (a * a + b * b) / width**2, (c * c + d * d) / height**2
        xy = (a * c + b * d) / (width * height)
        stretch = math.sqrt((xx + yy + math.hypot(xx - yy, 2 * xy)) / 2)
        values.append(72 / stretch if stretch else 0)
    return min(values)


def _candidate(doc, xref, item, policy, Image):
    if any(_present(doc, xref, key) for key in (
        "SMask", "Mask", "Decode", "Alternates", "SMaskInData", "OC",
    )) or doc.xref_get_key(xref, "ImageMask")[1] == "true":
        return None, "mask or special image semantics"
    components = _components(doc, xref)
    if components is None:
        return None, "unsupported color space"
    if doc.xref_get_key(xref, "BitsPerComponent")[1] != "8":
        return None, "bitonal or unsupported bit depth"
    placements = item["placements"]
    if not placements or any(abs(a * d - b * c) < 1e-8 for a, b, c, d, _ in placements):
        return None, "unknown image placement"
    if any(area >= 0.75 for _, _, _, _, area in placements):
        return None, "full-page scan preserved"
    width = int(doc.xref_get_key(xref, "Width")[1])
    height = int(doc.xref_get_key(xref, "Height")[1])
    if width * height > 80_000_000:
        return None, "image exceeds decode limit"
    # The largest occurrence along either axis limits how far a shared image
    # may be reduced. Apply one scale to both axes to preserve its aspect ratio.
    dpi = _effective_dpi(width, height, placements)
    target = policy["dpi"]
    if dpi <= target * policy.get("downsample_threshold", 1.4):
        return None, "resolution below downsample threshold"
    mode = "RGB" if components == 3 else "L"
    filter_value = doc.xref_get_key(xref, "Filter")[1]
    raw = doc.xref_stream_raw(xref)
    decode_params = _resolved(doc, *doc.xref_get_key(xref, "DecodeParms"))
    if decode_params not in {"null", "<<>>", "<< >>"}:
        return None, "nontrivial image decode parameters"
    if filter_value == "/DCTDecode":
        image = Image.open(io.BytesIO(raw))
        image.load()
        if image.mode != mode or image.size != (width, height):
            return None, "JPEG components do not match color space"
    elif filter_value in {"null", "/FlateDecode", "[/FlateDecode]"}:
        samples = doc.xref_stream(xref)
        if len(samples) != width * height * components:
            return None, "unsupported decoded image layout"
        image = Image.frombytes(mode, (width, height), samples)
    else:
        return None, "unsupported image encoding"
    if not _photographic(image):
        return None, "raster line art or limited-tone image preserved"
    scale = target / dpi
    size = (max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale)))
    image = image.resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=policy["quality"], optimize=True, subsampling=0)
    encoded = output.getvalue()
    saved = len(raw) - len(encoded)
    if saved < max(policy.get("min_savings_bytes", 4096),
                   len(raw) * policy.get("min_savings_ratio", 0.01)):
        return None, "insufficient image savings"
    return (encoded, *size), None


def _compress(doc, source: Path, output: Path, policy: dict, Image) -> dict:
    images, inline = _inventory(doc)
    changed = 0
    reasons = Counter({"inline image preserved": inline}) if inline else Counter()
    details = []
    for xref, item in images.items():
        if policy["profile"] == "lossless":
            reasons["lossless profile"] += 1
            continue
        try:
            candidate, reason = _candidate(doc, xref, item, policy, Image)
        except (ValueError, OSError, RuntimeError, KeyError, OverflowError):
            candidate, reason = None, "image could not be safely decoded"
        if candidate is None:
            reasons[reason] += 1
            continue
        encoded, width, height = candidate
        doc.update_stream(xref, encoded, compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
        doc.xref_set_key(xref, "DecodeParms", "null")
        doc.xref_set_key(xref, "Width", str(width))
        doc.xref_set_key(xref, "Height", str(height))
        changed += 1
        details.append({"xref": xref, "width": width, "height": height})
    if changed:
        doc.save(output, garbage=0, deflate=True, use_objstms=1)
    else:
        shutil.copyfile(source, output)
    return {
        "changed_images": changed, "skipped_images": len(images) + inline - changed,
        "total_images": len(images) + inline, "lossy_applied": bool(changed),
        "reasons": dict(reasons), "image_changes": details,
    }


def main() -> int:
    try:
        import PIL
        import pymupdf
        from PIL import Image

        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
        request = json.load(sys.stdin)
        info = _info(pymupdf, PIL)
        if request["operation"] == "info":
            print(json.dumps(info))
            return 0
        if not info["eligible"]:
            raise RuntimeError(info["eligibility_note"])
        source, output = Path(request["source"]), Path(request["output"])
        if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
            raise ValueError("output must differ from source")
        with pymupdf.open(source) as doc:
            if not doc.is_pdf or doc.needs_pass or doc.is_repaired:
                raise ValueError("PDF requires repair, authentication or conversion")
            result = _compress(doc, source, output, request["policy"], Image)
        print(json.dumps({**info, **result}))
        return 0
    except Exception as error:
        # Do not leak filenames / document contents into parent logs.
        print(json.dumps({"error": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
