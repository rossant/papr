from __future__ import annotations

import html
import logging
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import Config
from ..http import client, http_deadline, remaining_time
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


def _read_response(
    c: httpx.Client, url: str, destination: Path | None = None
) -> tuple[httpx.Response, bytes]:
    """Read incrementally while retaining headers and URL for PDF/link detection."""
    detail = f"Downloading from {urlsplit(url).hostname or 'publisher'}"
    emit("fetch", detail)
    with c.stream("GET", url, timeout=remaining_time()) as response:
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
        chunks = bytearray()
        pdf = False
        output = None
        emit("fetch", detail, completed=completed, total=total, unit="bytes")
        try:
            for chunk in response.iter_bytes():
                remaining_time()
                completed += len(chunk)
                if output is not None:
                    output.write(chunk)
                else:
                    chunks.extend(chunk)
                    if len(chunks) >= 5:
                        pdf = is_pdf(chunks)
                        if pdf and destination is not None:
                            output = destination.open("wb")
                            output.write(chunks)
                            chunks = chunks[:5]
                        elif not pdf and len(chunks) > 2 * 1024 * 1024:
                            # Landing pages need only a bounded amount of HTML.
                            del chunks[2 * 1024 * 1024:]
                            break
                emit("fetch", detail, completed=completed, total=total, unit="bytes")
        finally:
            if output is not None:
                output.close()
        return response, bytes(chunks)



def _download(url: str, destination: Path | None = None) -> tuple[bytes, str, str] | None:
    with client() as c:
        r, content = _read_response(c, url, destination)
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
                pr, pdf_content = _read_response(c, pdf_url, destination)
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
            timeout=remaining_time(),
        )
        if r.status_code >= 400:
            return None
        best = r.json().get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url")


def candidate_urls(article: Article, config: Config) -> Iterator[str]:
    """Yield known direct sources before performing a lazy discovery request."""
    seen: set[str] = set()
    direct = []
    if article.oa_pdf_url:
        direct.append(article.oa_pdf_url)
    if article.pmcid:
        pmcid = article.pmcid.upper()
        if not pmcid.startswith("PMC"):
            pmcid = "PMC" + pmcid
        direct.append(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
    for url in direct:
        if url not in seen:
            seen.add(url)
            yield url
    try:
        remaining_time()
        upw = _unpaywall_url(article, config)
        if upw and upw not in seen:
            seen.add(upw)
            yield upw
    except Exception:
        logger.debug("Unpaywall lookup failed", exc_info=True)
    for url in (article.url, f"https://doi.org/{article.doi}" if article.doi else None):
        if url and url not in seen:
            seen.add(url)
            yield url


def fetch(article: Article, destination: Path, config: Config) -> str | None:
    emit("fetch", "Searching open-access PDF sources")
    with http_deadline():
        for url in candidate_urls(article, config):
            temporary = None
            try:
                remaining_time()
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, suffix=".oa.pdf", delete=False
                ) as output:
                    temporary = Path(output.name)
                result = _download(url, temporary)
                if not result:
                    continue
                content, final_url, _ = result
                # The public bytes mode remains useful for small callers and tests.
                if temporary.stat().st_size == 0:
                    temporary.write_bytes(content)
                size = temporary.stat().st_size
                temporary.replace(destination)
                emit("fetch", f"Downloaded PDF ({size:,} bytes)")
                return final_url
            except httpx.TimeoutException:
                logger.debug("PDF candidate timed out: %s", url, exc_info=True)
                try:
                    remaining_time()
                except httpx.TimeoutException:
                    break
            except Exception:
                logger.debug("PDF candidate download failed: %s", url, exc_info=True)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    return None
