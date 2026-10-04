"""Remember an exported PDF and its metadata for the latest-download shortcut."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import fields
from pathlib import Path

from .config import Config
from .model import Article, Person

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
    pdf = pdf.expanduser().resolve()
    payload = {
        "version": 1,
        "pdf": str(pdf),
        "sha256": _pdf_hash(pdf),
        "article": article.to_dict(),
    }
    config.data_dir.mkdir(parents=True, exist_ok=True)
    destination = config.data_dir / _HISTORY_NAME
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config.data_dir,
            prefix=".latest-download-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _article(data: object) -> Article:
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise ValueError("invalid article metadata")
    names = {field.name for field in fields(Article)}
    if set(data) - names:
        raise ValueError("unknown article fields")
    authors = data.get("authors", [])
    if not isinstance(authors, list):
        raise ValueError("invalid authors")
    people = []
    for person in authors:
        if not isinstance(person, dict) or set(person) - {"family", "given", "orcid"}:
            raise ValueError("invalid author metadata")
        if any(not isinstance(person.get(key, ""), str) for key in ("family", "given")):
            raise ValueError("invalid author name")
        if person.get("orcid") is not None and not isinstance(person["orcid"], str):
            raise ValueError("invalid author identifier")
        people.append(Person(**person))
    for name, value in data.items():
        if name in {"authors", "year", "score"}:
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid article field")
    year = data.get("year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ValueError("invalid article year")
    score = data.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
        raise ValueError("invalid article score")
    if score is not None and not math.isfinite(score):
        raise ValueError("invalid article score")
    values = dict(data)
    values["authors"] = people
    return Article(**values)


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
    if _pdf_hash(pdf) != digest:
        raise ValueError("The latest downloaded PDF has changed; download it again")
    return article, pdf
