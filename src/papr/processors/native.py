from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from ..progress import emit


def extract_text(pdf: Path) -> str:
    emit("process", "Opening PDF for text extraction")
    reader = PdfReader(str(pdf))
    pages = []
    total = len(reader.pages)
    emit("process", "Extracting PDF text", completed=0, total=total, unit="pages")
    for number, page in enumerate(reader.pages, start=1):
        pages.append(page.extract_text() or "")
        emit(
            "process", f"Extracted page {number}/{total}",
            completed=number, total=total, unit="pages",
        )
    return "\n\n".join(pages).strip() + "\n"


def to_markdown(pdf: Path) -> str:
    return extract_text(pdf)
