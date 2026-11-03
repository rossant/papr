from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from ..acquisition import record_attempt
from ..config import Config
from ..model import Article
from ..pdf import PdfValidationError, validate_pdf
from ..pdf import is_local_pdf as _is_local_pdf
from ..progress import emit
from . import oa, ucl


class PdfUnavailable(RuntimeError):
    pass


def _cache_path(article: Article, config: Config) -> Path:
    key = article.doi or article.pmid or article.title
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return config.cache_dir / "pdf" / f"{digest}.pdf"


def fetch_pdf(
    article: Article,
    destination: Path,
    config: Config,
    *,
    allow_ucl: bool = True,
    refresh: bool = False,
    show_browser: bool = False,
) -> str:
    emit("fetch", "Checking local PDF and cache")
    cache = _cache_path(article, config)
    local = None
    if article.local_pdf:
        candidate = Path(article.local_pdf).expanduser()
        if _is_local_pdf(candidate):
            local = candidate
    valid_cache = False
    if local is None and not refresh and _is_local_pdf(cache):
        try:
            validate_pdf(cache, article)
            valid_cache = True
        except PdfValidationError as exc:
            emit("fetch", f"Ignoring invalid cached PDF: {exc}")
            record_attempt("cache", reason=str(exc), recovery="Download another original PDF")
    if valid_cache:
        emit("fetch", "Using cached PDF")
        record_attempt("cache", outcome="success")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if cache.resolve() != destination.resolve():
            shutil.copy2(cache, destination)
        return "cache"

    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache.parent, suffix=".tmp.pdf", delete=False) as file:
        temp = Path(file.name)
    try:
        if local is not None:
            emit("fetch", "Copying local PDF")
            shutil.copy2(local, temp)
            try:
                validate_pdf(temp, article)
            except PdfValidationError as exc:
                record_attempt("local", reason=str(exc), recovery="Check the original local PDF")
                raise
            source = f"local:{local}"
            record_attempt("local", outcome="success")
        else:
            source = oa.fetch(article, temp, config)
            validated = False
            if source:
                try:
                    validate_pdf(temp, article)
                    validated = True
                except PdfValidationError as exc:
                    emit("fetch", f"Rejecting invalid open-access PDF: {exc}")
                    record_attempt("oa", source, reason=str(exc),
                                   recovery="Try another original PDF source")
                    source = None
            if not source and allow_ucl:
                emit("fetch", "Open-access PDF unavailable; trying UCL access")
                temp.unlink(missing_ok=True)
                source = (
                    ucl.fetch(article, temp, config, headless=False)
                    if show_browser
                    else ucl.fetch(article, temp, config)
                )
            if not source:
                raise PdfUnavailable(f"No valid PDF found for {article.doi or article.title}")
            if not validated:
                try:
                    validate_pdf(temp, article)
                except PdfValidationError as exc:
                    record_attempt("ucl", source, reason=str(exc),
                                   recovery="Try another original PDF source")
                    message = f"Invalid PDF for {article.doi or article.title}: {exc}"
                    raise PdfUnavailable(message) from exc
        emit("fetch", "Saving PDF to cache")
        temp.replace(cache)
    finally:
        temp.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if cache.resolve() != destination.resolve():
        shutil.copy2(cache, destination)
    return source
