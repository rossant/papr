from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

from ..config import Config
from ..model import Article
from ..progress import emit
from .oa import is_pdf

UCL_LOGIN = "https://libproxy.ucl.ac.uk/login"
logger = logging.getLogger(__name__)


def _browser_executable() -> str | None:
    candidates = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    if sys.platform == "darwin":
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.exists():
            return str(chrome)
    return None


def _launch_kwargs() -> dict:
    executable = _browser_executable()
    return {"executable_path": executable} if executable else {}


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "UCL browser support is optional. Install it with `uv tool install 'papr[ucl]'` "
            "(or `uv sync --extra ucl`) and run `playwright install chromium`."
        ) from exc
    return sync_playwright


def login(config: Config) -> None:
    config.ucl_profile_dir.mkdir(parents=True, exist_ok=True)
    sync_playwright = _playwright()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(config.ucl_profile_dir),
            headless=False,
            **_launch_kwargs(),
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            f"{UCL_LOGIN}?url=https://doi.org/10.1038/nature12373",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        print("Complete the UCL SSO/MFA login in the browser.")
        input("Press Enter here once you are logged in...")
        context.close()


def _proxy_url(article: Article) -> str:
    publisher_url = article.url
    if publisher_url and urlsplit(publisher_url).hostname in {"doi.org", "dx.doi.org"}:
        publisher_url = None
    target = publisher_url or (f"https://doi.org/{article.doi}" if article.doi else article.url)
    if not target:
        raise RuntimeError("UCL fallback needs a DOI or landing-page URL")
    return f"{UCL_LOGIN}?url={quote(target, safe=':/?=&%')}"


def _article_identity(url: str) -> tuple[str, str] | None:
    path = unquote(urlsplit(url).path).lower()
    pii = re.search(r"/(?:pii|retrieve/pii)/([a-z0-9]+)", path)
    if pii:
        return "pii", pii[1]
    if "/journals/" in path and "/article" in path:
        return "journal", re.sub(r"\.(?:xml|html|pdf)$", "", path[path.index("/journals/") :])
    doi = re.search(r"/(10\.\d{4,9}/.+)", path)
    if doi:
        return "doi", re.sub(r"\.(?:pdf|full|abstract)$", "", doi[1])
    article = re.search(r"/articles?/([^/]+)", path)
    if article:
        return "article", re.sub(r"\.(?:html|xml|pdf)$", "", article[1])
    return None


def _rank_candidate_urls(
    page_url: str, urls: list[str], citation_url: str | None = None
) -> list[str]:
    identity = _article_identity(page_url)
    trusted = urljoin(page_url, citation_url) if citation_url else None
    accepted: list[str] = []
    for candidate in ([trusted] if trusted else []) + urls:
        url = urljoin(page_url, candidate)
        if urlsplit(url).scheme not in {"http", "https"} or url in accepted:
            continue
        candidate_identity = _article_identity(url)
        if identity and candidate_identity != identity:
            # Citation metadata can legitimately point to a CDN without an article identifier.
            if url != trusted or candidate_identity is not None:
                continue
        elif not identity and url != trusted:
            page_path = re.sub(r"\.(?:html|xml|pdf)$", "", urlsplit(page_url).path)
            candidate_path = re.sub(r"\.(?:html|xml|pdf)$", "", urlsplit(url).path)
            if candidate_path != page_path and not candidate_path.startswith(page_path + "/"):
                continue
        accepted.append(url)

    def priority(url: str) -> int:
        path = urlsplit(url).path.lower()
        if "previewpdf" in path:
            return 100
        if "downloadpdf" in path or "/pdfft" in path or "/pdf/" in path:
            return 0
        return 10 if url == trusted else 20

    return sorted(accepted, key=priority)[:20]


