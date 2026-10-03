from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .formats import bib, csl
from .local import article_from_pdf
from .model import Article


def load_input(value: str, config: Config) -> list[str | Article | Path]:
    path = Path(value).expanduser()
    if not path.exists():
        return [value]
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return [path]
    if suffix in {".txt", ".refs"}:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if suffix == ".bib":
        return bib.decode_many(path.read_text(encoding="utf-8"))
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        return [csl.decode(item) for item in data]
    return [value]


def materialize_article(item: str | Article | Path, config: Config) -> tuple[Article, Path | None]:
    from .resolvers import resolve

    if isinstance(item, Article):
        if item.doi:
            richer = resolve(item.doi, config)
            if richer:
                richer[0].merge(item)
                return richer[0], None
        return item, None
    if isinstance(item, Path):
        return article_from_pdf(item, config), item
    candidates = resolve(item, config)
    if not candidates:
        raise LookupError(f"No bibliographic match for: {item}")
    return candidates[0], None
