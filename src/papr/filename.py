from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .config import Config
from .model import Article

_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)(.*?)\}\}")
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _value(article: Article, name: str) -> str:
    values = {
        "firstCreator": article.first_creator,
        "author": article.first_creator,
        "year": str(article.year or "n.d."),
        "title": article.title,
        "journal": article.journal or "",
        "container-title": article.journal or "",
        "DOI": article.doi or "",
        "doi": article.doi or "",
        "PMID": article.pmid or "",
        "pmid": article.pmid or "",
        "PMCID": article.pmcid or "",
        "pmcid": article.pmcid or "",
        "citationKey": article.citation_key,
        "citation-key": article.citation_key,
    }
    return values.get(name, "")


def render_template(article: Article, template: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = _value(article, match.group(1))
        attrs = dict(_ATTR_RE.findall(match.group(2)))
        if "truncate" in attrs:
            try:
                value = value[: int(attrs["truncate"])]
            except ValueError:
                pass
        if value:
            value = attrs.get("prefix", "") + value + attrs.get("suffix", "")
        return value

    return _TOKEN_RE.sub(repl, template)


def sanitize_filename(name: str, config: Config) -> str:
    name = unicodedata.normalize("NFKC", name).strip()
    if config.filename_ascii:
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r'[\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", config.filename_space, name)
    name = re.sub(r"_+", "_", name).strip(" ._-")
    if len(name) > config.filename_max_length:
        name = name[: config.filename_max_length].rstrip(" ._-")
    return name or "paper"


def basename(article: Article, config: Config, override: str | None = None) -> str:
    return sanitize_filename(render_template(article, override or config.filename_template), config)


def collision_safe_path(path: Path, *, overwrite: bool = False) -> Path:
    if overwrite or not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1
