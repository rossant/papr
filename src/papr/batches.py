"""Versioned export manifests and offline re-export of verified artifacts."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .artifacts import atomic_json, checksum, locate, remember
from .config import Config
from .history import _article, record_download
from .model import Article


def identity(article: Article) -> str:
    if article.doi:
        from .resolvers.common import extract_doi

        doi = extract_doi(article.doi) or article.doi
        return "doi:" + doi.casefold()
    if article.pmid:
        return "pmid:" + article.pmid
    if article.source_key:
        return f"source:{article.source_library or article.source or ''}:{article.source_key}"
    # Titles alone cannot safely identify two editions or similarly named works.
    return ""


def identities(article: Article) -> set[str]:
    """Keep aliases so enrichment does not change whether a work was delivered."""
    keys = set()
    if article.doi:
        keys.add(identity(article))
    if article.pmid:
        keys.add("pmid:" + article.pmid)
    if article.source_key:
        keys.add(f"source:{article.source_library or article.source or ''}:{article.source_key}")
    return keys


def _has_exports(row: dict) -> bool:
    return row["status"] in {"ok", "error"} and (
        row["status"] == "ok" or row.get("export_status") in {"ok", "partial"}
    )


def save_manifest(rows: list[dict], config: Config, path: Path | None = None) -> Path:
    destination = path or config.data_dir / "batches" / f"{uuid4().hex}.json"
    destination = destination.expanduser().resolve()
    atomic_json(
        destination,
        {
            "schema": "papr-export-batch",
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "items": rows,
        },
    )
    return destination


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema") != "papr-export-batch"
            or type(data.get("version")) is not int
            or data["version"] != 1
            or not isinstance(data.get("items"), list)
        ):
            raise ValueError("unsupported manifest schema")
        for row in data["items"]:
            if not isinstance(row, dict) or row.get("status") not in {
                "ok",
                "error",
                "dry-run",
                "excluded",
            }:
                raise ValueError("invalid manifest item")
            if "export_status" in row and row["export_status"] not in {
                "ok",
                "partial",
                "error",
                "dry-run",
            }:
                raise ValueError("invalid export status")
            for field in ("artifacts", "pending_artifacts"):
                if not isinstance(row.get(field, []), list):
                    raise ValueError("invalid artifact list")
            if (row.get("artifacts") or row.get("pending_artifacts")) and "article" not in row:
                raise ValueError("missing article metadata")
            if "article" in row:
                _article(row["article"])
            for artifact in [*row.get("artifacts", []), *row.get("pending_artifacts", [])]:
                if not isinstance(artifact, dict):
                    raise ValueError("invalid artifact")
                name, stored = artifact.get("name"), artifact.get("path")
                if (
                    not isinstance(name, str)
                    or not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                    or "\x00" in name
                    or not isinstance(stored, str)
                    or "\x00" in stored
                    or not Path(stored).is_absolute()
                ):
                    raise ValueError("invalid artifact path")
                from .artifacts import cache_path

                cache_path(artifact.get("sha256"), Config())
        return data
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise ValueError(f"Cannot read batch manifest {path}: {exc}") from exc


def delivered_identities(paths: list[Path]) -> set[str]:
    delivered = set()
    for path in paths:
        for row in load_manifest(path)["items"]:
            if (
                _has_exports(row)
                and "article" in row
                and any(
                    artifact["name"].lower().endswith(".pdf")
                    for artifact in row.get("artifacts", [])
                )
            ):
                delivered.update(identities(_article(row["article"])))
    return delivered


def export_batch(
    manifest: Path,
    output: Path,
    config: Config,
    *,
    exclude: list[Path] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """Preflight every checksum before copying; identical destinations are reused."""
    data = load_manifest(manifest)
    excluded = delivered_identities(exclude or [])
    plan = []
    for original in data["items"]:
        if original["status"] in {"excluded", "dry-run"}:
            continue
        available = [*original.get("artifacts", []), *original.get("pending_artifacts", [])]
        if not (_has_exports(original) or original.get("pending_artifacts")) or not available:
            continue
        row = dict(original)
        article = _article(row["article"])
        if identities(article) & excluded:
            row.update(
                status="excluded", exclusion_reason="already delivered", artifacts=[], outputs=[]
            )
            row.pop("export_status", None)
            row.pop("pending_artifacts", None)
            plan.append((row, []))
            continue
        files = [
            (artifact, locate(Path(artifact["path"]), artifact["sha256"], config))
            for artifact in available
        ]
        plan.append((row, files))

    output = output.expanduser().resolve()
    rows = []
    reserved: dict[Path, str] = {}
    for row, files in plan:
        if row["status"] == "excluded":
            rows.append(row)
            continue
        artifacts = []
        try:
            for artifact, source in files:
                base = output / artifact["name"]
                destination = base
                counter = 2
                while True:
                    reserved_digest = reserved.get(destination)
                    identical = reserved_digest == artifact["sha256"] or (
                        reserved_digest is None
                        and destination.is_file()
                        and checksum(destination) == artifact["sha256"]
                    )
                    occupied = destination.exists() and not overwrite
                    if identical or (reserved_digest is None and not occupied):
                        break
                    destination = base.with_name(f"{base.stem}_{counter}{base.suffix}")
                    counter += 1
                if not identical and not dry_run:
                    output.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(dir=output) as directory:
                        temporary = Path(directory) / "artifact"
                        shutil.copyfile(source, temporary)
                        if checksum(temporary) != artifact["sha256"]:
                            raise ValueError(f"Artifact changed during export: {source}")
                        if overwrite:
                            temporary.replace(destination)
                        else:
                            destination.hardlink_to(temporary)
                reserved[destination] = artifact["sha256"]
                artifacts.append({**artifact, "path": str(destination), "name": destination.name})
        except (OSError, ValueError) as exc:
            row.update(
                status="error",
                export_status="partial" if artifacts else "error",
                error=str(exc),
                artifacts=artifacts,
                pending_artifacts=[a for a, _ in files[len(artifacts) :]],
                outputs=[a["path"] for a in artifacts],
            )
            rows.append(row)
            continue
        row.update(
            status="dry-run" if dry_run else "ok",
            export_status="dry-run" if dry_run else "ok",
            artifacts=artifacts,
            outputs=[artifact["path"] for artifact in artifacts],
        )
        row.pop("pending_artifacts", None)
        row.pop("error", None)
        row.pop("zotero", None)
        row.pop("failed_stage", None)
        if not dry_run:
            pdf = next((a for a in artifacts if a["name"].lower().endswith(".pdf")), None)
            if pdf:
                try:
                    record_download(_article(row["article"]), Path(pdf["path"]), config)
                except (OSError, ValueError) as exc:
                    row.update(status="error", error=f"Cannot record download: {exc}")
        rows.append(row)
    return rows


def record_artifacts(article: Article, outputs: list[Path], config: Config) -> dict:
    return {
        "article": article.to_dict(),
        "identity": identity(article),
        "artifacts": [remember(path, config) for path in outputs],
    }
