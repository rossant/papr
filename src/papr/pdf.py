"""Provider-independent PDF byte and local-file signature checks."""

from __future__ import annotations

from pathlib import Path


def is_pdf(content: bytes, content_type: str = "") -> bool:
    """Check the PDF signature; publisher content types are not authoritative."""
    return content.startswith(b"%PDF-")


def is_local_pdf(value: str | Path | None) -> bool:
    """Check that a local path is a readable regular file with a PDF signature."""
    if not value:
        return False
    path = Path(value).expanduser()
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return is_pdf(stream.read(5))
    except OSError:
        return False
