from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .formats import bib, csl
from .local import article_from_pdf
from .model import Article, article_from_dict
from .selection import select_candidate


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
            line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
        ]
    if suffix == ".bib":
        return bib.decode_many(path.read_text(encoding="utf-8"))
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError("JSON input must be an article object or a list of article objects")
        return [_decode_json(item) for item in data]
    return [value]


def _decode_json(item: dict) -> Article:
    native_keys = {"authors", "year", "journal", "doi", "pmid", "item_type", "local_pdf"}
    if not native_keys.intersection(item):
        return csl.decode(item)
    return article_from_dict(item, strict=False)


def materialize_article(
    item: str | Article | Path,
    config: Config,
    *,
    use_local: bool = True,
    use_remote: bool = True,
    enrich: bool = False,
    chooser: Callable[[str, list[Article], Config], Article] = select_candidate,
) -> tuple[Article, Path | None]:
    from .resolvers import resolve

    options = {"enrich": True} if enrich else {}

    if isinstance(item, Article):
        if item.doi:
            richer = resolve(
                item.doi, config, use_local=use_local, use_remote=use_remote, **options
            )
            if richer:
                try:
                    enriched = select_candidate(item.doi, richer, config)
                except LookupError:
                    return item, None
                enriched.merge(item)
                return enriched, None
        return item, None
    if isinstance(item, Path):
        return article_from_pdf(
            item, config, use_local=use_local, use_remote=use_remote, **options
        ), item
    candidates = resolve(item, config, use_local=use_local, use_remote=use_remote, **options)
    return chooser(item, candidates, config), None
