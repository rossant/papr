from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from . import __version__
from .config import DEFAULT_TEMPLATE, Config, set_setting
from .export import FORMAT_SUFFIX, export_outputs, output_suffix
from .fetchers import fetch_pdf, ucl
from .filename import basename
from .input import load_input, materialize_article
from .model import Article
from .progress import ProgressLogHandler, Reporter, emit, pause
from .resolvers import resolve
from .resolvers.common import extract_doi, extract_pmid, extract_year
from .resolvers.session import resolution_session
from .selection import select_candidate
from .sources import statuses as source_statuses


@contextmanager
def _diagnostics(verbose: bool):
    logger = logging.getLogger("papr")
    if not verbose:
        yield
        return
    previous_level = logger.level
    previous_propagate = logger.propagate
    handler = ProgressLogHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _formats(value: str) -> list[str]:
    items = [x.strip().lower() for x in value.split(",") if x.strip()]
    if not items:
        raise argparse.ArgumentTypeError("at least one output format is required")
    unknown = sorted(set(items) - set(FORMAT_SUFFIX))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown format(s): {', '.join(unknown)}")
    return items


def _article_line(article: Article) -> str:
    authors = article.first_creator
    year = article.year or "n.d."
    journal = f" — {article.journal}" if article.journal else ""
    doi = f" — {article.doi}" if article.doi else ""
    source = f" [{article.source}]" if article.source else ""
    return f"{authors} ({year}). {article.title}{journal}{doi}{source}"


def _choose(query: str, candidates: list[Article], config: Config, interactive: bool) -> Article:
    try:
        return select_candidate(query, candidates, config)
    except LookupError:
        if not interactive or not candidates or extract_doi(query) or extract_pmid(query):
            raise
    with pause():
        return _prompt_candidate(query, candidates)


def _prompt_candidate(query: str, candidates: list[Article]) -> Article:
    print(f"Ambiguous reference: {query}", file=sys.stderr)
    for i, item in enumerate(candidates, 1):
        score = f" [{item.score:.2f}]" if item.score is not None else ""
        print(f"  {i}. {_article_line(item)}{score}", file=sys.stderr)
    while True:
        answer = input(f"Choose [1-{len(candidates)}] or q: ").strip().lower()
        if answer in {"q", "quit", ""}:
            raise LookupError("Selection cancelled")
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]


def _resolve_item(
    item: str | Article | Path,
    config: Config,
    interactive: bool,
    *,
    use_local: bool = True,
    use_remote: bool = True,
) -> tuple[Article, Path | None]:
    return materialize_article(
        item,
        config,
        use_local=use_local,
        use_remote=use_remote,
        chooser=lambda query, candidates, cfg: _choose(query, candidates, cfg, interactive),
    )


def _add_source_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--quiet", action="store_true", help="suppress progress and summary")
    parser.add_argument("--verbose", action="store_true", help="show diagnostic details")
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
        epilog="Commands: resolve, compress, zotero, config, sources. Use papr COMMAND --help.",
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
    p.add_argument("--zotero", action="store_true", help="save the paper and PDF to Zotero")
    p.add_argument("--collection", help="Zotero collection name or key (with --zotero)")
    p.add_argument("--no-compress", action="store_true", help="export the original PDF")
    _add_source_flags(p)
    return p


def _resolve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr resolve", description="Resolve a scholarly reference.")
    p.add_argument("query", nargs="+")
    p.add_argument("--json", action="store_true")
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


