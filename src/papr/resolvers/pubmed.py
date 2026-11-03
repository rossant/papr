from __future__ import annotations

import xml.etree.ElementTree as ET

from ..config import Config
from ..http import client
from ..model import Article, Person
from .common import normalize_doi, score_article

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def by_pmid(pmid: str, config: Config) -> Article | None:
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    if config.email:
        params["email"] = config.email
    with client() as c:
        r = c.get(EFETCH, params=params)
        r.raise_for_status()
    root = ET.fromstring(r.text)
    return _from_record(root, pmid)


def search(query: str, config: Config) -> list[Article]:
    params = {"db": "pubmed", "term": query, "retmode": "json", "retmax": config.max_candidates}
    if config.email:
        params["email"] = config.email
    with client() as c:
        r = c.get(ESEARCH, params=params)
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
        if config.email:
            fetch_params["email"] = config.email
        r = c.get(EFETCH, params=fetch_params)
        r.raise_for_status()
    articles = []
    for record in ET.fromstring(r.text).findall("PubmedArticle"):
        pmid = record.findtext("MedlineCitation/PMID")
        if pmid and (article := _from_record(record, pmid)):
            article.score = score_article(query, article)
            articles.append(article)
    return articles


def _from_record(root: ET.Element, pmid: str) -> Article | None:
    citation = root.find(".//MedlineCitation")
    if citation is None:
        return None
    article_node = citation.find("Article")
    if article_node is None:
        return None
    title_node = article_node.find("ArticleTitle")
    title = "".join(title_node.itertext()) if title_node is not None else ""
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
