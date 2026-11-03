"""Selective remote acquisition retries using the ordinary fetch pipeline."""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from .artifacts import atomic_json
from .batches import identities, save_manifest
from .cli_arguments import _get_parser
from .config import Config
from .get_cli import process_items
from .get_pipeline import GetPipeline, GetServices
from .http import http_session
from .locking import state_lock
from .model import Article, article_from_dict
from .progress import Reporter
from .resolvers.common import extract_doi


def retry_input(row: dict) -> str | Article | None:
    """Saved file imports and completed PDF publication never trigger acquisition."""
    if row["status"] != "error" or row.get("failed_stage") not in {"resolve", "fetch"}:
        return None
    if any(
        artifact["name"].lower().endswith(".pdf")
        for artifact in row.get("artifacts", [])
    ) or any(str(value).lower().endswith(".pdf") for value in row.get("outputs", [])):
        return None
    if "article" in row:
        article = article_from_dict(row["article"])
        article.local_pdf = None
        return article
    value = row.get("input")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    # Pass references straight to resolution, never through load_input(). Reject
    # obvious saved file paths rather than letting a resolver reinterpret them.
    path = Path(value).expanduser()
    if (
        value.startswith(("/", "~", "./", "../", "file:"))
        or path.suffix.lower() in {".pdf", ".txt", ".refs", ".bib", ".json"}
    ) and not value.startswith(("http://", "https://")) and not extract_doi(value):
        return None
    return value


def _resolver(services: GetServices):
    def resolve(item, config, interactive, **options):
        if isinstance(item, Article) and not (item.doi or item.pmid):
            # A title/source key alone cannot authorize choosing a remote work.
            return deepcopy(item), None
        article, _ = services.resolve_item(item, config, interactive, **options)
        if isinstance(item, Article):
            for field in ("doi", "pmid"):
                expected, actual = getattr(item, field), getattr(article, field)
                if expected and actual:
                    if field == "doi":
                        expected = extract_doi(expected) or expected.casefold()
                        actual = extract_doi(actual) or actual.casefold()
                    if expected != actual:
                        raise ValueError("Retry resolution changed the saved article identity")
            if identities(item) and not identities(item).intersection(identities(article)):
                raise ValueError("Retry resolution did not preserve the saved article identity")
            article.merge(item)
        article.local_pdf = None
        return article, None

    return resolve


def _merge(original: dict, fresh: dict) -> dict:
    result = {**original, **fresh, "input": original.get("input", fresh["input"])}
    result["acquisition_attempts"] = [
        *original.get("acquisition_attempts", []), *fresh.get("acquisition_attempts", [])
    ]
    for field in ("artifacts", "pending_artifacts", "outputs"):
        combined = list(original.get(field, []))
        combined.extend(value for value in fresh.get(field, []) if value not in combined)
        if combined:
            result[field] = combined
    if fresh["status"] == "ok":
        for field in ("error", "failed_stage", "pdf_status"):
            result.pop(field, None)
    return result


def _arguments(args: argparse.Namespace, output: Path) -> argparse.Namespace:
    options = _get_parser().parse_args(["retry"])
    for name in ("show_browser", "oa_only", "jobs", "non_interactive", "overwrite"):
        setattr(options, name, getattr(args, name))
    options.output = output
    options.formats = args.formats or ["pdf"]
    options.no_local = options.enrich = True
    return options


def _validate(args: argparse.Namespace, source: Path) -> None:
    if set(args.formats or []) & {"md", "txt"}:
        raise ValueError("Batch retry does not run processors; use papr to request md/txt outputs")
    if args.name is not None and (
        not args.name.strip() or args.name != args.name.strip() or args.name in {".", ".."}
        or any(char in args.name for char in ("/", "\\", "\x00"))
    ):
        raise ValueError("Batch name must be a non-empty label, not a path")
    report = args.report.expanduser().resolve() if args.report else None
    target = args.new_manifest.expanduser().resolve() if args.new_manifest else None
    if report is not None and report in {source.resolve(), target}:
        raise ValueError("Report path must differ from the input and result manifests")
    for path in (report, target):
        if path is not None and path.exists() and not path.is_file():
            raise ValueError(f"Expected a file path: {path}")
    if args.output and args.output.exists() and not args.output.is_dir():
        raise ValueError(f"Expected an output directory: {args.output}")


def run(
    args: argparse.Namespace,
    data: dict,
    source: Path,
    output: Path,
    config: Config,
    services: GetServices,
) -> int:
    """Keep a loaded snapshot so an explicitly reused manifest path is safe."""
    _validate(args, source)
    rows = deepcopy(data["items"])
    for row in rows:
        if not isinstance(row.get("outputs", []), list) or any(
            not isinstance(value, str) for value in row.get("outputs", [])
        ):
            raise ValueError("Invalid output list in retry manifest")
        if not isinstance(row.get("acquisition_attempts", []), list):
            raise ValueError("Invalid acquisition history in retry manifest")
    selected = [(index, item) for index, row in enumerate(rows)
                if (item := retry_input(row)) is not None]
    if args.dry_run:
        for index, item in selected:
            label = item.doi or item.title if isinstance(item, Article) else item
            print(f"Would retry item {index + 1}: {label}")
        print(f"{len(selected)} item(s) would be retried; {len(rows) - len(selected)} retained")
        return 0
    options = _arguments(args, output)
    interactive = not options.non_interactive and sys.stdin.isatty()
    with http_session(), Reporter() as reporter, state_lock(config):
        pipeline = GetPipeline(
            options, config, reporter, replace(services, resolve_item=_resolver(services)),
            set(), interactive,
        )
        fresh = process_items(pipeline, [item for _, item in selected])
        for (index, _), row in zip(selected, fresh, strict=True):
            rows[index] = _merge(rows[index], row)
        failures = sum(row["status"] == "error" for row in rows)
        reporter.summary(failures, output)
        if args.report:
            atomic_json(args.report.expanduser(), rows)
        saved = save_manifest(
            rows, config, args.new_manifest, name=args.name or data.get("name"),
            output_dir=output,
        )
        print(f"Batch manifest: {saved}")
    return 1 if failures else 0
