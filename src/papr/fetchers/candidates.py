"""Identify and rank publisher PDF links without depending on a browser."""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit


def article_identity(url: str) -> tuple[str, str] | None:
    path = unquote(urlsplit(url).path).lower().rstrip("/")
    pmc = re.search(r"/articles/(?:instance/)?(?:pmc)?(\d+)(?:/|$)", path)
    if pmc and urlsplit(url).hostname in {
        "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    }:
        return "pmc", pmc[1]
    ovid = re.search(r"/jnls/([^/]+)/(?:abstract|fulltext|pdf)/([^/~]+)", path)
    if ovid:
        return "ovid", f"{ovid[1]}/{ovid[2]}"
    pii = re.search(r"/(?:pii|retrieve/pii)/([a-z0-9]+)", path)
    if pii:
        return "pii", pii[1]
    if "/journals/" in path and "/article" in path:
        return "journal", re.sub(r"\.(?:xml|html|pdf)$", "", path[path.index("/journals/") :])
    doi = re.search(r"/(10\.\d{4,9}/.+)", path)
    if doi:
        return "doi", re.sub(
            r"(?:\.(?:pdf|full|abstract)|/(?:pdf|full|abstract|pdfft))+$", "", doi[1]
        )
    article = re.search(r"/articles?/([^/]+)", path)
    if article:
        return "article", re.sub(r"\.(?:html|xml|pdf)$", "", article[1])
    return None


def _supplement(url: str) -> bool:
    path = unquote(urlsplit(url).path).lower()
    return bool(re.search(
        r"(?:^|[/_.-])(?:supplement(?:al|ary)?|supporting[-_]?information|"
        r"(?:mo)?esm\d*)(?:[/_.-]|$)|[._-]s\d{3}\.pdf$",
        path,
    ))


def rank_candidate_urls(
    page_url: str, urls: list[str], citation_url: str | None = None,
    *, allow_generic: bool = False,
) -> list[str]:
    """Reject conflicting article identities and prefer full PDFs over previews.

    Citation metadata may name an opaque CDN URL, but cannot override an
    explicitly different article identity or an explicit supplement filename.
    OA landing pages can allow same-origin generic links when neither URL has
    an article identity; browser discovery keeps its stricter path requirement.
    """
    page = urlsplit(page_url)
    identity = article_identity(page_url)
    trusted = urljoin(page_url, citation_url) if citation_url else None
    accepted: list[str] = []
    for candidate in ([trusted] if trusted else []) + urls:
        url = urljoin(page_url, candidate)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or url in accepted or _supplement(url):
            continue
        candidate_identity = article_identity(url)
        if identity and candidate_identity != identity:
            if url != trusted or candidate_identity is not None:
                continue
        elif not identity and url != trusted:
            page_path = re.sub(r"\.(?:html|xml|pdf)$", "", page.path).rstrip("/")
            candidate_path = re.sub(r"\.(?:html|xml|pdf)$", "", parsed.path).rstrip("/")
            matching_path = (
                candidate_path == page_path or candidate_path.startswith(page_path + "/")
            )
            generic = allow_generic and candidate_identity is None
            if (parsed.scheme, parsed.netloc) != (page.scheme, page.netloc):
                continue
            if not matching_path and not generic:
                continue
        accepted.append(url)

    def priority(url: str) -> int:
        path = urlsplit(url).path.lower()
        if "previewpdf" in path:
            return 100
        if url == trusted:
            return 0
        if "downloadpdf" in path or "/pdfft" in path or "/pdf/" in path:
            return 10
        return 20

    return sorted(accepted, key=priority)[:20]
