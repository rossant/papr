from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class Person:
    family: str = ""
    given: str = ""
    orcid: str | None = None

    @property
    def display(self) -> str:
        return " ".join(x for x in (self.given, self.family) if x)


@dataclass
class Article:
    title: str
    authors: list[Person] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    url: str | None = None
    abstract: str | None = None
    item_type: str = "article-journal"
    source_item_type: str | None = None
    source_key: str | None = None
    source_library: str | None = None
    oa_pdf_url: str | None = None
    local_pdf: str | None = None
    source: str | None = None
    score: float | None = None

    @property
    def first_creator(self) -> str:
        return self.authors[0].family if self.authors else "Unknown"

    @property
    def citation_key(self) -> str:
        word = next((w for w in self.title.split() if len(w) >= 4), "Paper")
        year = str(self.year or "n.d.")
        return f"{self.first_creator}{year}{word}".replace(" ", "")

    def merge(self, other: Article) -> Article:
        """Fill missing fields from another representation of the same work."""
        for name in (
            "year",
            "journal",
            "volume",
            "issue",
            "pages",
            "doi",
            "pmid",
            "pmcid",
            "url",
            "abstract",
            "oa_pdf_url",
            "local_pdf",
            "source_item_type",
            "source_key",
            "source_library",
        ):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if not self.authors and other.authors:
            self.authors = other.authors
        if self.item_type in {"", "document"} and other.item_type:
            self.item_type = other.item_type
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def article_from_dict(data: object, *, strict: bool = True) -> Article:
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise ValueError("invalid article metadata")
    names = {field.name for field in fields(Article)}
    if strict and set(data) - names:
        raise ValueError("unknown article fields")
    data = {name: value for name, value in data.items() if name in names}
    authors = data.get("authors", [])
    if not isinstance(authors, list):
        raise ValueError("invalid authors")
    people = []
    for person in authors:
        if not isinstance(person, dict) or (strict and set(person) - {"family", "given", "orcid"}):
            raise ValueError("invalid author metadata")
        if any(not isinstance(person.get(key, ""), str) for key in ("family", "given")):
            raise ValueError("invalid author name")
        if person.get("orcid") is not None and not isinstance(person["orcid"], str):
            raise ValueError("invalid author identifier")
        people.append(
            Person(
                **{
                    key: value
                    for key, value in person.items()
                    if key in {"family", "given", "orcid"}
                }
            )
        )
    for name, value in data.items():
        if name in {"authors", "year", "score"}:
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid article field")
    year = data.get("year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ValueError("invalid article year")
    score = data.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
        raise ValueError("invalid article score")
    if score is not None and not math.isfinite(score):
        raise ValueError("invalid article score")
    values = dict(data)
    values["authors"] = people
    return Article(**values)
