from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

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


def output_suffix(format_name: str, formats: list[str]) -> str:
    if format_name == "bibtex" and "bib" in formats:
        return ".bibtex.bib"
    return FORMAT_SUFFIX[format_name]


def _write(path: Path, text: str, overwrite: bool) -> Path:
    path = collision_safe_path(path, overwrite=overwrite)
    emit("export", f"Writing {path.name}")
    path.write_text(text, encoding="utf-8")
    return path


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
) -> tuple[list[Path], str | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = basename(article, config, filename_template)
    outputs: list[Path] = []
    processor_used: str | None = None

    if "pdf" in formats:
        dest = collision_safe_path(output_dir / f"{base}.pdf", overwrite=overwrite)
        emit("export", f"Saving {dest.name}")
        if pdf.resolve() != dest.resolve():
            shutil.copy2(pdf, dest)
        outputs.append(dest)
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
            _write(output_dir / f"{base}{suffix}", bib.encode(article, biblatex=False), overwrite)
        )
    if "csl" in formats:
        text = json.dumps([csl.encode(article)], indent=2, ensure_ascii=False) + "\n"
        outputs.append(_write(output_dir / f"{base}.csl.json", text, overwrite))
    if "json" in formats:
        text = json.dumps(article.to_dict(), indent=2, ensure_ascii=False) + "\n"
        outputs.append(_write(output_dir / f"{base}.json", text, overwrite))
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
