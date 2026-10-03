from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path

from .config import Config
from .formats import bib, csl
from .local import article_from_pdf
from .model import Article, Person
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
    values = {field.name: item[field.name] for field in fields(Article) if field.name in item}
    authors = values.get("authors", [])
    if not isinstance(authors, list) or not all(isinstance(author, dict) for author in authors):
        raise ValueError("Native JSON authors must be a list of person objects")
    person_fields = {field.name for field in fields(Person)}
    values["authors"] = [
        Person(**{key: value for key, value in author.items() if key in person_fields})
        for author in authors
    ]
    if not isinstance(values.get("title"), str):
        raise ValueError("Native JSON article requires a title string")
    return Article(**values)


def materialize_article(
    item: str | Article | Path,
    config: Config,
    *,
    use_local: bool = True,
    use_remote: bool = True,
    chooser: Callable[[str, list[Article], Config], Article] = select_candidate,
) -> tuple[Article, Path | None]:
    from .resolvers import resolve

    if isinstance(item, Article):
        if item.doi:
            richer = resolve(item.doi, config, use_local=use_local, use_remote=use_remote)
            if richer:
                try:
                    enriched = select_candidate(item.doi, richer, config)
                except LookupError:
                    return item, None
                enriched.merge(item)
                return enriched, None
        return item, None
    if isinstance(item, Path):
        return article_from_pdf(item, config, use_local=use_local, use_remote=use_remote), item
    candidates = resolve(item, config, use_local=use_local, use_remote=use_remote)
    return chooser(item, candidates, config), None
