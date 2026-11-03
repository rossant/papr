from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .artifacts import atomic_copy, atomic_text
from .config import Config
from .filename import basename, collision_safe_path
from .formats import bib, csl
from .model import Article
from .processors import markdown, select_backend
from .processors.native import extract_text
from .progress import emit

FORMAT_SUFFIX = {
    "pdf": ".pdf",
    "md": ".md",
    "txt": ".txt",
    "bib": ".bib",
    "bibtex": ".bib",
    "csl": ".csl.json",
    "json": ".json",
}


class ExportError(Exception):
    """A later export failed after these outputs were successfully published."""

    def __init__(self, message: str, outputs: list[Path], processor: str | None):
        super().__init__(message)
        self.outputs = list(outputs)
        self.processor = processor


def output_suffix(format_name: str, formats: list[str]) -> str:
    if format_name == "bibtex" and "bib" in formats:
        return ".bibtex.bib"
    return FORMAT_SUFFIX[format_name]


def _write(path: Path, text: str, overwrite: bool) -> Path:
    while True:
        destination = collision_safe_path(path, overwrite=overwrite)
        emit("export", f"Writing {destination.name}")
        try:
            atomic_text(destination, text, overwrite=overwrite)
        except FileExistsError:
            if overwrite:
                raise
            continue
        return destination


def _copy(source: Path, path: Path, overwrite: bool) -> Path:
    while True:
        destination = collision_safe_path(path, overwrite=overwrite)
        emit("export", f"Saving {destination.name}")
        if source.resolve() == destination.resolve():
            return destination
        try:
            atomic_copy(source, destination, overwrite=overwrite)
        except FileExistsError:
            if overwrite:
                raise
            continue
        return destination


def export_outputs(
    article: Article,
    pdf: Path,
    formats: list[str],
    output_dir: Path,
    config: Config,
    *,
    filename_template: str | None = None,
    processor: str | None = None,
    overwrite: bool = False,
    pdf_output: Path | None = None,
) -> tuple[list[Path], str | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = basename(article, config, filename_template)
    outputs: list[Path] = []
    processor_used: str | None = None

    try:
        if "pdf" in formats:
            source_pdf = pdf_output if pdf_output is not None else pdf
            outputs.append(_copy(source_pdf, output_dir / f"{base}.pdf", overwrite))
        if "md" in formats:
            text, processor_used = _cached_markdown(pdf, config, processor)
            outputs.append(_write(output_dir / f"{base}.md", text, overwrite))
        if "txt" in formats:
            outputs.append(_write(output_dir / f"{base}.txt", extract_text(pdf), overwrite))
        if "bib" in formats:
            outputs.append(
                _write(output_dir / f"{base}.bib", bib.encode(article, biblatex=True), overwrite)
            )
        if "bibtex" in formats:
            suffix = output_suffix("bibtex", formats)
            outputs.append(
                _write(
                    output_dir / f"{base}{suffix}", bib.encode(article, biblatex=False), overwrite
                )
            )
        if "csl" in formats:
            text = json.dumps([csl.encode(article)], indent=2, ensure_ascii=False) + "\n"
            outputs.append(_write(output_dir / f"{base}.csl.json", text, overwrite))
        if "json" in formats:
            text = json.dumps(article.to_dict(), indent=2, ensure_ascii=False) + "\n"
            outputs.append(_write(output_dir / f"{base}.json", text, overwrite))
    except Exception as exc:
        if outputs:
            raise ExportError(str(exc), outputs, processor_used) from exc
        raise
    return outputs, processor_used


def _cached_markdown(pdf: Path, config: Config, processor: str | None) -> tuple[str, str]:
    emit("process", "Checking Markdown cache")
    backend = select_backend(config, processor)
    with pdf.open("rb") as file:
        key = hashlib.file_digest(file, "sha256").hexdigest()
    model_key = hashlib.sha256(config.mistral_model.encode()).hexdigest()[:16]
    suffix = f"{backend}.{model_key}" if backend == "mistral" else backend
    cache = config.cache_dir / "processors" / f"{key}.{suffix}.md"
    if cache.exists():
        emit("process", f"Using cached Markdown ({backend})")
        return cache.read_text(encoding="utf-8"), backend
    text, used = markdown(pdf, config, backend)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=cache.parent, suffix=".tmp", delete=False
        ) as file:
            temp = Path(file.name)
            file.write(text)
        emit("process", "Saving Markdown to cache")
        temp.replace(cache)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return text, used
