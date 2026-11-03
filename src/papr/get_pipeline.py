"""Per-paper resolution, planning, and publication for the fetch command."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .batches import identities, identity, record_artifacts
from .config import Config
from .export import ExportError, output_suffix
from .fetchers import ucl
from .filename import collision_safe_path
from .history import record_download
from .model import Article
from .prefetch import Prefetch, PreparedItem
from .progress import Reporter, emit, pause


@dataclass(frozen=True)
class GetServices:
    """CLI lookup seams captured once for a run, including worker callbacks."""

    resolve_item: Callable[..., tuple[Article, Path | None]]
    fetch_pdf: Callable[..., str]
    export_outputs: Callable[..., tuple[list[Path], str | None]]
    basename: Callable[..., str]
    article_line: Callable[[Article], str]


@dataclass
class GetPipeline:
    args: argparse.Namespace
    config: Config
    reporter: Reporter
    services: GetServices
    excluded: set[str]
    interactive: bool

    @property
    def formats(self) -> list[str]:
        return list(self.args.formats or self.config.formats)

    @property
    def output_dir(self) -> Path:
        return (self.args.output or self.config.download_dir).expanduser()

    @property
    def needs_pdf(self) -> bool:
        return self.args.zotero or bool({"pdf", "md", "txt"} & set(self.formats))

    def resolve(self, item: str | Article | Path) -> tuple[Article, Path | None]:
        emit("resolve", "Resolving reference")
        return self.services.resolve_item(
            item,
            self.config,
            self.interactive,
            use_local=not self.args.no_local,
            use_remote=not self.args.local_only,
            **({"enrich": True} if self.args.enrich else {}),
        )

    def exclusion_reason(self, article: Article) -> str | None:
        if self.args.item_type and article.item_type not in self.args.item_type:
            return f"document type: {article.item_type}"
        if identities(article) & self.excluded:
            return "already delivered"
        return None

    def prepare(self, item: str | Article | Path) -> tuple[Article, Path | None, bool]:
        article, local_pdf = self.resolve(item)
        return article, local_pdf, self.needs_pdf and self.exclusion_reason(article) is None

    def process(
        self,
        item: str | Article | Path,
        row: dict,
        prepared: PreparedItem | None,
        prefetch: Prefetch | None,
    ) -> None:
        if prefetch is not None:
            emit("resolve", "Resolving reference")
            prepared.events.replay()
            if prepared.error is not None:
                raise prepared.error
            article, local_pdf = prepared.article, prepared.local_pdf
        else:
            article, local_pdf = self.resolve(item)
        row.update(article=article.to_dict(), identity=identity(article))
        reason = self.exclusion_reason(article)
        if reason:
            row.update(status="excluded", exclusion_reason=reason)
            with pause():
                print(f"− {self.services.article_line(article)} — {reason}")
            return
        row.update(doi=article.doi, title=article.title, resolver=article.source)
        self.reporter.label = self.services.article_line(article)
        if self.args.rename:
            self.rename(article, local_pdf, row)
        elif self.args.dry_run:
            self.plan(article, row)
        else:
            self.export(article, local_pdf, row, prepared, prefetch)

    def rename(self, article: Article, local_pdf: Path | None, row: dict) -> None:
        emit("export", "Planning rename" if self.args.dry_run else "Renaming local PDF")
        if local_pdf is None:
            raise ValueError("--rename is only valid for a local PDF input")
        base = self.services.basename(article, self.config, self.args.filename)
        destination = local_pdf.with_name(f"{base}.pdf")
        if destination.resolve() != local_pdf.resolve():
            if destination.exists() and not self.args.overwrite:
                destination = collision_safe_path(destination)
            if not self.args.dry_run:
                if self.args.overwrite:
                    local_pdf.replace(destination)
                else:
                    local_pdf.rename(destination)
        with pause():
            print(f"✓ {destination}")
        row.update(
            status="dry-run" if self.args.dry_run else "ok",
            source="local",
            outputs=[str(destination)],
        )
        if not self.args.dry_run:
            self.remember(article, [destination], row, "ok")

    def plan(self, article: Article, row: dict) -> None:
        emit("export", "Planning outputs")
        base = self.services.basename(article, self.config, self.args.filename)
        formats = self.formats
        planned = [str(self.output_dir / f"{base}{output_suffix(f, formats)}") for f in formats]
        with pause():
            print(f"✓ {self.services.article_line(article)}")
            for path in planned:
                print(f"  {path}")
        row.update(status="dry-run", outputs=planned)

    def fetch(
        self,
        article: Article,
        local_pdf: Path | None,
        temporary: Path,
        prepared: PreparedItem | None,
        prefetch: Prefetch | None,
    ) -> tuple[Path, str]:
        if local_pdf is not None:
            emit("fetch", "Using local PDF")
            return local_pdf, "local"
        if not self.needs_pdf:
            return temporary / "unused.pdf", "metadata"
        emit("fetch", "Fetching PDF")
        if prefetch is not None:
            return prefetch.result(
                prepared,
                allow_ucl=not self.args.oa_only,
                ucl_fetch=ucl.fetch,
                show_browser=self.args.show_browser,
            )
        pdf = temporary / "paper.pdf"
        source = self.services.fetch_pdf(
            article,
            pdf,
            self.config,
            allow_ucl=not self.args.oa_only,
            refresh=self.args.refresh,
            show_browser=self.args.show_browser,
        )
        return pdf, source

    def remember(self, article: Article, outputs: list[Path], row: dict, status: str) -> None:
        row.update(record_artifacts(article, outputs, self.config), export_status=status)
        exported_pdf = next((path for path in outputs if path.suffix == ".pdf"), None)
        if exported_pdf is not None:
            record_download(article, exported_pdf, self.config)

    def publish(
        self,
        article: Article,
        pdf: Path,
        prepared_pdf: Path,
        source: str,
        row: dict,
    ) -> list[Path]:
        emit("export", "Preparing outputs")
        options = {"pdf_output": prepared_pdf} if "pdf" in self.formats else {}
        export_error = None
        try:
            outputs, processor = self.services.export_outputs(
                article,
                pdf,
                self.formats,
                self.output_dir,
                self.config,
                filename_template=self.args.filename,
                processor=self.args.processor,
                overwrite=self.args.overwrite,
                **options,
            )
        except ExportError as exc:
            outputs, processor = exc.outputs, exc.processor
            export_error = exc
        row.update(
            status="ok",
            source=source,
            processor=processor,
            outputs=[str(path) for path in outputs],
        )
        self.remember(article, outputs, row, "partial" if export_error else "ok")
        if export_error is not None:
            raise export_error
        return outputs

    def export(
        self,
        article: Article,
        local_pdf: Path | None,
        row: dict,
        prepared: PreparedItem | None,
        prefetch: Prefetch | None,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="papr-") as directory:
            temporary = Path(directory)
            pdf, source = self.fetch(article, local_pdf, temporary, prepared, prefetch)
            prepared_pdf = pdf
            if "pdf" in self.formats or self.args.zotero:
                from .compression import compress_pdf

                prepared_pdf, row["compression"] = compress_pdf(
                    pdf, self.config, enabled=False if self.args.no_compress else None
                )
            outputs = self.publish(article, pdf, prepared_pdf, source, row)
            if self.args.zotero:
                self.save_zotero(article, prepared_pdf, outputs, temporary, row)
        joined = ", ".join(str(path) for path in outputs)
        with pause():
            print(f"✓ {article.first_creator} {article.year or ''} — {joined}")
            if row.get("zotero"):
                saved = row["zotero"]
                target = saved.get("library") or "My Library"
                if saved.get("collection"):
                    target += f" / {saved['collection']}"
                print(f"✓ Zotero: {saved['status']} — {target}")

    def save_zotero(
        self,
        article: Article,
        prepared_pdf: Path,
        outputs: list[Path],
        temporary: Path,
        row: dict,
    ) -> None:
        from .zotero import save

        upload_pdf = next((path for path in outputs if path.suffix == ".pdf"), None)
        if upload_pdf is None:
            base = self.services.basename(article, self.config, self.args.filename)
            upload_pdf = temporary / f"{base}.pdf"
            if prepared_pdf.resolve() != upload_pdf.resolve():
                shutil.copy2(prepared_pdf, upload_pdf)
        row["zotero"] = save(
            article,
            upload_pdf,
            self.config,
            collection=self.args.collection,
            interactive=self.interactive,
        )
