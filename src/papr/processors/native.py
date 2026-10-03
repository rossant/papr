from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def extract_text(pdf: Path) -> str:
    reader = PdfReader(str(pdf))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages).strip() + "\n"


def to_markdown(pdf: Path) -> str:
    return extract_text(pdf)