def _config_init(config: Config) -> int:
    path = config.config_dir / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(path)
        return 0
    path.write_text(
        (
            'download_dir = "~/Downloads"\n'
            'formats = ["pdf"]\n'
            '# email = "you@example.org"\n'
            '# unpaywall_email = "you@example.org"\n'
            '# openalex_api_key = "..."\n\n'
            "[filename]\n"
            f"template = '{DEFAULT_TEMPLATE}'\n"
            "max_length = 180\n"
            "ascii = false\n"
            'space = "_"\n\n'
            "[processors.md]\n"
            'backend = "auto"  # auto, mistral, native\n'
            'model = "mistral-ocr-latest"\n\n'
            "[matching]\n"
            "auto_accept_score = 0.78\n"
            "auto_accept_margin = 0.08\n"
            "max_candidates = 5\n\n"
            "[pdf]\n"
            "compress = true\n"
            "dpi = 200\n"
            "timeout = 120\n\n"
            "[zotero]\n"
            "# group_id = 1234567  # optional shared group library\n"
            '# collection = "Inbox"  # optional collection within the selected library\n\n'
            "[local]\n"
            "enabled = true\n"
            'zotero_api_url = "http://127.0.0.1:23119/api"\n'
            '# zotero_data_dir = "~/Zotero"\n'
            '# zolit_repo = "~/src/zolit"\n'
            '# zolit_db = "~/src/zolit/data.local/zolit.sqlite"\n'
            '# zolit_sbs_repo = "~/src/zolit-sbs"\n'
        ),
        encoding="utf-8",
    )
    print(path)
    return 0


def _run_get(args: argparse.Namespace, config: Config) -> int:
    with Reporter(enabled=not args.quiet) as reporter:
        return _run_get_reported(args, config, reporter)


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


