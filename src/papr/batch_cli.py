"""Command handling for persistent export batches."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from .artifacts import locate
from .batch_catalog import list_manifests, manifest_summary, resolve_manifest
from .batches import export_batch, load_manifest, save_manifest
from .config import Config
from .locking import state_lock


def run(argv: list[str], config: Config) -> int:
    parser = argparse.ArgumentParser(prog="papr batch", description="Inspect or re-export a batch.")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List known batches")
    commands.choices["list"].add_argument("--json", action="store_true", dest="as_json")
    for action in ("show", "summary", "check", "export", "resume"):
        command = commands.add_parser(action)
        command.add_argument("manifest", help="manifest path, batch name, or batch ID")
        if action in {"export", "resume"}:
            command.add_argument("-o", "--output", type=Path)
            command.add_argument("--overwrite", action="store_true")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--manifest", type=Path, dest="new_manifest")
            command.add_argument("--name", help="name the saved result batch")
            command.add_argument("--exclude-delivered", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            batches = list_manifests(config)
            if args.as_json:
                print(json.dumps(batches, indent=2, ensure_ascii=False))
            elif not batches:
                print("No batches found.")
            else:
                for batch in batches:
                    label = f"{batch['name']} ({batch['id']})" if batch.get("name") else batch["id"]
                    print(
                        f"{label}: {batch['available']} available, {batch['missing']} missing, "
                        f"{batch['changed']} changed, {batch['unreadable']} unreadable, "
                        f"{batch['incomplete']} incomplete of {batch['total']} item(s) — "
                        f"{batch['path']}"
                    )
        elif args.action == "show":
            print(
                json.dumps(
                    load_manifest(resolve_manifest(args.manifest, config)),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif args.action == "summary":
            data = load_manifest(resolve_manifest(args.manifest, config))
            summary = manifest_summary(data, config)
            print(
                f"Batch {data.get('name') or Path(args.manifest).name}: "
                f"{summary['available']} available, {summary['missing']} missing, "
                f"{summary['changed']} changed, {summary['unreadable']} unreadable, "
                f"{summary['incomplete']} incomplete of {summary['total']} item(s)."
            )
            for item in summary["items"]:
                print(
                    f"  {item['availability']}: {item['title']} "
                    f"({item['available_artifacts']}/{item['total_artifacts']} artifacts; "
                    f"{item['status']})"
                )
        elif args.action == "check":
            checked = 0
            for row in load_manifest(resolve_manifest(args.manifest, config))["items"]:
                if row.get("artifacts") or row.get("pending_artifacts"):
                    for artifact in [*row.get("artifacts", []), *row.get("pending_artifacts", [])]:
                        found = locate(Path(artifact["path"]), artifact["sha256"], config)
                        print(f"✓ {found}")
                        checked += 1
            print(f"{checked} artifact(s) verified")
        else:
            manifest_path = resolve_manifest(args.manifest, config)
            data = load_manifest(manifest_path)
            output = args.output
            if output is None and args.action == "resume":
                output = _resume_output(data, config)
            output = output or config.download_dir
            with nullcontext() if args.dry_run else state_lock(config):
                rows = export_batch(
                    manifest_path,
                    output,
                    config,
                    exclude=[resolve_manifest(value, config) for value in args.exclude_delivered],
                    overwrite=args.overwrite,
                    dry_run=args.dry_run,
                    resume=args.action == "resume",
                )
                for row in rows:
                    if row["status"] == "error":
                        print(f"✗ {row.get('error', 'Export failed')}", file=sys.stderr)
                    if row["status"] == "excluded":
                        print(f"− {row['article']['title']} — already delivered")
                    for path in row.get("outputs", []) if row["status"] != "excluded" else []:
                        print(f"✓ {path}")
                if not args.dry_run:
                    saved_manifest = save_manifest(
                        rows,
                        config,
                        args.new_manifest,
                        name=args.name or data.get("name"),
                        output_dir=output,
                    )
                    print(f"Batch manifest: {saved_manifest}")
            return 1 if any(row["status"] == "error" for row in rows) else 0
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"✗ Batch: {exc}", file=sys.stderr)
        return 1


def _resume_output(data: dict, config: Config) -> Path:
    stored = data.get("output_dir")
    if stored:
        return Path(stored)
    output_paths = [
        Path(artifact["path"]).parent
        for row in data["items"]
        for artifact in row.get("artifacts", [])
        if row.get("status") == "error"
    ]
    if output_paths:
        return output_paths[0]
    return config.download_dir
