from __future__ import annotations

import re

from ..model import Article, Person

_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,]+),(.*)\}\s*$", re.S)
_FIELD_RE = re.compile(r"(\w+)\s*=\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\}|\"([^\"]*)\")\s*,?", re.S)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def encode(article: Article, *, biblatex: bool = True) -> str:
    entry_type = {
        "article-journal": "article",
        "article-magazine": "article",
        "article-newspaper": "article",
        "book": "book",
        "chapter": "incollection",
        "paper-conference": "inproceedings",
        "thesis": "phdthesis",
        "report": "techreport",
    }.get(article.item_type, "misc")
    fields: list[tuple[str, str]] = []
    if article.authors:
        authors = " and ".join(
            f"{a.family}, {a.given}" if a.given else a.family for a in article.authors
        )
        fields.append(("author", authors))
    fields.append(("title", article.title))
    if article.journal:
        container_field = (
            "booktitle"
            if article.item_type in {"chapter", "paper-conference"}
            else "journaltitle"
            if biblatex
            else "journal"
        )
        fields.append((container_field, article.journal))
    if article.year:
        fields.append(("date" if biblatex else "year", str(article.year)))
    if article.volume:
        fields.append(("volume", article.volume))
    if article.issue:
        fields.append(("number", article.issue))
    if article.pages:
        fields.append(("pages", article.pages.replace("-", "--")))
    if article.doi:
        fields.append(("doi", article.doi))
    if article.url:
        fields.append(("url", article.url))
    if article.pmid:
        if biblatex:
            fields.extend((("eprint", article.pmid), ("eprinttype", "pubmed")))
        else:
            fields.append(("pmid", article.pmid))
    body = ",\n".join(f"  {k} = {{{_escape(v)}}}" for k, v in fields)
    return f"@{entry_type}{{{article.citation_key},\n{body}\n}}\n"


def decode_many(text: str) -> list[Article]:
    entries = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "@" and depth == 0:
            start = i
        elif ch == "{" and start is not None:
            depth += 1
        elif ch == "}" and start is not None:
            depth -= 1
            if depth == 0:
                block = text[start : i + 1]
                article = _decode_entry(block)
                if article:
                    entries.append(article)
                start = None
    return entries


def _decode_entry(block: str) -> Article | None:
    match = _ENTRY_RE.search(block.strip())
    if not match:
        return None
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(match.group(3)):
        fields[m.group(1).lower()] = (m.group(2) if m.group(2) is not None else m.group(3)).strip()
    title = fields.get("title", "").replace("{", "").replace("}", "")
    if not title and not fields.get("doi"):
        return None
    authors = []
    for raw in fields.get("author", "").split(" and "):
        raw = raw.strip()
        if not raw:
            continue
        if "," in raw:
            family, given = (x.strip() for x in raw.split(",", 1))
        else:
            parts = raw.split()
            family, given = (parts[-1], " ".join(parts[:-1])) if parts else ("", "")
        authors.append(Person(family=family, given=given))
    year_text = fields.get("date") or fields.get("year") or ""
    m = re.search(r"\d{4}", year_text)
    return Article(
        title=title,
        authors=authors,
        year=int(m.group()) if m else None,
        journal=fields.get("journaltitle") or fields.get("journal") or fields.get("booktitle"),
        volume=fields.get("volume"),
        issue=fields.get("number"),
        pages=(fields.get("pages") or "").replace("--", "-") or None,
        doi=fields.get("doi"),
        pmid=(
            fields.get("pmid")
            or (fields.get("eprint") if fields.get("eprinttype") == "pubmed" else None)
        ),
        url=fields.get("url"),
        item_type={
            "article": "article-journal",
            "book": "book",
            "incollection": "chapter",
            "inbook": "chapter",
            "inproceedings": "paper-conference",
            "conference": "paper-conference",
            "phdthesis": "thesis",
            "mastersthesis": "thesis",
            "thesis": "thesis",
            "techreport": "report",
            "report": "report",
            "unpublished": "manuscript",
        }.get(match.group(1).lower(), "document"),
        source_item_type=match.group(1).lower(),
        source="bib",
    )
