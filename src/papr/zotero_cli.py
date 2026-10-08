"""Zotero import command orchestration, independent from the main argument router."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .model import Article
from .progress import Reporter, emit, pause


def parser(add_source_flags: Callable[[argparse.ArgumentParser], None]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="papr zotero", description="Save papers to Zotero.")
    parser.add_argument("action", nargs="?", default="last", choices=["add", "last"])
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF to import (for add)")
    parser.add_argument("--collection", help="override your personal default collection")
    parser.add_argument("--no-compress", action="store_true", help="import the original PDF")
    parser.add_argument(
        "--non-interactive", action="store_true", help="do not ask for authorization"
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without changing Zotero")
    add_source_flags(parser)
    return parser


def run(
    args: argparse.Namespace,
    config: Config,
    *,
    resolve_item: Callable,
    article_line: Callable[[Article], str],
) -> int:
    from .compression import compress_pdf
    from .history import load_latest
    from .zotero import save

    if args.action == "add" and args.pdf is None:
        print("✗ Specify a PDF: papr zotero add paper.pdf", file=sys.stderr)
        return 1
    if args.action == "last" and args.pdf is not None:
        print("✗ papr zotero last takes no PDF path", file=sys.stderr)
        return 1
    interactive = not args.non_interactive and sys.stdin.isatty()
    with Reporter(enabled=not args.quiet) as reporter:
        reporter.start_batch(1)
        reporter.start_item("Latest downloaded paper" if args.action == "last" else str(args.pdf))
        status = "error"
        try:
            emit("resolve", "Loading latest download" if args.action == "last" else "Reading PDF")
            if args.action == "last":
                article, pdf = load_latest(config)
            else:
                pdf = args.pdf.expanduser().resolve()
                if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
                    raise ValueError(f"PDF not found: {pdf}")
                article, _ = resolve_item(
                    pdf,
                    config,
                    interactive,
                    use_local=not args.no_local,
                    use_remote=not args.local_only,
                    **({"enrich": True} if args.enrich else {}),
                )
            reporter.label = article_line(article)
            collection = args.collection or config.zotero_collection
            if args.dry_run:
                with pause():
                    print(f"Would save {article_line(article)}")
                    print(f"  PDF: {pdf}")
                    if config.zotero_group_id is not None:
                        from .zotero import library_info

                        target = library_info(config)
                        library = f"{target['library']} (group {target['group_id']})"
                    else:
                        library = "My Library"
                    print(f"  Library: {library}")
                    if collection:
                        print(f"  Collection: {collection}")
                status = "dry-run"
                return 0
            prepared, _ = compress_pdf(pdf, config, enabled=False if args.no_compress else None)
            with tempfile.TemporaryDirectory(prefix="papr-zotero-") as directory:
                upload_pdf = prepared
                if prepared.name != pdf.name:
                    upload_pdf = Path(directory) / pdf.name
                    shutil.copy2(prepared, upload_pdf)
                result = save(
                    article, upload_pdf, config, collection=collection, interactive=interactive
                )
            with pause():
                print(f"✓ Zotero: {result['status']} — {article.title}")
                print(f"  Library: {result.get('library') or 'My Library'}")
                if result.get("collection"):
                    print(f"  Collection: {result['collection']}")
            status = "ok"
            return 0
        except Exception as exc:
            logging.getLogger(__name__).debug("Zotero saving failed", exc_info=True)
            with pause():
                print(f"✗ Zotero: {exc}", file=sys.stderr)
            return 1
        finally:
            reporter.finish_item(status)
