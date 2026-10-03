from __future__ import annotations

from pathlib import Path

from ..config import Config
from . import mistral, native


def markdown(pdf: Path, config: Config, backend: str | None = None) -> tuple[str, str]:
    backend = backend or config.md_backend
    if backend == "auto":
        backend = "mistral" if mistral.available() else "native"
    if backend == "mistral":
        return mistral.to_markdown(pdf, config), "mistral"
    if backend == "native":
        return native.to_markdown(pdf), "native"
    raise ValueError(f"Unknown Markdown processor: {backend}")
