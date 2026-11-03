"""Versioned compression results, keyed by source, policy and tool versions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .artifacts import atomic_json, checksum
from .compression_validation import validate_compression

CACHE_VERSION = 2


class CompressionCache:
    def __init__(self, directory: Path, digest: str, settings: dict):
        self.directory = directory
        self.digest = digest
        self.settings = json.dumps({"schema": CACHE_VERSION, **settings}, sort_keys=True)
        self.pdf, self.manifest = self._paths(digest)

    def _paths(self, digest: str) -> tuple[Path, Path]:
        key = hashlib.sha256(f"{digest}:{self.settings}".encode()).hexdigest()
        return self.directory / f"{key}.pdf", self.directory / f"{key}.json"

    def load(self, source: Path) -> tuple[Path, dict] | None:
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("cache_schema") != CACHE_VERSION:
                return None
            if data.get("status") == "unchanged":
                return source, {**data, "cached": True}
            if (
                data.get("status") != "compressed" or not self.pdf.is_file()
                or self.pdf.stat().st_size >= source.stat().st_size
                or checksum(self.pdf) != data.get("output_sha256")
            ):
                return None
            evidence = validate_compression(source, self.pdf)
            return self.pdf, {**data, "status": "cached", "cached": True,
                              "validation": evidence}
        except (OSError, ValueError, TypeError):
            return None

    def store(self, prepared: Path, report: dict, *, remember: bool = True) -> Path:
        prepared.replace(self.pdf)
        if not remember:
            # Keep a usable disposable result, but retry a transient backend
            # failure on the next request rather than remembering its fallback.
            self.manifest.unlink(missing_ok=True)
            return self.pdf
        digest = checksum(self.pdf)
        record = {**report, "cache_schema": CACHE_VERSION, "output_sha256": digest}
        atomic_json(self.manifest, record)
        _, marker = self._paths(digest)
        atomic_json(marker, {
            **record, "status": "unchanged", "original_bytes": report["output_bytes"],
            "saved_bytes": 0, "savings_percent": 0,
            "reason": "already optimized with these settings",
        })
        return self.pdf

    def remember_unchanged(self, report: dict) -> None:
        atomic_json(self.manifest, {**report, "cache_schema": CACHE_VERSION})