def _collect_candidate_urls(page) -> dict:
    from playwright.sync_api import Error as PlaywrightError

    script = r"""() => {
            const out = [];
            const m = document.querySelector('meta[name="citation_pdf_url"]');
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.href || '';
                const t = (a.textContent || '').toLowerCase();
                if (/\.pdf(?:$|[?#])/i.test(h) || t.includes('pdf') ||
                    t.includes('download')) out.push(h);
            }
            return {citation: m?.content || null, links: [...new Set(out)]};
        }"""
    for attempt in range(3):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
            return page.evaluate(script)
        except PlaywrightError as exc:
            message = str(exc).lower()
            navigation_race = (
                "execution context was destroyed" in message
                or "cannot find context with specified id" in message
            )
            if not navigation_race or attempt == 2:
                raise
            logger.debug("UCL page navigated during PDF link collection; retrying", exc_info=True)
            emit("fetch", "UCL page navigating; retrying PDF link discovery")
            page.wait_for_timeout(250 * (attempt + 1))
    return {"citation": None, "links": []}


def _candidate_urls(page) -> list[str]:
    emit("fetch", "Discovering publisher PDF links through UCL")
    for attempt in range(11):
        result = _collect_candidate_urls(page)
        urls = _rank_candidate_urls(page.url, result["links"], result["citation"])
        if urls or attempt == 10:
            return urls
        page.wait_for_timeout(1_000)
    return []


def _browser_pdf(page, url: str, destination: Path, *, referer: str | None = None) -> str | None:
    from playwright.sync_api import Error as PlaywrightError

    downloads = []

    def capture_download(download):
        downloads.append(download)

    emit("fetch", "Downloading PDF through UCL browser")
    page.on("download", capture_download)
    navigation_error = None
    try:
        response = None
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=30_000, referer=referer or page.url
            )
        except PlaywrightError as exc:
            if "net::ERR_ABORTED" not in str(exc):
                raise
            navigation_error = exc
        if response and response.ok:
            body = response.body()
            if is_pdf(body):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(body)
                return response.url
        # Chromium reports ERR_ABORTED when a navigation becomes a download.
        for _ in range(20):
            if downloads:
                break
            page.wait_for_timeout(250)
        if not downloads:
            if navigation_error is not None:
                raise navigation_error
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".browser.pdf", delete=False
        ) as file:
            temp = Path(file.name)
        try:
            downloads[0].save_as(temp)
            with temp.open("rb") as file:
                valid = is_pdf(file.read(5))
            if not valid:
                return None
            temp.replace(destination)
            return downloads[0].url
        finally:
            temp.unlink(missing_ok=True)
    finally:
        page.remove_listener("download", capture_download)


def fetch(
    article: Article, destination: Path, config: Config, *, headless: bool = True
) -> str | None:
    profile = config.ucl_profile_dir
    if not profile.exists():
        emit("fetch", "UCL access unavailable: no saved browser profile")
        return None
    sync_playwright = _playwright()
    from playwright.sync_api import Error as PlaywrightError

    emit("fetch", "Starting UCL browser session")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=headless, accept_downloads=True, **_launch_kwargs()
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            try:
                emit("fetch", "Opening publisher page through UCL")
                response = page.goto(
                    _proxy_url(article), wait_until="domcontentloaded", timeout=90_000
                )
            except PlaywrightError as exc:
                if "net::ERR_" not in str(exc):
                    raise
                raise RuntimeError(
                    "UCL could not open the publisher page. Check the publisher landing URL "
                    "and complete UCL login with `papr login ucl`."
                ) from exc
            if response:
                ctype = response.headers.get("content-type", "")
                if "application/pdf" in ctype.lower():
                    body = response.body()
                    if is_pdf(body, ctype):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                        return response.url
            urls = _candidate_urls(page)
            browser_headers = {
                "User-Agent": page.evaluate("() => navigator.userAgent"),
                "Referer": page.url,
            }
            for url in urls:
                emit("fetch", "Requesting publisher PDF through UCL")
                try:
                    r = context.request.get(url, timeout=60_000, headers=browser_headers)
                    body = r.body()
                    if r.ok and is_pdf(body, r.headers.get("content-type", "")):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                        return r.url
                except PlaywrightError as exc:
                    logger.debug("UCL PDF request failed (%s)", type(exc).__name__)
                source = _browser_pdf(page, url, destination, referer=browser_headers["Referer"])
                if source:
                    return source
        finally:
            context.close()
    return None
