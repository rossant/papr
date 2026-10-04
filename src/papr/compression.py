"""Optimize a disposable PDF copy, preserving the source and validated cache entries."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .config import Config
from .progress import emit

_CACHE_VERSION = 1


def _report(status: str, original: int, output: int | None = None, **values) -> dict:
    output = original if output is None else output
    saved = original - output
    return {
        "status": status,
        "backend": None,
        "original_bytes": original,
        "output_bytes": output,
        "saved_bytes": saved,
        "savings_percent": round(100 * saved / original, 2) if original else 0,
        "cached": False,
        **values,
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


def _validate(path: Path, page_count: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as file:
            if file.read(5) != b"%PDF-":
                return False
            reader = PdfReader(file)
            return not reader.is_encrypted and len(reader.pages) == page_count
    except Exception:
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


def _store_manifest(path: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(data, file)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def compress_pdf(
    pdf: Path, config: Config, *, enabled: bool | None = None,
) -> tuple[Path, dict]:
    """Return the original path or a smaller validated copy, never replacing the source."""
    original = pdf.stat().st_size
    if not (getattr(config, "pdf_compress", True) if enabled is None else enabled):
        emit("compress", "PDF compression disabled")
        return pdf, _report("disabled", original)
    emit("compress", f"Checking PDF for compression ({original:,} bytes)")
    try:
        with pdf.open("rb") as file:
            if file.read(5) != b"%PDF-":
                raise ValueError("PDF signature missing")
            file.seek(0)
            reader = PdfReader(file)
            if reader.is_encrypted:
                emit("compress", "Keeping encrypted PDF unchanged")
                return pdf, _report("skipped", original, reason="encrypted PDF")
            count = len(reader.pages)
            if _signed(reader):
                emit("compress", "Keeping signed PDF unchanged")
                return pdf, _report("skipped", original, reason="signature or signature field")
            protected = bool(reader.trailer["/Root"].get("/AcroForm")) or any(
                page.get("/Annots") for page in reader.pages
            )
            file.seek(0)
            digest = hashlib.file_digest(file, "sha256").hexdigest()
    except Exception:
        emit("compress", "Keeping unreadable PDF unchanged")
        return pdf, _report("skipped", original, reason="unreadable PDF")

    dpi = getattr(config, "pdf_compression_dpi", 200)
    timeout = getattr(config, "pdf_compression_timeout", 120)
    gs = shutil.which("gs") if not protected else None
    qpdf = shutil.which("qpdf")
    backends = []
    if gs:
        backends.append(("ghostscript", gs))
    if qpdf:
        backends.append(("qpdf", qpdf))
    backends.append(("pypdf", None))
    settings = json.dumps([_CACHE_VERSION, dpi, protected, backends], sort_keys=True)
    key = hashlib.sha256(f"{digest}:{settings}".encode()).hexdigest()
    directory = config.cache_dir / "compression"
    cache = directory / f"{key}.pdf"
    manifest = directory / f"{key}.json"
    try:
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
            if data.get("status") == "compressed" and _validate(cache, count):
                size = cache.stat().st_size
                if size < original:
                    report = _report(
                        "cached", original, size, backend=data.get("backend"), cached=True,
                    )
                    emit("compress", f"Using cached compressed PDF ({size:,} bytes)")
                    return cache, report
            elif data.get("status") == "unchanged":
                emit("compress", "Cached compression check: original PDF is already smaller")
                return pdf, _report(
                    "unchanged", original, backend=data.get("backend"), cached=True,
                    reason=data.get("reason", "no smaller valid output"),
                )
        directory.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, TypeError, AttributeError):
        emit("compress", "Compression cache unavailable; keeping original PDF")
        return pdf, _report("failed", original, reason="compression cache unavailable")

    if protected:
        emit("compress", "Annotations or form fields detected; using lossless compression")
    elif not gs:
        emit("compress", "Ghostscript unavailable; using lossless compression")
    successful = False
    had_failure = False
    last_backend = None
    reason = "no smaller valid output"
    for backend, executable in backends:
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory, suffix=".tmp.pdf", delete=False,
            ) as file:
                temp = Path(file.name)
            last_backend = backend
            emit("compress", f"Compressing PDF with {backend}")
            if backend == "pypdf":
                _lossless(pdf, temp)
            else:
                result = subprocess.run(
                    _command(backend, executable, pdf, temp, dpi),
                    capture_output=True, timeout=timeout, check=False,
                )
                # qpdf can produce a valid output while reporting recoverable warnings.
                if result.returncode not in ({0, 3} if backend == "qpdf" else {0}):
                    had_failure = True
                    reason = f"{backend} exited with status {result.returncode}"
                    emit("compress", f"{reason}; trying lossless fallback")
                    continue
            if not _validate(temp, count):
                had_failure = True
                reason = f"{backend} returned an invalid PDF or changed the page count"
                emit("compress", f"{reason}; keeping original")
                continue
            successful = True
            size = temp.stat().st_size
            if size >= original:
                emit("compress", f"{backend} output is not smaller; keeping original")
                continue
            report = _report("compressed", original, size, backend=backend)
            temp.replace(cache)
            try:
                _store_manifest(manifest, report)
                # Recognize the optimized content even after it is copied out of our cache.
                with cache.open("rb") as file:
                    optimized_digest = hashlib.file_digest(file, "sha256").hexdigest()
                optimized_key = hashlib.sha256(
                    f"{optimized_digest}:{settings}".encode(),
                ).hexdigest()
                _store_manifest(
                    directory / f"{optimized_key}.json",
                    _report(
                        "unchanged", size, backend=backend,
                        reason="already optimized with these settings",
                    ),
                )
            except OSError:
                pass
            emit(
                "compress",
                f"PDF {original:,} → {size:,} bytes · saved {report['savings_percent']:g}%",
            )
            return cache, report
        except subprocess.TimeoutExpired:
            had_failure = True
            reason = f"{backend} timed out after {timeout:g}s"
            emit("compress", f"{reason}; trying lossless fallback")
        except Exception as exc:
            had_failure = True
            reason = f"{backend} compression failed ({type(exc).__name__})"
            emit("compress", f"{reason}; keeping original")
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    report = _report(
        "unchanged" if successful else "failed", original, backend=last_backend,
        reason="no smaller valid output" if successful else reason,
    )
    if successful and not had_failure:
        try:
            _store_manifest(manifest, report)
        except OSError:
            pass
    return pdf, report
