"""Private local index for named and externally stored batch manifests."""

from __future__ import annotations

import json
from pathlib import Path

from .artifacts import atomic_json
from .config import Config

_SCHEMA = "papr-batch-catalog"


def _catalog_path(config: Config) -> Path:
    return config.data_dir / "batches" / ".catalog.json"


def _read(config: Config) -> list[dict]:
    path = _catalog_path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema") != _SCHEMA
            or data.get("version") != 1
            or not isinstance(data.get("manifests"), list)
        ):
            raise ValueError("unsupported registry schema")
        rows = data["manifests"]
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("path"), str)
                or not Path(row["path"]).is_absolute()
                or "name" not in row
                or (row.get("name") is not None and not _valid_name(row["name"]))
                or not isinstance(row.get("id"), str)
                or not row["id"]
            ):
                raise ValueError("invalid registry record")
        return rows
    except (OSError, ValueError, TypeError) as exc:
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


def register_manifest(path: Path, config: Config, *, name: str | None = None) -> None:
    if name is not None and not _valid_name(name):
        raise ValueError("Batch name must be a non-empty label, not a path")
    resolved = path.expanduser().resolve()
    rows = [row for row in _read(config) if Path(row["path"]) != resolved]
    rows.append({"id": resolved.stem, "name": name, "path": str(resolved)})
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
                str(resolved), {"id": resolved.stem, "name": None, "path": str(resolved)}
            )
    manifests = []
    from .batches import load_manifest

    for record in records.values():
        path = Path(record["path"])
        if not path.is_file():
            continue
        data = load_manifest(path)
        items = data["items"]
        available = sum(bool(row.get("artifacts") or row.get("pending_artifacts")) for row in items)
        missing = sum(
            row.get("status") == "error"
            and not (row.get("artifacts") or row.get("pending_artifacts"))
            for row in items
        )
        incomplete = sum(
            row.get("status") == "error" or row.get("export_status") == "partial" for row in items
        )
        manifests.append(
            {
                **record,
                "created_at": data.get("created_at"),
                "total": len(items),
                "available": available,
                "missing": missing,
                "incomplete": incomplete,
            }
        )
    return sorted(manifests, key=lambda row: (row.get("created_at") or "", row["id"]))


def known_manifest_paths(config: Config) -> list[Path]:
    """All manifests that may protect cached artifacts, including external paths."""
    records = {row["path"] for row in _read(config)}
    folder = config.data_dir / "batches"
    if folder.is_dir():
        records.update(
            str(path.resolve()) for path in folder.glob("*.json") if not path.name.startswith(".")
        )
    return [Path(value) for value in sorted(records)]


def resolve_manifest(value: str | Path, config: Config) -> Path:
    """Resolve an explicit path or an unambiguous catalog name/ID."""
    candidate = Path(value).expanduser()
    if candidate.exists():
        return candidate.resolve()
    token = str(value)
    rows = list_manifests(config)
    matches = [
        row for row in rows if token == row["id"] or (row.get("name") and token == row["name"])
    ]
    paths = {row["path"] for row in matches}
    if len(paths) > 1:
        raise ValueError(f"Batch selector {token!r} is ambiguous; use a manifest path or ID")
    if paths:
        return Path(next(iter(paths)))
    return candidate.resolve()
