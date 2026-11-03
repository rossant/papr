"""Argument definitions shared by the main CLI and its command modules."""

from __future__ import annotations

import argparse
from pathlib import Path

from .export import FORMAT_SUFFIX


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("jobs must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("jobs must be a positive integer")
    return number


def _formats(value: str) -> list[str]:
    items = [x.strip().lower() for x in value.split(",") if x.strip()]
    if not items:
        raise argparse.ArgumentTypeError("at least one output format is required")
    unknown = sorted(set(items) - set(FORMAT_SUFFIX))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown format(s): {', '.join(unknown)}")
    return items


def _add_source_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--quiet", action="store_true", help="suppress progress and summary")
    parser.add_argument("--verbose", action="store_true", help="show diagnostic details")
    parser.add_argument(
        "--enrich", action="store_true", help="enrich local metadata with remote sources"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--local-only",
        action="store_true",
        help="resolve only against local Zotero/Zolit sources",
    )
    group.add_argument(
        "--no-local",
        action="store_true",
        help="ignore Zotero/Zolit and use remote resolvers only",
    )


def _get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="papr",
        description="Fetch and convert scholarly papers.",
        epilog=(
            "Commands: resolve, batch, cache, doctor, compress, zotero, config, sources. "
            "Use papr COMMAND --help."
        ),
    )
    p.add_argument(
        "inputs", nargs="+", help="reference, DOI, PMID, URL, PDF, .txt, .bib, or CSL-JSON"
    )
    p.add_argument(
        "-f",
        "--format",
        type=_formats,
        dest="formats",
        help="comma-separated: pdf,md,txt,bib,bibtex,csl,json",
    )
    p.add_argument("-o", "--output", type=Path, help="output directory (default: ~/Downloads)")
    p.add_argument("--filename", help="override filename template")
    p.add_argument("--processor", choices=["auto", "mistral", "native"], help="Markdown processor")
    p.add_argument("--oa-only", action="store_true", help="never use institutional browser access")
    p.add_argument(
        "--show-browser", action="store_true", help="show Chrome during institutional PDF retrieval"
    )
    p.add_argument(
        "--jobs",
        type=_positive_int,
        help="concurrent OA fetches (default: config jobs, 4); "
        "interactive, dry-run and rename runs are sequential",
    )
    p.add_argument("--refresh", action="store_true", help="ignore cached PDF")
    p.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    p.add_argument(
        "--non-interactive", action="store_true", help="fail instead of asking on ambiguity"
    )
    p.add_argument(
        "--rename",
        action="store_true",
        help="for a local PDF, rename it in place using the canonical filename",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="resolve and show planned outputs without fetching"
    )
    p.add_argument("--report", type=Path, help="write a JSON batch report")
    p.add_argument("--manifest", type=Path, help="save the persistent batch manifest at this path")
    p.add_argument("--batch-name", help="name this batch for later lookup")
    p.add_argument(
        "--exclude-delivered",
        type=Path,
        action="append",
        default=[],
        metavar="MANIFEST",
        help="exclude works with successful PDF exports in a previous manifest (repeatable)",
    )
    p.add_argument(
        "--zotero", action="store_true",
        help="save the paper and PDF to Zotero; save metadata if the PDF is unavailable",
    )
    p.add_argument("--collection", help="Zotero collection name or key (with --zotero)")
    p.add_argument("--no-compress", action="store_true", help="export the original PDF")
    p.add_argument(
        "--item-type",
        action="append",
        default=[],
        metavar="CSL_TYPE",
        help="export only these bibliographic types (repeatable, e.g. article-journal)",
    )
    _add_source_flags(p)
    return p


def _resolve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr resolve", description="Resolve a scholarly reference.")
    p.add_argument("query", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--item-type", action="append", default=[], metavar="CSL_TYPE")
    _add_source_flags(p)
    return p


def _login_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr login")
    p.add_argument("institution", choices=["ucl"])
    return p


def _logout_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr logout")
    p.add_argument("institution", choices=["ucl"])
    return p


def _config_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr config")
    p.add_argument("action", choices=["path", "init", "set"])
    p.add_argument("key", nargs="?", help="personal setting, e.g. zotero.collection")
    p.add_argument("value", nargs="?", help="new value")
    return p


def _sources_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr sources", description="Inspect local sources.")
    p.add_argument("--check", action="store_true", help="probe access and index freshness")
    p.add_argument("--json", action="store_true", help="print machine-readable diagnostics")
    return p
