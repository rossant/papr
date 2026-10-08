"""Command handling for persistent export batches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .artifacts import locate
from .batches import export_batch, load_manifest, save_manifest
from .config import Config


def run(argv: list[str], config: Config) -> int:
    parser = argparse.ArgumentParser(prog="papr batch", description="Inspect or re-export a batch.")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("show", "check", "export"):
        command = commands.add_parser(action)
        command.add_argument("manifest", type=Path)
        if action == "export":
            command.add_argument("-o", "--output", type=Path)
            command.add_argument("--overwrite", action="store_true")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--manifest", type=Path, dest="new_manifest")
            command.add_argument("--exclude-delivered", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.action == "show":
            print(json.dumps(load_manifest(args.manifest), indent=2, ensure_ascii=False))
        elif args.action == "check":
            checked = 0
            for row in load_manifest(args.manifest)["items"]:
                if row.get("artifacts") or row.get("pending_artifacts"):
                    for artifact in [*row.get("artifacts", []), *row.get("pending_artifacts", [])]:
                        found = locate(Path(artifact["path"]), artifact["sha256"], config)
                        print(f"✓ {found}")
                        checked += 1
            print(f"{checked} artifact(s) verified")
        else:
            rows = export_batch(
                args.manifest,
                args.output or config.download_dir,
                config,
                exclude=args.exclude_delivered,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
            for row in rows:
                if row["status"] == "error":
                    print(f"✗ {row.get('error', 'Export failed')}", file=sys.stderr)
                if row["status"] == "excluded":
                    print(f"− {row['article']['title']} — already delivered")
                for path in row.get("outputs", []) if row["status"] != "excluded" else []:
                    print(f"✓ {path}")
            if not args.dry_run:
                print(f"Batch manifest: {save_manifest(rows, config, args.new_manifest)}")
            return 1 if any(row["status"] == "error" for row in rows) else 0
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"✗ Batch: {exc}", file=sys.stderr)
        return 1
