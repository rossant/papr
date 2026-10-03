from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import Config
from ..http import client
from ..model import Article
from ..progress import emit

logger = logging.getLogger(__name__)

PDF_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
    re.I,
)
PDF_LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)


def is_pdf(content: bytes, content_type: str = "") -> bool:
    # Publishers sometimes label error pages as PDFs; require the file signature.
    return content.startswith(b"%PDF-")


def _read_response(c: httpx.Client, url: str) -> tuple[httpx.Response, bytes]:
    """Read incrementally while retaining headers and URL for PDF/link detection."""
    detail = f"Downloading from {urlsplit(url).hostname or 'publisher'}"
    emit("fetch", detail)
    with c.stream("GET", url) as response:
        if response.status_code >= 400:
            return response, b""
        total = None
        # iter_bytes yields decoded bytes; compressed Content-Length describes the wire data.
        if response.headers.get("content-encoding", "identity").lower() == "identity":
            try:
                length = int(response.headers.get("content-length", ""))
                if length >= 0:
                    total = length
            except ValueError:
                pass
        completed = 0
        chunks = []
        emit("fetch", detail, completed=completed, total=total, unit="bytes")
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            completed += len(chunk)
            emit("fetch", detail, completed=completed, total=total, unit="bytes")
        return response, b"".join(chunks)


def _download(url: str) -> tuple[bytes, str, str] | None:
    with client() as c:
        r, content = _read_response(c, url)
        if r.status_code >= 400:
            return None
        ctype = r.headers.get("content-type", "")
        if is_pdf(content, ctype):
            return content, str(r.url), ctype
        text = content.decode(r.encoding or "utf-8", errors="replace")
        if "html" not in ctype.lower() and not text.lstrip().startswith("<"):
            return None
        candidates = [html.unescape(x) for x in PDF_META_RE.findall(text)]
        candidates += [html.unescape(x) for x in PDF_LINK_RE.findall(text)]
        for candidate in candidates[:8]:
            pdf_url = urljoin(str(r.url), candidate)
            try:
                emit("fetch", "Following publisher PDF link")
                pr, pdf_content = _read_response(c, pdf_url)
            except httpx.HTTPError:
                logger.debug("PDF link download failed: %s", pdf_url, exc_info=True)
                continue
            if pr.status_code < 400 and is_pdf(pdf_content, pr.headers.get("content-type", "")):
                return pdf_content, str(pr.url), pr.headers.get("content-type", "")
    return None


def _unpaywall_url(article: Article, config: Config) -> str | None:
    if not article.doi or not config.unpaywall_email:
        return None
    emit("fetch", "Searching Unpaywall for open-access PDF")
    with client() as c:
        r = c.get(
            f"https://api.unpaywall.org/v2/{article.doi}",
            params={"email": config.unpaywall_email},
        )
        if r.status_code >= 400:
            return None
        best = r.json().get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url")


def candidate_urls(article: Article, config: Config) -> list[str]:
    urls: list[str] = []
    if article.oa_pdf_url:
        urls.append(article.oa_pdf_url)
    try:
        upw = _unpaywall_url(article, config)
        if upw:
            urls.append(upw)
    except Exception:
        logger.debug("Unpaywall lookup failed", exc_info=True)
    if article.pmcid:
        pmcid = article.pmcid.upper()
        if not pmcid.startswith("PMC"):
            pmcid = "PMC" + pmcid
        urls.append(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
    if article.url:
        urls.append(article.url)
    if article.doi:
        urls.append(f"https://doi.org/{article.doi}")
    seen: set[str] = set()
    return [u for u in urls if u and not (u in seen or seen.add(u))]


def fetch(article: Article, destination: Path, config: Config) -> str | None:
    emit("fetch", "Searching open-access PDF sources")
    for url in candidate_urls(article, config):
        try:
            result = _download(url)
        except Exception:
            logger.debug("PDF candidate download failed: %s", url, exc_info=True)
            continue
        if not result:
            continue
        content, final_url, _ = result
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        emit("fetch", f"Downloaded PDF ({len(content):,} bytes)")
        return final_url
    return None
