from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..progress import emit
from . import mistral, native


def select_backend(config: Config, backend: str | None = None) -> str:
    backend = backend or config.md_backend
    if backend == "auto":
        backend = "mistral" if mistral.available() else "native"
    if backend not in {"mistral", "native"}:
        raise ValueError(f"Unknown Markdown processor: {backend}")
    return backend


def markdown(pdf: Path, config: Config, backend: str | None = None) -> tuple[str, str]:
    backend = select_backend(config, backend)
    emit("process", f"Converting PDF with {backend}")
    if backend == "mistral":
        return mistral.to_markdown(pdf, config), "mistral"
    if backend == "native":
        return native.to_markdown(pdf), "native"
    raise ValueError(f"Unknown Markdown processor: {backend}")
