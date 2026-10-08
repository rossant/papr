"""Inspection and conservative pruning of papr's content-addressed cache."""

from __future__ import annotations

import json
import math
import re
import stat
import time
from pathlib import Path

from .config import Config

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CATEGORIES = ("artifacts", "pdf", "processors", "recovered")


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _tree_size(path: Path) -> int:
    """Count regular files below a known cache category without following links."""
    try:
        root_stat = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(root_stat.st_mode):
        return 0
    total = 0
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def status(config: Config) -> dict[str, object]:
    root = config.cache_dir.expanduser().absolute()
    categories = {name: _tree_size(root / name) for name in _CATEGORIES}
    return {
        "cache_dir": str(root),
        "total_bytes": sum(categories.values()),
        "categories": categories,
    }


def _history_digest(config: Config) -> str | None:
    path = config.data_dir / "latest-download.json"
    try:
        # Do not follow a redirecting symlink for the history record.
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ValueError(f"Cannot safely read latest-download history: {path} is a symlink")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot safely read latest-download history {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data.get("version") != 1
        or not isinstance(data.get("pdf"), str)
        or not Path(data["pdf"]).is_absolute()
        or not isinstance(data.get("sha256"), str)
        or not _DIGEST.fullmatch(data["sha256"])
    ):
        raise ValueError(f"Cannot safely read latest-download history {path}: invalid record")
    from .history import _article

    try:
        _article(data.get("article"))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            f"Cannot safely read latest-download history {path}: invalid article"
        ) from exc
    return data["sha256"]


def _manifest_paths(config: Config) -> list[Path]:
    # The catalogue records manifests outside data_dir as well. Its failure means
    # we cannot know which cache objects are still referenced.
    from .batch_catalog import known_manifest_paths

    paths = [Path(path) for path in known_manifest_paths(config)]
    return paths


def _referenced(config: Config, extra_manifests: list[Path]) -> set[str]:
    references: set[str] = set()
    latest = _history_digest(config)
    if latest:
        references.add(latest)
    from .batches import load_manifest

    paths = list(dict.fromkeys([*_manifest_paths(config), *extra_manifests]))
    for path in paths:
        data = load_manifest(path)
        for row in data["items"]:
            for artifact in [*row.get("artifacts", []), *row.get("pending_artifacts", [])]:
                references.add(artifact["sha256"])
    return references


def prune(
    config: Config,
    *,
    dry_run: bool = False,
    manifests: list[Path] | None = None,
    older_than_days: float = 7,
) -> list[Path]:
    """Remove only old, unreferenced flat artifact objects; return selected paths."""
    if not math.isfinite(older_than_days) or older_than_days < 0:
        raise ValueError("--older-than must be zero or greater")
    root = config.cache_dir.expanduser().absolute()
    artifacts_dir = root / "artifacts"
    if _has_symlink_component(artifacts_dir):
        raise ValueError(f"Cannot safely prune cache through a symlink: {artifacts_dir}")
    try:
        if stat.S_ISLNK(root.lstat().st_mode):
            raise ValueError(f"Cannot safely prune cache through symlink: {root}")
    except FileNotFoundError:
        return []
    try:
        if not stat.S_ISDIR(artifacts_dir.lstat().st_mode):
            return []
    except FileNotFoundError:
        return []

    # Resolve all protection records before looking at deletion candidates. Any
    # missing/corrupt known record blocks the complete prune operation.
    references = _referenced(config, manifests or [])
    cutoff = time.time() - older_than_days * 86400
    selected: list[Path] = []
    selected_stats = {}
    for candidate in artifacts_dir.iterdir():
        if not _DIGEST.fullmatch(candidate.name):
            continue
        try:
            before = candidate.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(before.st_mode) or candidate.name in references:
            continue
        if before.st_mtime > cutoff:
            continue
        selected.append(candidate)
        selected_stats[candidate] = before

    if dry_run:
        return selected
    removed: list[Path] = []
    for candidate in selected:
        # Revalidate immediately before unlinking. The age grace period protects
        # normal atomic writers; inode/mtime checks avoid deleting a replacement.
        try:
            current = candidate.lstat()
            initial = selected_stats[candidate]
            if (
                stat.S_ISREG(current.st_mode)
                and current.st_ino == initial.st_ino
                and current.st_dev == initial.st_dev
                and current.st_mtime == initial.st_mtime
                and current.st_mtime <= cutoff
                and candidate.name not in references
            ):
                candidate.unlink()
                removed.append(candidate)
        except (OSError, StopIteration):
            continue
    return removed
