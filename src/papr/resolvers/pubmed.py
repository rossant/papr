from __future__ import annotations

import xml.etree.ElementTree as ET

from ..config import Config
from ..http import client
from ..model import Article, Person
from .common import normalize_doi

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def by_pmid(pmid: str, config: Config) -> Article | None:
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    if config.email:
        params["email"] = config.email
    with client() as c:
        r = c.get(EFETCH, params=params)
        r.raise_for_status()
    root = ET.fromstring(r.text)
    citation = root.find(".//MedlineCitation")
    if citation is None:
        return None
    article_node = citation.find("Article")
    if article_node is None:
        return None
    title = "".join(article_node.findtext("ArticleTitle", default=""))
    authors = []
    for a in article_node.findall(".//Author"):
        authors.append(Person(family=a.findtext("LastName", ""), given=a.findtext("ForeName", "")))
    journal = article_node.findtext("Journal/Title")
    issue = article_node.find("Journal/JournalIssue")
    year = None
    if issue is not None:
        y = issue.findtext("PubDate/Year") or issue.findtext("PubDate/MedlineDate", "")[:4]
        year = int(y) if y.isdigit() else None
    id_nodes = root.findall(".//PubmedData/ArticleIdList/ArticleId")
    ids = {n.get("IdType"): (n.text or "") for n in id_nodes}
    return Article(
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        volume=issue.findtext("Volume") if issue is not None else None,
        issue=issue.findtext("Issue") if issue is not None else None,
        pages=article_node.findtext("Pagination/MedlinePgn"),
        doi=normalize_doi(ids.get("doi")),
        pmid=pmid,
        pmcid=ids.get("pmc"),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        source="pubmed",
    )
