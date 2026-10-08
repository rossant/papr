"""Inspection and conservative pruning of papr's content-addressed cache."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import time
from contextlib import nullcontext
from pathlib import Path

from .config import Config

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CATEGORIES = ("artifacts", "pdf", "processors", "recovered")


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
    if dry_run:
        lock = nullcontext()
    else:
        from .locking import state_lock

        lock = state_lock(config)
    with lock:
        return _prune_locked(
            config,
            dry_run=dry_run,
            manifests=manifests or [],
            older_than_days=older_than_days,
        )


def _open_directory_nofollow(path: Path) -> int | None:
    """Open a directory by walking every component without following symlinks."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("Safe cache pruning requires O_NOFOLLOW and O_DIRECTORY support")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                os.close(current_fd)
                return None
            except OSError as exc:
                if exc.errno in {errno.ENOTDIR, errno.ELOOP}:
                    raise ValueError(
                        f"Cannot safely prune through non-directory or symlink: {path}"
                    ) from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise


def _prune_locked(
    config: Config,
    *,
    dry_run: bool,
    manifests: list[Path],
    older_than_days: float,
) -> list[Path]:
    root = Path(os.path.abspath(config.cache_dir.expanduser()))
    artifacts_dir = root / "artifacts"
    fd = _open_directory_nofollow(artifacts_dir)
    if fd is None:
        return []
    try:
        # Resolve all protection records before looking at deletion candidates.
        # Any missing/corrupt known record blocks the complete prune operation.
        references = _referenced(config, manifests)
        cutoff = time.time() - older_than_days * 86400
        selected: list[tuple[str, os.stat_result]] = []
        try:
            names = os.listdir(fd)
        except OSError as exc:
            raise ValueError(f"Cannot safely read artifact cache {artifacts_dir}: {exc}") from exc
        for name in names:
            if not _DIGEST.fullmatch(name):
                continue
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(before.st_mode) or name in references:
                continue
            if before.st_mtime > cutoff:
                continue
            selected.append((name, before))

        selected_paths = [artifacts_dir / name for name, _ in selected]
        if dry_run:
            return selected_paths
        removed: list[Path] = []
        for name, initial in selected:
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (
                    stat.S_ISREG(current.st_mode)
                    and current.st_ino == initial.st_ino
                    and current.st_dev == initial.st_dev
                    and current.st_mtime_ns == initial.st_mtime_ns
                    and current.st_mtime <= cutoff
                    and name not in references
                ):
                    os.unlink(name, dir_fd=fd)
                    removed.append(artifacts_dir / name)
            except OSError:
                continue
        return removed
    finally:
        os.close(fd)
