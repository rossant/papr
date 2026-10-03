from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    oa_pdf_url: str | None = None
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

    def merge(self, other: "Article") -> "Article":
        """Fill missing fields from another representation of the same work."""
        for name in (
            "year", "journal", "volume", "issue", "pages", "doi", "pmid", "pmcid",
            "url", "abstract", "oa_pdf_url",
        ):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if not self.authors and other.authors:
            self.authors = other.authors
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
