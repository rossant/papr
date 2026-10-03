from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from ..config import Config
from ..model import Article
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
) -> str:
    cache = _cache_path(article, config)
    if cache.exists() and not refresh:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache, destination)
        return "cache"

    temp = cache.with_suffix(".tmp.pdf")
    temp.parent.mkdir(parents=True, exist_ok=True)
    source = oa.fetch(article, temp, config)
    if not source and allow_ucl:
        source = ucl.fetch(article, temp, config)
    if not source:
        temp.unlink(missing_ok=True)
        raise PdfUnavailable(f"No PDF found for {article.doi or article.title}")
    if not temp.read_bytes().startswith(b"%PDF-"):
        temp.unlink(missing_ok=True)
        raise PdfUnavailable("Downloaded content is not a PDF")
    temp.replace(cache)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache, destination)
    return source
