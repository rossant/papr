"""Command-line interface for inspecting and pruning papr caches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from .cache import prune, status
from .config import Config


def run(argv: list[str], config: Config) -> int:
    parser = argparse.ArgumentParser(prog="papr cache", description="Inspect or prune papr caches.")
    commands = parser.add_subparsers(dest="action", required=True)
    status_parser = commands.add_parser("status", help="show cache sizes")
    status_parser.add_argument("--json", action="store_true")
    import_parser = commands.add_parser("import", help="verify and cache an original PDF source")
    import_parser.add_argument("doi")
    import_parser.add_argument("pdf", type=Path)
    import_parser.add_argument("--pages", help="inclusive PDF source pages, e.g. 99-103")
    import_parser.add_argument("--source-url", help="public archive record URL for provenance")
    discover_parser = commands.add_parser(
        "discover", help="find possible public archive or thesis records for a DOI"
    )
    discover_parser.add_argument("doi")
    discover_parser.add_argument("--json", action="store_true")
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
        if args.action == "import":
            from .pdf_source import import_pdf

            result = import_pdf(
                args.doi, args.pdf, config, pages=args.pages, source_url=args.source_url
            )
            print(f"Verified original PDF: {result['title']} ({result['output_pages']} pages)")
            print(f"Cached: {result['cache_path']}")
            return 0
        if args.action == "discover":
            from .archive_discovery import discover

            try:
                results = discover(args.doi, config)
            except httpx.HTTPError:
                # HTTP errors may include signed URLs; keep diagnostics secret-free.
                raise ValueError("Archive discovery request failed") from None
            if args.json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            elif not results:
                print("No possible archive records found.")
            else:
                print(
                    "Possible records (unverified; inspect the record and select pages explicitly):"
                )
                for index, item in enumerate(results, 1):
                    print(f"{index}. [{item['type']}] {item['title']}")
                    print(f"   {item['url']}")
                    print(f"   Why: {item['reason']} — {item['verification']}")
            return 0
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
    except (ValueError, OSError, KeyError, ImportError, LookupError) as exc:
        print(f"✗ Cache: {exc}", file=sys.stderr)
        return 1