def _run_get_reported(args: argparse.Namespace, config: Config, reporter: Reporter) -> int:
    if args.collection and not args.zotero:
        print("✗ --collection requires --zotero", file=sys.stderr)
        return 1
    if args.rename and args.zotero:
        print("✗ Use papr zotero add after renaming, or omit --rename", file=sys.stderr)
        return 1
    formats = args.formats or config.formats
    output_dir = (args.output or config.download_dir).expanduser()
    interactive = not args.non_interactive and sys.stdin.isatty()
    use_local = not args.no_local
    use_remote = not args.local_only
    report: list[dict] = []
    failures = 0
    items: list[str | Article | Path] = []
    for value in _input_values(args.inputs):
        started = perf_counter()
        try:
            emit("input", f"Loading {value}")
            items.extend(load_input(value, config))
        except Exception as exc:
            failures += 1
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

    reporter.start_batch(len(items))
    for item in items:
        label = str(item) if not isinstance(item, Article) else (item.doi or item.title)
        row: dict = {"input": label}
        reporter.start_item(label)
        try:
            emit("resolve", "Resolving reference")
            article, local_pdf = _resolve_item(
                item,
                config,
                interactive,
                use_local=use_local,
                use_remote=use_remote,
            )
            row["doi"] = article.doi
            row["title"] = article.title
            reporter.label = _article_line(article)
            row["resolver"] = article.source
            local_formats = list(formats)
            local_output = output_dir
            if args.rename:
                emit("export", "Planning rename" if args.dry_run else "Renaming local PDF")
                if local_pdf is None:
                    raise ValueError("--rename is only valid for a local PDF input")
                base = basename(article, config, args.filename)
                destination = local_pdf.with_name(f"{base}.pdf")
                if destination.resolve() != local_pdf.resolve():
                    if destination.exists() and not args.overwrite:
                        from .filename import collision_safe_path

                        destination = collision_safe_path(destination)
                    if not args.dry_run:
                        if args.overwrite:
                            local_pdf.replace(destination)
                        else:
                            local_pdf.rename(destination)
                with pause():
                    print(f"✓ {destination}")
                row.update(
                    status="dry-run" if args.dry_run else "ok",
                    source="local",
                    outputs=[str(destination)],
                )
                continue
            base = basename(article, config, args.filename)
            if args.dry_run:
                emit("export", "Planning outputs")
                planned = [
                    str(local_output / f"{base}{output_suffix(f, local_formats)}")
                    for f in local_formats
                ]
                with pause():
                    print(f"✓ {_article_line(article)}")
                    for path in planned:
                        print(f"  {path}")
                row.update(status="dry-run", outputs=planned)
                continue

            needs_pdf = args.zotero or bool({"pdf", "md", "txt"} & set(local_formats))
            with tempfile.TemporaryDirectory(prefix="papr-") as td:
                if local_pdf is not None:
                    emit("fetch", "Using local PDF")
                    pdf = local_pdf
                    source = "local"
                elif needs_pdf:
                    emit("fetch", "Fetching PDF")
                    pdf = Path(td) / "paper.pdf"
                    source = fetch_pdf(
                        article,
                        pdf,
                        config,
                        allow_ucl=not args.oa_only,
                        refresh=args.refresh,
                        show_browser=args.show_browser,
                    )
                else:
                    pdf = Path(td) / "unused.pdf"
                    source = "metadata"
                prepared_pdf = pdf
                if "pdf" in local_formats or args.zotero:
                    from .compression import compress_pdf

                    prepared_pdf, row["compression"] = compress_pdf(
                        pdf, config, enabled=False if args.no_compress else None
                    )
                emit("export", "Preparing outputs")
                export_options = {}
                if "pdf" in local_formats:
                    export_options["pdf_output"] = prepared_pdf
                outputs, processor_used = export_outputs(
                    article,
                    pdf,
                    local_formats,
                    local_output,
                    config,
                    filename_template=args.filename,
                    processor=args.processor,
                    overwrite=args.overwrite,
                    **export_options,
                )
                row.update(
                    status="ok",
                    source=source,
                    processor=processor_used,
                    outputs=[str(path) for path in outputs],
                )
                exported_pdf = next((path for path in outputs if path.suffix == ".pdf"), None)
                if exported_pdf is not None:
                    from .history import record_download

                    record_download(article, exported_pdf, config)
                if args.zotero:
                    from .zotero import save

                    upload_pdf = exported_pdf or prepared_pdf
                    if exported_pdf is None:
                        upload_pdf = Path(td) / f"{base}.pdf"
                        if prepared_pdf.resolve() != upload_pdf.resolve():
                            shutil.copy2(prepared_pdf, upload_pdf)
                    row["zotero"] = save(
                        article,
                        upload_pdf,
                        config,
                        collection=args.collection,
                        interactive=interactive,
                    )
            joined = ", ".join(str(x) for x in outputs)
            with pause():
                print(f"✓ {article.first_creator} {article.year or ''} — {joined}")
                if row.get("zotero"):
                    saved = row["zotero"]
                    target = saved.get("library") or "My Library"
                    if saved.get("collection"):
                        target += f" / {saved['collection']}"
                    print(f"✓ Zotero: {saved['status']} — {target}")
            row.update(
                status="ok",
                source=source,
                processor=processor_used,
                outputs=[str(x) for x in outputs],
            )
        except Exception as exc:
            failures += 1
            logging.getLogger(__name__).debug("Processing failed: %s", label, exc_info=True)
            with pause():
                print(f"✗ {label}: {exc}", file=sys.stderr)
                for path in row.get("outputs", []):
                    print(f"  Saved: {path}", file=sys.stderr)
            row.update(status="error", error=str(exc))
        finally:
            row.update(reporter.finish_item(row.get("status", "error")))
            report.append(row)

    reporter.summary(failures, output_dir)
    if args.report:
        report_path = args.report.expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return 1 if failures else 0


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


