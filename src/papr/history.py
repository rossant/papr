"""Remember an exported PDF and its metadata for the latest-download shortcut."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .artifacts import atomic_copy, atomic_json, locate, remember
from .config import Config
from .locking import state_lock
from .model import Article
from .model import article_from_dict as _article

_HISTORY_NAME = "latest-download.json"


def _pdf_hash(pdf: Path) -> str:
    if not pdf.is_file():
        raise ValueError("The latest downloaded PDF is missing or was moved; download it again")
    try:
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("The latest downloaded file is no longer a PDF; download it again")
            stream.seek(0)
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ValueError("The latest downloaded PDF cannot be read; download it again") from exc


def record_download(article: Article, pdf: Path, config: Config) -> None:
    """Record only a successful PDF export, as selected by the caller."""
    with state_lock(config):
        pdf = pdf.expanduser().resolve()
        payload = {
            "version": 1,
            "pdf": str(pdf),
            "sha256": _pdf_hash(pdf),
            "article": article.to_dict(),
        }
        remember(pdf, config)
        atomic_json(config.data_dir / _HISTORY_NAME, payload)


def load_latest(config: Config) -> tuple[Article, Path]:
    """Load the recorded export without guessing from files in Downloads."""
    history = config.data_dir / _HISTORY_NAME
    try:
        raw = history.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError("Download a paper with papr first") from exc
    except UnicodeError as exc:
        raise ValueError("Invalid latest-download history; download a paper again") from exc
    except OSError as exc:
        raise ValueError("Cannot read latest-download history; download a paper again") from exc
    try:
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise ValueError("unsupported history version")
        stored_path = data.get("pdf")
        digest = data.get("sha256")
        if (
            not isinstance(stored_path, str)
            or "\x00" in stored_path
            or not Path(stored_path).is_absolute()
        ):
            raise ValueError("invalid PDF path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid PDF checksum")
        article = _article(data.get("article"))
        pdf = Path(stored_path)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid latest-download history; download a paper again") from exc
    if not pdf.exists():
        try:
            pdf = locate(pdf, digest, config)
        except ValueError as exc:
            raise ValueError("The latest downloaded PDF is missing; download it again") from exc
        if pdf.name != Path(stored_path).name:
            # Zotero attachment imports should retain the original human-readable filename.
            recovered = config.cache_dir / "recovered" / digest / Path(stored_path).name
            if not recovered.is_file() or _pdf_hash(recovered) != digest:
                atomic_copy(pdf, recovered, overwrite=True)
            pdf = recovered
    if _pdf_hash(pdf) != digest:
        raise ValueError("The latest downloaded PDF has changed; download it again")
    return article, pdf
