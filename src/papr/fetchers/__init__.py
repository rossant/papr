from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from ..config import Config
from ..model import Article
from ..progress import emit
from . import oa, ucl


class PdfUnavailable(RuntimeError):
    pass


def _cache_path(article: Article, config: Config) -> Path:
    key = article.doi or article.pmid or article.title
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return config.cache_dir / "pdf" / f"{digest}.pdf"


def _is_local_pdf(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("rb") as file:
            return file.read(5) == b"%PDF-"
    except OSError:
        return False


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
    if local is None and not refresh and _is_local_pdf(cache):
        emit("fetch", "Using cached PDF")
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
            source = f"local:{local}"
        else:
            source = oa.fetch(article, temp, config)
            if source and not _is_local_pdf(temp):
                source = None
            if not source and allow_ucl:
                emit("fetch", "Open-access PDF unavailable; trying UCL access")
                temp.unlink(missing_ok=True)
                source = (
                    ucl.fetch(article, temp, config, headless=False)
                    if show_browser
                    else ucl.fetch(article, temp, config)
                )
            if not source or not _is_local_pdf(temp):
                raise PdfUnavailable(f"No valid PDF found for {article.doi or article.title}")
        emit("fetch", "Saving PDF to cache")
        temp.replace(cache)
    finally:
        temp.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if cache.resolve() != destination.resolve():
        shutil.copy2(cache, destination)
    return source
