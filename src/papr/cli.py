from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .cli_arguments import (
    _add_source_flags,
    _config_parser,
    _get_parser,
    _login_parser,
    _logout_parser,
    _resolve_parser,
    _sources_parser,
)
from .cli_arguments import _formats as _formats
from .compress_cli import _atomic_pdf_copy as _atomic_pdf_copy
from .compress_cli import _compress_parser, _run_compress
from .config import DEFAULT_TEMPLATE, Config, set_setting
from .export import export_outputs
from .fetchers import fetch_pdf, ucl
from .filename import basename
from .get_cli import _input_values as _input_values
from .get_cli import run as run_get
from .get_pipeline import GetServices
from .http import http_session
from .input import materialize_article
from .model import Article
from .progress import ProgressLogHandler, Reporter, emit, pause
from .resolvers import resolve
from .resolvers.common import extract_doi, extract_pmid
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
    enrich: bool = False,
) -> tuple[Article, Path | None]:
    return materialize_article(
        item,
        config,
        use_local=use_local,
        use_remote=use_remote,
        **({"enrich": True} if enrich else {}),
        chooser=lambda query, candidates, cfg: _choose(query, candidates, cfg, interactive),
    )


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
            'jobs = 4\n'
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
    return run_get(args, config, GetServices(
        resolve_item=_resolve_item,
        fetch_pdf=fetch_pdf,
        export_outputs=export_outputs,
        basename=basename,
        article_line=_article_line,
    ))


def _zotero_parser() -> argparse.ArgumentParser:
    from .zotero_cli import parser

    return parser(_add_source_flags)


def _run_zotero(args: argparse.Namespace, config: Config) -> int:
    from .zotero_cli import run

    return run(args, config, resolve_item=_resolve_item, article_line=_article_line)


def _print_sources(config: Config, *, check: bool = False, as_json: bool = False) -> int:
    from dataclasses import asdict

    statuses = source_statuses(config, check=True) if check else source_statuses(config)
    if as_json:
        print(json.dumps([asdict(status) for status in statuses], indent=2, ensure_ascii=False))
        return 0
    for status in statuses:
        mark = "✓" if status.available else "−"
        print(f"{mark} {status.name:<12} {status.detail}")
    return 0


def _run_batch(argv: list[str], config: Config) -> int:
    from .batch_cli import run

    return run(argv, config)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--version"], ["-V"]):
        print(__version__)
        return 0
    try:
        config = Config.load()
    except (ValueError, OSError) as exc:
        if argv and argv[0] == "doctor" and "--json" in argv:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "strict": "--strict" in argv,
                        "checks": [
                            {"name": "configuration", "status": "error", "detail": str(exc)}
                        ],
                    }
                )
            )
            return 1
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
        args = _sources_parser().parse_args(argv[1:])
        return _print_sources(config, check=args.check, as_json=args.json)
    if argv and argv[0] == "batch":
        return _run_batch(argv[1:], config)
    if argv and argv[0] == "cache":
        from .cache_cli import run

        return run(argv[1:], config)
    if argv and argv[0] == "doctor":
        from .doctor_cli import run

        return run(argv[1:], config)
    if argv and argv[0] == "resolve":
        args = _resolve_parser().parse_args(argv[1:])
        query = " ".join(args.query)
        with (
            _diagnostics(args.verbose),
            resolution_session(),
            http_session(),
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
                    **({"enrich": True} if args.enrich else {}),
                )
                if args.item_type:
                    candidates = [a for a in candidates if a.item_type in args.item_type]
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
