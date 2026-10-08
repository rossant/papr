"""Local PDF compression command, separate from resolution and retrieval."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .artifacts import atomic_copy as _atomic_pdf_copy
from .config import Config
from .progress import Reporter, emit, pause


def _compress_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="papr compress", description="Compress local PDFs.")
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF files to compress")
    parser.add_argument(
        "-i", "--in-place", action="store_true", help="replace the original if smaller"
    )
    parser.add_argument("-o", "--output", type=Path, help="directory for compressed copies")
    parser.add_argument(
        "--dpi", type=int, help="color/gray image resolution (default: personal setting)"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace existing compressed copies"
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress and summary")
    parser.add_argument("--verbose", action="store_true", help="show diagnostic details")
    return parser


def _run_compress(args: argparse.Namespace, config: Config) -> int:
    from .compression import compress_pdf

    if args.in_place and args.output:
        print("✗ --in-place cannot be combined with --output", file=sys.stderr)
        return 1
    try:
        if args.dpi is not None:
            config = replace(config, pdf_compression_dpi=args.dpi)
    except ValueError as exc:
        print(f"✗ Compression: {exc}", file=sys.stderr)
        return 1
    failures = 0
    with Reporter(enabled=not args.quiet) as reporter:
        reporter.start_batch(len(args.inputs))
        for input_path in args.inputs:
            source = input_path.expanduser().resolve()
            reporter.start_item(str(source))
            status = "error"
            try:
                emit("compress", "Checking PDF")
                if not source.is_file() or source.suffix.lower() != ".pdf":
                    raise ValueError(f"PDF not found: {source}")
                prepared, result = compress_pdf(source, config, enabled=True)
                if result["status"] == "failed":
                    raise RuntimeError(result.get("reason", "Compression failed"))
                if result.get("reason") == "unreadable PDF":
                    raise ValueError("Unreadable or corrupt PDF")
                if result["saved_bytes"] > 0:
                    destination = (
                        source
                        if args.in_place
                        else (
                            (args.output.expanduser() if args.output else source.parent)
                            / f"{source.stem}.compressed.pdf"
                        )
                    )
                    if destination.exists() and not args.in_place and not args.overwrite:
                        raise FileExistsError(f"{destination} already exists; use --overwrite")
                    emit("export", "Saving compressed PDF")
                    _atomic_pdf_copy(
                        prepared, destination, overwrite=args.in_place or args.overwrite
                    )
                    with pause():
                        print(
                            f"✓ {destination} — {result['original_bytes']:,} → "
                            f"{result['output_bytes']:,} bytes "
                            f"({result['savings_percent']:g}% smaller)"
                        )
                else:
                    with pause():
                        print(f"✓ {source} — unchanged: {result.get('reason', 'already compact')}")
                status = "ok"
            except Exception as exc:
                failures += 1
                logging.getLogger(__name__).debug("PDF compression failed", exc_info=True)
                with pause():
                    print(f"✗ {source}: {exc}", file=sys.stderr)
            finally:
                reporter.finish_item(status)
        directory = args.output.expanduser() if args.output else args.inputs[0].expanduser().parent
        reporter.summary(failures, directory.resolve())
    return 1 if failures else 0
