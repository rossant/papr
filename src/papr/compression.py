"""Optimize disposable PDF copies while preserving the source document."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .compression_cache import CompressionCache
from .compression_images import backend_info, compress_images
from .compression_policy import resolve_policy
from .compression_tools import pypdf_version, tool_version
from .compression_validation import VALIDATION_VERSION, validate_compression
from .config import Config
from .progress import emit


def _report(status: str, original: int, output: int | None = None, **values) -> dict:
    output = original if output is None else output
    saved = original - output
    return {
        "status": status, "backend": None, "original_bytes": original,
        "output_bytes": output, "saved_bytes": saved,
        "savings_percent": round(100 * saved / original, 2) if original else 0,
        "cached": False, **values,
    }


def _signed(reader: PdfReader) -> bool:
    root = reader.trailer["/Root"]
    if root.get("/Perms"):
        return True
    form = root.get("/AcroForm")
    if not form:
        return False
    form = form.get_object()
    if form.get("/SigFlags"):
        return True
    todo = list(form.get("/Fields", []))
    visited = set()
    while todo:
        field = todo.pop().get_object()
        if id(field) in visited:
            continue
        visited.add(id(field))
        if field.get("/FT") == "/Sig":
            return True
        todo.extend(field.get("/Kids", []))
    return False


def _command(backend: str, executable: str, source: Path, output: Path, dpi: int) -> list[str]:
    if backend == "qpdf":
        return [
            executable, "--object-streams=generate", "--stream-data=compress",
            "--recompress-flate", "--compression-level=9", str(source.resolve()), str(output),
        ]
    return [
        executable, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7", "-dEmbedAllFonts=true", "-dSubsetFonts=true",
        "-dDetectDuplicateImages=true", "-dCompressFonts=true", "-dCompressPages=true",
        "-dPassThroughJPEGImages=true", "-dPassThroughJPXImages=true",
        "-dDownsampleColorImages=true", "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={dpi}", "-dColorImageDownsampleThreshold=1.5",
        "-dDownsampleGrayImages=true", "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={dpi}", "-dGrayImageDownsampleThreshold=1.5",
        "-dDownsampleMonoImages=true", "-dMonoImageDownsampleType=/Subsample",
        "-dMonoImageResolution=600", "-dMonoImageDownsampleThreshold=1.5",
        f"-sOutputFile={output}", "-f", str(source.resolve()),
    ]


def _lossless(source: Path, output: Path) -> None:
    writer = PdfWriter(clone_from=str(source))
    for page in writer.pages:
        page.compress_content_streams(level=9)
    writer.write(output)


def compress_pdf(
    pdf: Path, config: Config, *, enabled: bool | None = None,
) -> tuple[Path, dict]:
    """Return the original or a smaller, preservation-validated delivery copy."""
    original = pdf.stat().st_size
    if not (config.pdf_compress if enabled is None else enabled):
        emit("compress", "PDF compression disabled")
        return pdf, _report("disabled", original)
    policy = resolve_policy(config)
    common = {"requested_profile": policy.profile, "resolved_dpi": policy.dpi,
              "resolved_quality": policy.quality, "lossy_applied": False,
              "changed_images": 0, "skipped_images": 0}
    emit("compress", f"Checking PDF for {policy.profile} compression ({original:,} bytes)")
    try:
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("PDF signature missing")
            stream.seek(0)
            reader = PdfReader(stream)
            if reader.is_encrypted:
                return pdf, _report("skipped", original, reason="encrypted PDF", **common)
            if _signed(reader):
                return pdf, _report("skipped", original, reason="signature or signature field",
                                    **common)
            protected = bool(reader.trailer["/Root"].get("/AcroForm")) or any(
                page.get("/Annots") for page in reader.pages
            )
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except Exception:
        return pdf, _report("skipped", original, reason="unreadable PDF", **common)

    info = backend_info() if policy.profile != "lossless" else None
    image_ready = bool(info and info.get("eligible"))
    backends = []
    if image_ready:
        backends.append(("pymupdf", None, info))
    elif policy.profile != "lossless" and not protected and (gs := shutil.which("gs")):
        backends.append(("ghostscript", gs, {"version": tool_version(gs)}))
    if qpdf := shutil.which("qpdf"):
        backends.append(("qpdf", qpdf, {"version": tool_version(qpdf)}))
    backends.append(("pypdf", None, {"version": pypdf_version()}))
    cache = CompressionCache(config.cache_dir / "compression", digest, {
        "policy": policy.to_dict(), "protected": protected,
        "backends": backends, "validation_version": VALIDATION_VERSION,
    })
    cached = cache.load(pdf)
    if cached:
        emit("compress", "Using cached compression result")
        return cached
    try:
        cache.directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return pdf, _report("failed", original, reason="compression cache unavailable", **common)

    successful = False
    had_failure = False
    fallback = None
    if policy.profile != "lossless" and not image_ready:
        fallback = (info or {}).get("eligibility_note") or (
            "Image compression extra unavailable; using legacy or lossless fallback"
        )
    reasons = []
    last_backend = None
    for backend, executable, versions in backends:
        temporary = None
        metadata = {**common, "backend": backend, "backend_versions": versions}
        if fallback:
            metadata["fallback_reason"] = fallback
        try:
            with tempfile.NamedTemporaryFile(dir=cache.directory, suffix=".tmp.pdf",
                                             delete=False) as stream:
                temporary = Path(stream.name)
            last_backend = backend
            emit("compress", f"Compressing PDF with {backend}")
            if backend == "pymupdf":
                image_result = compress_images(
                    pdf, temporary, policy.to_dict(), config.pdf_compression_timeout,
                )
                metadata.update({key: value for key, value in image_result.items()
                                 if key not in {"backend", "eligible"}})
            elif backend == "pypdf":
                _lossless(pdf, temporary)
            else:
                result = subprocess.run(
                    _command(backend, executable, pdf, temporary, policy.dpi),
                    capture_output=True, timeout=config.pdf_compression_timeout, check=False,
                )
                if result.returncode not in ({0, 3} if backend == "qpdf" else {0}):
                    raise RuntimeError(f"{backend} exited with status {result.returncode}")
                if backend == "ghostscript":
                    metadata["lossy_applied"] = None
                    metadata["image_policy"] = "legacy Ghostscript; image changes uncounted"
            metadata["validation"] = validate_compression(pdf, temporary)
            successful = True
            size = temporary.stat().st_size
            minimum = max(policy.min_savings_bytes, original * policy.min_savings_ratio)
            if size >= original or (backend == "pymupdf" and original - size < minimum):
                if backend == "pymupdf":
                    fallback = (
                        "Image rewrite produced insufficient savings; using lossless fallback"
                    )
                continue
            report = _report("compressed", original, size, **metadata)
            output = cache.store(temporary, report, remember=not had_failure)
            temporary = None
            emit("compress", f"PDF {original:,} → {size:,} bytes · "
                             f"saved {report['savings_percent']:g}%")
            return output, report
        except Exception as exc:
            had_failure = True
            reason = (f"{backend} timed out" if isinstance(exc, subprocess.TimeoutExpired)
                      else f"{backend}: {type(exc).__name__}: {exc}")
            # Exceptions from our fixed worker protocol and validation contain no
            # source text. Native command stderr and PDF contents are never logged.
            reasons.append(reason)
            fallback = reason
            emit("compress", f"{backend} result rejected; trying lossless fallback")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    report = _report("unchanged" if successful else "failed", original, **common,
                     backend=last_backend,
                     reason="no smaller valid output" if successful else "; ".join(reasons),
                     **({"fallback_reason": fallback} if fallback else {}))
    if successful and not had_failure:
        try:
            cache.remember_unchanged(report)
        except OSError:
            pass
    return pdf, report
