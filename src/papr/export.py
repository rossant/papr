from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .config import Config
from .filename import basename, collision_safe_path
from .formats import bib, csl
from .model import Article
from .processors import markdown
from .processors.native import extract_text

FORMAT_SUFFIX = {
    "pdf": ".pdf",
    "md": ".md",
    "txt": ".txt",
    "bib": ".bib",
    "bibtex": ".bib",
    "csl": ".csl.json",
    "json": ".json",
}


def _write(path: Path, text: str, overwrite: bool) -> Path:
    path = collision_safe_path(path, overwrite=overwrite)
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
        suffix = ".bibtex.bib" if "bib" in formats else ".bib"
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
    requested = processor or config.md_backend
    key = hashlib.sha256(pdf.read_bytes()).hexdigest()
    backend_key = requested.replace("/", "_")
    cache = config.cache_dir / "processors" / f"{key}.{backend_key}.{config.mistral_model}.md"
    marker = cache.with_suffix(cache.suffix + ".backend")
    if cache.exists():
        used = marker.read_text().strip() if marker.exists() else requested
        return cache.read_text(encoding="utf-8"), used
    text, used = markdown(pdf, config, requested)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    marker.write_text(used, encoding="utf-8")
    return text, used