def _atomic_pdf_copy(source: Path, destination: Path, *, overwrite: bool):
    """Publish a complete PDF without truncating an existing file on failure."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".pdf", delete=False
        ) as file:
            temporary = Path(file.name)
        shutil.copy2(source, temporary)
        if overwrite:
            if destination.exists():
                temporary.chmod(destination.stat().st_mode & 0o777)
            temporary.replace(destination)
        else:
            # An exclusive hard link also protects against a destination appearing mid-copy.
            destination.hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def _zotero_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="papr zotero", description="Save papers to Zotero.")
    parser.add_argument("action", nargs="?", default="last", choices=["add", "last"])
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF to import (for add)")
    parser.add_argument("--collection", help="override your personal default collection")
    parser.add_argument("--no-compress", action="store_true", help="import the original PDF")
    parser.add_argument(
        "--non-interactive", action="store_true", help="do not ask for authorization"
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without changing Zotero")
    _add_source_flags(parser)
    return parser


def _run_zotero(args: argparse.Namespace, config: Config) -> int:
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
                article, _ = _resolve_item(
                    pdf,
                    config,
                    interactive,
                    use_local=not args.no_local,
                    use_remote=not args.local_only,
                )
            reporter.label = _article_line(article)
            collection = args.collection or config.zotero_collection
            if args.dry_run:
                with pause():
                    print(f"Would save {_article_line(article)}")
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


def _print_sources(config: Config) -> int:
    for status in source_statuses(config):
        mark = "✓" if status.available else "−"
        print(f"{mark} {status.name:<12} {status.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--version"], ["-V"]):
        print(__version__)
        return 0
    try:
        config = Config.load()
    except (ValueError, OSError) as exc:
        print(f"✗ Configuration: {exc}", file=sys.stderr)
        return 1
    if argv and argv[0] == "login":
        args = _login_parser().parse_args(argv[1:])
        if args.institution == "ucl":
            ucl.login(config)
        return 0
    if argv and argv[0] == "logout":
        args = _logout_parser().parse_args(argv[1:])
        if args.institution == "ucl" and config.ucl_profile_dir.exists():
            shutil.rmtree(config.ucl_profile_dir)
        return 0
    if argv and argv[0] == "compress":
        args = _compress_parser().parse_args(argv[1:])
        with _diagnostics(args.verbose):
            return _run_compress(args, config)
    if argv and argv[0] == "zotero":
        args = _zotero_parser().parse_args(argv[1:])
        with _diagnostics(args.verbose), resolution_session():
            return _run_zotero(args, config)
    if argv and argv[0] == "sources":
        return _print_sources(config)
    if argv and argv[0] == "resolve":
        args = _resolve_parser().parse_args(argv[1:])
        query = " ".join(args.query)
        with (
            _diagnostics(args.verbose),
            resolution_session(),
            Reporter(enabled=not args.quiet) as reporter,
        ):
            reporter.start_batch(1)
            reporter.start_item(query)
            status = "error"
            try:
                emit("resolve", "Resolving reference")
                candidates = resolve(
                    query,
                    config,
                    use_local=not args.no_local,
                    use_remote=not args.local_only,
                )
                status = "ok" if candidates else "error"
            except Exception as exc:
                logging.getLogger(__name__).debug("Resolution failed: %s", query, exc_info=True)
                with pause():
                    print(f"✗ {query}: {exc}", file=sys.stderr)
                return 1
            finally:
                reporter.finish_item(status)
        if args.json:
            print(json.dumps([a.to_dict() for a in candidates], indent=2, ensure_ascii=False))
        else:
            for i, article in enumerate(candidates, 1):
                score = f" [{article.score:.2f}]" if article.score is not None else ""
                print(f"{i}. {_article_line(article)}{score}")
        return 0 if candidates else 1
    if argv and argv[0] == "config":
        args = _config_parser().parse_args(argv[1:])
        if args.action == "path":
            print(config.config_dir / "config.toml")
            return 0
        if args.action == "set":
            if args.key is None or args.value is None:
                print("✗ Usage: papr config set SETTING VALUE", file=sys.stderr)
                return 1
            try:
                path = set_setting(config, args.key, args.value)
            except (ValueError, OSError) as exc:
                print(f"✗ Configuration: {exc}", file=sys.stderr)
                return 1
            print(f"✓ {args.key} = {args.value} ({path})")
            return 0
        return _config_init(config)
    if not argv:
        _get_parser().print_help()
        return 0
    args = _get_parser().parse_args(argv)
    with _diagnostics(args.verbose), resolution_session():
        return _run_get(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
