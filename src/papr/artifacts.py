"""Atomic local records and content-addressed copies of exported artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .config import Config


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_copy(source: Path, destination: Path, *, overwrite: bool) -> None:
    """Publish a complete file; exclusive linking protects existing destinations."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        shutil.copy2(source, temporary)
        if overwrite:
            if destination.exists():
                temporary.chmod(destination.stat().st_mode & 0o777)
            temporary.replace(destination)
        else:
            destination.hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cache_path(digest: str, config: Config) -> Path:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid artifact checksum")
    return config.cache_dir / "artifacts" / digest


def remember(path: Path, config: Config) -> dict:
    path = path.expanduser().resolve()
    digest = checksum(path)
    cached = cache_path(digest, config)
    if not cached.is_file() or checksum(cached) != digest:
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as stream:
                temporary = Path(stream.name)
            shutil.copyfile(path, temporary)
            if checksum(temporary) != digest:
                raise ValueError(f"Artifact changed while caching: {path}")
            temporary.replace(cached)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"path": str(path), "name": path.name, "sha256": digest, "bytes": path.stat().st_size}


def locate(path: Path, digest: str, config: Config) -> Path:
    """Recover missing files by verified identity, never by date or title similarity."""
    cached = cache_path(digest, config)
    if path.exists():
        if not path.is_file() or checksum(path) != digest:
            raise ValueError(f"Recorded artifact has changed: {path}")
        return path
    for candidate in (config.download_dir.expanduser() / path.name, cached):
        if candidate.is_file() and checksum(candidate) == digest:
            return candidate
    raise ValueError(f"Recorded artifact is missing: {path}; restore it or export it again")
