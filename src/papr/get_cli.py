"""Fetch-command batch lifetime, progress, and persistent reports."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter

from .artifacts import atomic_json
from .batch_catalog import resolve_manifest
from .batches import delivered_identities, save_manifest
from .config import Config
from .get_pipeline import GetPipeline, GetServices
from .http import http_session
from .input import load_input
from .locking import state_lock
from .model import Article
from .prefetch import Prefetch, PreparedItem
from .progress import Reporter, emit, pause
from .resolvers.common import extract_doi, extract_pmid, extract_year


def _input_values(values: list[str]) -> list[str]:
    """Keep an unquoted author/year citation together without merging batch inputs."""
    if len(values) < 2:
        return values
    years = [value for value in values if value.isdigit() and extract_year(value)]
    if len(years) != 1:
        return values
    for value in values:
        path = Path(value).expanduser()
        if (
            any(char.isspace() for char in value)
            or path.exists()
            or path.suffix.lower() in {".pdf", ".txt", ".refs", ".bib", ".json"}
            or extract_doi(value)
            or extract_pmid(value)
            or "://" in value
        ):
            return values
    return [" ".join(values)]


def _load_inputs(
    values: list[str],
    config: Config,
    report: list[dict],
) -> list[str | Article | Path]:
    items: list[str | Article | Path] = []
    for value in _input_values(values):
        started = perf_counter()
        try:
            emit("input", f"Loading {value}")
            items.extend(load_input(value, config))
        except Exception as exc:
            logging.getLogger(__name__).debug("Input loading failed: %s", value, exc_info=True)
            with pause():
                print(f"✗ {value}: {exc}", file=sys.stderr)
            duration = perf_counter() - started
            report.append(
                {
                    "input": value,
                    "status": "error",
                    "error": str(exc),
                    "failed_stage": "input",
                    "duration": duration,
                    "timings": {"input": duration},
                }
            )
    return items


def _process_item(
    pipeline: GetPipeline,
    item: str | Article | Path,
    prepared: PreparedItem | None,
    prefetch: Prefetch | None,
) -> dict:
    label = str(item) if not isinstance(item, Article) else (item.doi or item.title)
    row: dict = {"input": label}
    reporter = pipeline.reporter
    reporter.start_item(label)
    try:
        pipeline.process(item, row, prepared, prefetch)
    except Exception as exc:
        logging.getLogger(__name__).debug("Processing failed: %s", label, exc_info=True)
        with pause():
            print(f"✗ {label}: {exc}", file=sys.stderr)
            for path in row.get("outputs", []):
                print(f"  Saved: {path}", file=sys.stderr)
        row.update(status="error", error=str(exc))
    finally:
        if prefetch is not None:
            prefetch.release(prepared)
        row.update(reporter.finish_item(row.get("status", "error")))
        if prefetch is not None:
            row["timings"].update(prefetch.timings(prepared))
            row["duration"] = round(perf_counter() - prepared.started, 3)
    return row


def _finish_reports(pipeline: GetPipeline, report: list[dict]) -> int:
    args, config = pipeline.args, pipeline.config
    failures = sum(row["status"] == "error" for row in report)
    pipeline.reporter.summary(failures, pipeline.output_dir)
    if args.report:
        try:
            atomic_json(args.report.expanduser(), report)
        except (OSError, ValueError) as exc:
            print(f"✗ Cannot save batch report: {exc}", file=sys.stderr)
            failures += 1
    if not args.dry_run or args.manifest or args.batch_name:
        try:
            manifest = save_manifest(
                report, config, args.manifest, name=args.batch_name, output_dir=pipeline.output_dir
            )
            if not args.quiet:
                print(f"Batch manifest: {manifest}", file=sys.stderr)
        except (OSError, ValueError) as exc:
            print(f"✗ Cannot save batch manifest: {exc}", file=sys.stderr)
            return 1
    return 1 if failures else 0


def _run_reported(
    args: argparse.Namespace,
    config: Config,
    reporter: Reporter,
    services: GetServices,
) -> int:
    if args.collection and not args.zotero:
        print("✗ --collection requires --zotero", file=sys.stderr)
        return 1
    if args.rename and args.zotero:
        print("✗ Use papr zotero add after renaming, or omit --rename", file=sys.stderr)
        return 1
    try:
        excluded = delivered_identities(
            [resolve_manifest(str(value), config) for value in args.exclude_delivered]
        )
    except (ValueError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    interactive = not args.non_interactive and sys.stdin.isatty()
    pipeline = GetPipeline(args, config, reporter, services, excluded, interactive)
    report: list[dict] = []
    items = _load_inputs(args.inputs, config, report)
    jobs = args.jobs or config.jobs
    concurrent = jobs > 1 and not (interactive or args.dry_run or args.rename)
    pool = Prefetch(
        items, pipeline.prepare, services.fetch_pdf, config, jobs=jobs, refresh=args.refresh
    )
    with pool if concurrent else nullcontext() as prefetch:
        reporter.start_batch(len(items))
        if prefetch is None:
            for item in items:
                report.append(_process_item(pipeline, item, None, None))
        else:
            for prepared in prefetch:
                report.append(_process_item(pipeline, prepared.item, prepared, prefetch))
    return _finish_reports(pipeline, report)


def run(args: argparse.Namespace, config: Config, services: GetServices) -> int:
    with http_session(), Reporter(enabled=not args.quiet) as reporter:
        try:
            if args.dry_run:
                return _run_reported(args, config, reporter, services)
            # Protect new objects through manifest registration, even with zero prune grace.
            with state_lock(config):
                return _run_reported(args, config, reporter, services)
        except OSError as exc:
            print(f"✗ Export: {exc}", file=sys.stderr)
            return 1
