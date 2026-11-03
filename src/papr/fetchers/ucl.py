from __future__ import annotations

import base64
import logging
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..config import Config
from ..model import Article
from ..progress import emit
from ..resolvers.common import normalize_pmcid
from .browser import native_context
from .candidates import rank_candidate_urls as _rank_candidate_urls
from .diagnostics import PdfAccessError, access_problem
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


@contextmanager
def _browser_context(playwright, config: Config, headless: bool):
    if not headless:
        executable = _browser_executable() or playwright.chromium.executable_path
        emit("fetch", "Opening normal Chrome for original PDF retrieval")
        with native_context(playwright, config.ucl_profile_dir, executable) as context:
            yield context
    else:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.ucl_profile_dir), headless=True,
            accept_downloads=True, **_launch_kwargs(),
        )
        try:
            yield context
        finally:
            context.close()


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
    with sync_playwright() as p, _browser_context(p, config, False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            f"{UCL_LOGIN}?url=https://doi.org/10.1038/nature12373",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        print("Complete the UCL SSO/MFA login in the browser.")
        input("Press Enter here once you are logged in...")


def _proxy_url(article: Article) -> str:
    publisher_url = article.url
    if publisher_url and urlsplit(publisher_url).hostname in {"doi.org", "dx.doi.org"}:
        publisher_url = None
    target = publisher_url or (f"https://doi.org/{article.doi}" if article.doi else article.url)
    if not target:
        raise RuntimeError("UCL fallback needs a DOI or landing-page URL")
    return f"{UCL_LOGIN}?url={quote(target, safe=':/?=&%')}"


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


def _browser_fetch_pdf(page, url: str, destination: Path) -> str | None:
    """Use the actual browser network/session after navigation or verification.

    Playwright's request client uses a different network stack; inline Chrome PDF
    viewers may also make response.body() unavailable. Neither is a missing PDF.
    """
    result = page.evaluate("""async url => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 30000);
        try {
        const response = await fetch(url, {credentials: 'include', signal: controller.signal});
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (!response.ok || String.fromCharCode(...bytes.slice(0, 5)) !== '%PDF-')
            return null;
        const parts = [];
        for (let offset = 0; offset < bytes.length; offset += 8192)
            parts.push(String.fromCharCode(...bytes.subarray(offset, offset + 8192)));
        return {url: response.url, data: btoa(parts.join(''))};
        } finally { clearTimeout(timer); }
    }""", url)
    if not isinstance(result, dict) or not isinstance(result.get("data"), str):
        return None
    content = base64.b64decode(result["data"], validate=True)
    if not is_pdf(content):
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return result["url"]


def _browser_pdf(
    page, url: str, destination: Path, *, referer: str | None = None,
    verification_wait: float = 0,
) -> str | None:
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
            if not any(message in str(exc) for message in (
                "net::ERR_ABORTED", "Download is starting",
            )):
                raise
            navigation_error = exc
        if response and response.ok:
            try:
                body = response.body()
            except PlaywrightError:
                body = b""  # Native Chrome may already have opened its inline PDF viewer.
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
            problem = access_problem(page.url, page.content())
            if problem and "reCAPTCHA" in problem and verification_wait > 0:
                emit("fetch", "Complete verification in Chrome; waiting up to 60 seconds")
                deadline = time.monotonic() + verification_wait
                while time.monotonic() < deadline and problem and not downloads:
                    page.wait_for_timeout(1000)
                    problem = access_problem(page.url, page.content())
            if problem:
                raise PdfAccessError(problem)
        if not downloads:
            try:
                source = _browser_fetch_pdf(page, url, destination)
                if source:
                    return source
            except PlaywrightError:
                logger.debug("In-browser PDF request failed", exc_info=True)
            problem = access_problem(page.url, page.content())
            if problem:
                raise PdfAccessError(problem)
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
    if not profile.exists() and headless:
        emit("fetch", "UCL access unavailable: no saved browser profile")
        return None
    sync_playwright = _playwright()
    from playwright.sync_api import Error as PlaywrightError

    emit("fetch", "Starting UCL browser session")
    with sync_playwright() as p, _browser_context(p, config, headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        pmcid = normalize_pmcid(article.pmcid)
        landings = [f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"] if pmcid else []
        if article.url or article.doi or not landings:
            landings.append(_proxy_url(article))
        problems = []
        for landing in dict.fromkeys(landings):
            try:
                emit("fetch", "Opening publisher page through UCL")
                response = page.goto(
                    landing, wait_until="domcontentloaded", timeout=90_000
                )
            except PlaywrightError as exc:
                problems.append(
                    "Browser could not open a PDF source. Check the publisher URL "
                    "or complete institutional login with `papr login ucl`."
                )
                logger.debug("PDF landing navigation failed (%s)", type(exc).__name__)
                continue
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
                try:
                    source = _browser_pdf(
                        page, url, destination, referer=browser_headers["Referer"],
                        verification_wait=0 if headless else 60,
                    )
                except PdfAccessError as exc:
                    problems.append(str(exc))
                    emit("fetch", str(exc))
                    continue
                except PlaywrightError as exc:
                    logger.debug("Browser PDF transfer failed (%s)", type(exc).__name__)
                    problems.append("A publisher PDF browser transfer failed; try another source.")
                    continue
                if source:
                    return source
            problem = access_problem(page.url, page.content())
            if problem:
                problems.append(problem)
        if problems:
            raise PdfAccessError(" ".join(dict.fromkeys(problems)))
    return None
