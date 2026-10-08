from __future__ import annotations

from ..model import Article


def encode(article: Article) -> dict:
    out: dict = {
        "id": article.doi or article.pmid or article.citation_key,
        "type": article.item_type or "document",
        "title": article.title,
        "author": [
            {k: v for k, v in {"family": a.family, "given": a.given, "ORCID": a.orcid}.items() if v}
            for a in article.authors
        ],
    }
    if article.year:
        out["issued"] = {"date-parts": [[article.year]]}
    mapping = {
        "container-title": article.journal,
        "volume": article.volume,
        "issue": article.issue,
        "page": article.pages,
        "DOI": article.doi,
        "PMID": article.pmid,
        "PMCID": article.pmcid,
        "URL": article.url,
        "abstract": article.abstract,
    }
    out.update({k: v for k, v in mapping.items() if v})
    return out


def decode(item: dict) -> Article:
    from ..model import Person

    date_parts = (item.get("issued") or {}).get("date-parts") or []
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    return Article(
        title=item.get("title", ""),
        authors=[
            Person(family=a.get("family", ""), given=a.get("given", ""), orcid=a.get("ORCID"))
            for a in item.get("author", [])
        ],
        year=int(year) if year else None,
        journal=item.get("container-title"),
        volume=item.get("volume"),
        issue=item.get("issue"),
        pages=item.get("page"),
        doi=item.get("DOI"),
        pmid=item.get("PMID"),
        pmcid=item.get("PMCID"),
        url=item.get("URL"),
        abstract=item.get("abstract"),
        item_type=item.get("type") or "document",
        source="csl",
    )
