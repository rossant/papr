"""Command-line interface for inspecting and pruning papr caches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cache import prune, status
from .config import Config


def run(argv: list[str], config: Config) -> int:
    parser = argparse.ArgumentParser(prog="papr cache", description="Inspect or prune papr caches.")
    commands = parser.add_subparsers(dest="action", required=True)
    status_parser = commands.add_parser("status", help="show cache sizes")
    status_parser.add_argument("--json", action="store_true")
    prune_parser = commands.add_parser("prune", help="remove old orphaned artifact cache files")
    prune_parser.add_argument("--dry-run", action="store_true")
    prune_parser.add_argument(
        "--manifest", type=Path, action="append", default=[], metavar="PATH",
        help="protect references in an additional manifest (repeatable)",
    )
    prune_parser.add_argument(
        "--older-than", type=float, default=7, metavar="DAYS",
        help="only prune artifacts at least this many days old (default: 7; 0 disables grace)",
    )
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            result = status(config)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(f"Cache: {result['cache_dir']}")
                for name, size in result["categories"].items():
                    print(f"{name:12} {size:>12,} bytes")
                print(f"{'total':12} {result['total_bytes']:>12,} bytes")
            return 0
        if args.older_than < 0:
            raise ValueError("--older-than must be zero or greater")
        paths = prune(
            config,
            dry_run=args.dry_run,
            manifests=args.manifest,
            older_than_days=args.older_than,
        )
        verb = "Would remove" if args.dry_run else "Removed"
        for path in paths:
            print(f"{verb}: {path}")
        if not paths:
            print("No orphaned artifacts eligible for pruning.")
        print(f"{len(paths)} artifact(s) {'eligible' if args.dry_run else 'pruned'}")
        return 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(f"✗ Cache: {exc}", file=sys.stderr)
        return 1
