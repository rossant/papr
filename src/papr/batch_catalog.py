"""Private local index for named and externally stored batch manifests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from .artifacts import atomic_json
from .config import Config

_SCHEMA = "papr-batch-catalog"


def _catalog_path(config: Config) -> Path:
    return config.data_dir / "batches" / ".catalog.json"


def _read(config: Config) -> list[dict]:
    path = _catalog_path(config)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(f"Cannot read batch catalog {path}: {exc}") from exc
    try:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("catalog must be a regular file, not a symlink")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema") != _SCHEMA
            or type(data.get("version")) is not int
            or data.get("version") != 1
            or not isinstance(data.get("manifests"), list)
        ):
            raise ValueError("unsupported registry schema")
        rows = data["manifests"]
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("path"), str)
                or "\x00" in row["path"]
                or not Path(row["path"]).is_absolute()
                or "name" not in row
                or (row.get("name") is not None and not _valid_name(row["name"]))
                or not isinstance(row.get("id"), str)
                or not row["id"]
            ):
                raise ValueError("invalid registry record")
        return rows
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError(f"Cannot read batch catalog {path}: {exc}") from exc


def _valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name.strip())
        and name == name.strip()
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
    )


def register_manifest(
    path: Path,
    config: Config,
    *,
    name: str | None = None,
    manifest_id: str | None = None,
) -> None:
    if name is not None and not _valid_name(name):
        raise ValueError("Batch name must be a non-empty label, not a path")
    resolved = path.expanduser().resolve()
    from .locking import state_lock

    with state_lock(config):
        if manifest_id is None:
            from .batches import load_manifest

            data = load_manifest(resolved)
            manifest_id = data.get("id", resolved.stem)
        if not isinstance(manifest_id, str) or not manifest_id or "\x00" in manifest_id:
            raise ValueError("Batch ID must be a non-empty string")
        rows = [row for row in _read(config) if Path(row["path"]) != resolved]
        rows.append({"id": manifest_id, "name": name, "path": str(resolved)})
        atomic_json(_catalog_path(config), {"schema": _SCHEMA, "version": 1, "manifests": rows})


def list_manifests(config: Config) -> list[dict]:
    """Return automatic manifests and registered external manifests with summaries."""
    records = {row["path"]: dict(row) for row in _read(config)}
    folder = config.data_dir / "batches"
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if path.name.startswith("."):
                continue
            resolved = path.resolve()
            records.setdefault(
                str(resolved),
                {
                    "id": _manifest_id(resolved),
                    "name": None,
                    "path": str(resolved),
                },
            )
    manifests = []
    from .batches import load_manifest

    for record in records.values():
        path = Path(record["path"])
        if not path.is_file():
            manifests.append(
                {
                    **record,
                    "created_at": None,
                    "total": 0,
                    "available": 0,
                    "missing": 1,
                    "changed": 0,
                    "unreadable": 0,
                    "incomplete": 0,
                    "items": [],
                    "manifest_status": "missing",
                    "detail": "registered manifest file is missing",
                }
            )
            continue
        try:
            data = load_manifest(path)
        except ValueError as exc:
            manifests.append(
                {
                    **record,
                    "created_at": None,
                    "total": 0,
                    "available": 0,
                    "missing": 0,
                    "changed": 0,
                    "unreadable": 1,
                    "incomplete": 0,
                    "items": [],
                    "manifest_status": "unreadable",
                    "detail": str(exc),
                }
            )
            continue
        summary = manifest_summary(data, config)
        manifests.append({**record, "created_at": data.get("created_at"), **summary})
    return sorted(manifests, key=lambda row: (row.get("created_at") or "", row["id"]))


def _manifest_id(path: Path) -> str:
    from .batches import load_manifest

    return load_manifest(path).get("id", path.stem)


def manifest_summary(data: dict, config: Config) -> dict:
    """Summarize artifact availability by verifying every recorded hash."""
    from .artifacts import locate

    items = []
    counts = {state: 0 for state in ("available", "missing", "changed", "unreadable")}
    incomplete = 0
    for index, row in enumerate(data["items"], 1):
        artifacts = [*row.get("artifacts", []), *row.get("pending_artifacts", [])]
        title = row.get("article", {}).get("title") or row.get("input") or "Untitled item"
        failures = []
        found = 0
        for artifact in artifacts:
            try:
                locate(Path(artifact["path"]), artifact["sha256"], config)
                found += 1
            except ValueError as exc:
                message = str(exc).lower()
                failures.append("changed" if "changed" in message else "missing")
            except OSError:
                failures.append("unreadable")
        if not artifacts:
            state = "missing"
        elif not failures:
            state = "available"
        else:
            state = next(
                value for value in ("changed", "unreadable", "missing") if value in failures
            )
        counts[state] += 1
        incomplete += int(row.get("status") == "error" or row.get("export_status") == "partial")
        items.append(
            {
                "index": index,
                "title": title,
                "status": row.get("export_status") or row["status"],
                "availability": state,
                "available_artifacts": found,
                "total_artifacts": len(artifacts),
            }
        )
    return {"total": len(items), **counts, "incomplete": incomplete, "items": items}


def known_manifest_paths(config: Config) -> list[Path]:
    """All manifests that may protect cached artifacts, including external paths."""
    records = {row["path"] for row in _read(config)}
    folder = config.data_dir / "batches"
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if not path.name.startswith("."):
                records.add(str(path.resolve()))
    return [Path(value) for value in sorted(records)]


def resolve_manifest(value: str | Path, config: Config) -> Path:
    """Resolve an explicit path or an unambiguous catalog name/ID."""
    candidate = Path(value).expanduser()
    if candidate.exists():
        return candidate.resolve()
    token = str(value)
    rows = list_manifests(config)
    matches = [
        row
        for row in rows
        if token == row["id"]
        or token == Path(row["path"]).stem
        or (row.get("name") and token == row["name"])
    ]
    paths = {row["path"] for row in matches}
    if len(paths) > 1:
        raise ValueError(f"Batch selector {token!r} is ambiguous; use a manifest path or ID")
    if paths:
        return Path(next(iter(paths)))
    return candidate.resolve()
