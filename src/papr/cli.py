from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .config import Config, DEFAULT_TEMPLATE
from .export import FORMAT_SUFFIX, export_outputs
from .fetchers import PdfUnavailable, fetch_pdf
from .fetchers import ucl
from .filename import basename
from .input import load_input
from .local import article_from_pdf
from .model import Article
from .resolvers import resolve


def _formats(value: str) -> list[str]:
    items = [x.strip().lower() for x in value.split(",") if x.strip()]
    unknown = sorted(set(items) - set(FORMAT_SUFFIX))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown format(s): {', '.join(unknown)}")
    return items


def _article_line(article: Article) -> str:
    authors = article.first_creator
    year = article.year or "n.d."
    journal = f" — {article.journal}" if article.journal else ""
    doi = f" — {article.doi}" if article.doi else ""
    return f"{authors} ({year}). {article.title}{journal}{doi}"


def _choose(query: str, candidates: list[Article], config: Config, interactive: bool) -> Article:
    if not candidates:
        raise LookupError(f"No bibliographic match for: {query}")
    if len(candidates) == 1:
        return candidates[0]
    first = candidates[0].score or 0.0
    second = candidates[1].score or 0.0
    if first >= config.auto_accept_score and first - second >= config.auto_accept_margin:
        return candidates[0]
    if not interactive:
        raise LookupError(f"Ambiguous reference: {query}")
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
    item: str | Article | Path, config: Config, interactive: bool
) -> tuple[Article, Path | None]:
    if isinstance(item, Article):
        if item.doi:
            richer = resolve(item.doi, config)
            if richer:
                richer[0].merge(item)
                return richer[0], None
        return item, None
    if isinstance(item, Path):
        return article_from_pdf(item, config), item
    return _choose(item, resolve(item, config), config, interactive), None


def _get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr", description="Fetch and convert scholarly papers.")
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
    return p


def _resolve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papr resolve", description="Resolve a scholarly reference.")
    p.add_argument("query", nargs="+")
    p.add_argument("--json", action="store_true")
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
    p.add_argument("action", choices=["path", "init"])
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
            '[filename]\n'
            f"template = '{DEFAULT_TEMPLATE}'\n"
            'max_length = 180\n'
            'ascii = false\n'
            'space = "_"\n\n'
            '[processors.md]\n'
            'backend = "auto"  # auto, mistral, native\n'
            'model = "mistral-ocr-latest"\n\n'
            '[matching]\n'
            'auto_accept_score = 0.78\n'
            'auto_accept_margin = 0.08\n'
            'max_candidates = 5\n'
        ),
        encoding="utf-8",
    )
    print(path)
    return 0


def _run_get(args: argparse.Namespace, config: Config) -> int:
    formats = args.formats or config.formats
    output_dir = (args.output or config.download_dir).expanduser()
    interactive = not args.non_interactive and sys.stdin.isatty()
    report: list[dict] = []
    failures = 0
    items: list[str | Article | Path] = []
    for value in args.inputs:
        items.extend(load_input(value, config))

    for item in items:
        label = str(item) if not isinstance(item, Article) else (item.doi or item.title)
        row: dict = {"input": label}
        try:
            article, local_pdf = _resolve_item(item, config, interactive)
            row["doi"] = article.doi
            row["title"] = article.title
            local_formats = list(formats)
            local_output = output_dir
            if args.rename:
                if local_pdf is None:
                    raise ValueError("--rename is only valid for a local PDF input")
                base = basename(article, config, args.filename)
                destination = local_pdf.with_name(f"{base}.pdf")
                if destination.resolve() != local_pdf.resolve():
                    if destination.exists() and not args.overwrite:
                        from .filename import collision_safe_path

                        destination = collision_safe_path(destination)
                    if destination.exists() and args.overwrite:
                        destination.unlink()
                    local_pdf.rename(destination)
                print(f"✓ {destination}")
                row.update(status="ok", source="local", outputs=[str(destination)])
                report.append(row)
                continue
            base = basename(article, config, args.filename)
            if args.dry_run:
                planned = [str(local_output / f"{base}{FORMAT_SUFFIX[f]}") for f in local_formats]
                print(f"✓ {_article_line(article)}")
                for path in planned:
                    print(f"  {path}")
                row.update(status="dry-run", outputs=planned)
                report.append(row)
                continue

            needs_pdf = bool({"pdf", "md", "txt"} & set(local_formats))
            with tempfile.TemporaryDirectory(prefix="papr-") as td:
                if local_pdf is not None:
                    pdf = local_pdf
                    source = "local"
                elif needs_pdf:
                    pdf = Path(td) / "paper.pdf"
                    source = fetch_pdf(
                        article,
                        pdf,
                        config,
                        allow_ucl=not args.oa_only,
                        refresh=args.refresh,
                    )
                else:
                    pdf = Path(td) / "unused.pdf"
                    source = "metadata"
                outputs, processor_used = export_outputs(
                    article,
                    pdf,
                    local_formats,
                    local_output,
                    config,
                    filename_template=args.filename,
                    processor=args.processor,
                    overwrite=args.overwrite,
                )
            joined = ", ".join(str(x) for x in outputs)
            print(f"✓ {article.first_creator} {article.year or ''} — {joined}")
            row.update(
                status="ok",
                source=source,
                processor=processor_used,
                outputs=[str(x) for x in outputs],
            )
        except (LookupError, PdfUnavailable, RuntimeError, ValueError, OSError) as exc:
            failures += 1
            print(f"✗ {label}: {exc}", file=sys.stderr)
            row.update(status="error", error=str(exc))
        report.append(row)

    if args.report:
        report_path = args.report.expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["--version"], ["-V"]):
        print(__version__)
        return 0
    config = Config.load()
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
    if argv and argv[0] == "resolve":
        args = _resolve_parser().parse_args(argv[1:])
        query = " ".join(args.query)
        candidates = resolve(query, config)
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
        return _config_init(config)
    if not argv:
        _get_parser().print_help()
        return 0
    return _run_get(_get_parser().parse_args(argv), config)


if __name__ == "__main__":
    raise SystemExit(main())
