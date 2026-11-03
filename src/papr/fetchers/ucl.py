from __future__ import annotations

import logging
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..acquisition import record_attempt
from ..config import Config
from ..model import Article
from ..pdf import PdfValidationError, is_pdf, validate_pdf
from ..progress import emit
from ..resolvers.common import normalize_pmcid
from . import browser_pdf as _browser_pdf_transport
from .browser import native_context
from .candidates import rank_candidate_urls as _rank_candidate_urls
from .diagnostics import PdfAccessError, access_problem

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


def fetch(
    article: Article, destination: Path, config: Config, *, headless: bool = True
) -> str | None:
    profile = config.ucl_profile_dir
    if not profile.exists() and headless:
        emit("fetch", "UCL access unavailable: no saved browser profile")
        record_attempt("ucl", outcome="unavailable", reason="No saved browser profile",
                       recovery="Run papr login ucl before retrying institutional access")
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
                record_attempt("ucl", landing, reason="Browser could not open landing page",
                               recovery="Run papr login ucl or check the publisher URL")
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
                        if _save_pdf(body, destination, article, response.url):
                            return response.url
            urls = _candidate_urls(page)
            record_attempt(
                "ucl", landing, outcome="discovered" if urls else "failed",
                reason="Publisher PDF links found" if urls else "No matching PDF links found",
                recovery=None if urls else "Try another repository or import an original PDF",
            )
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
                        if _save_pdf(body, destination, article, r.url):
                            return r.url
                    else:
                        problem = access_problem(r.url, body.decode("utf-8", errors="replace"))
                        record_attempt(
                            "ucl", r.url, status_code=r.status,
                            reason=problem or "Browser HTTP response is not a PDF",
                            recovery="Retry with --show-browser or try another repository",
                        )
                except PlaywrightError as exc:
                    record_attempt("ucl", url, reason="Browser HTTP request failed",
                                   recovery="Retry with --show-browser")
                    logger.debug("UCL PDF request failed (%s)", type(exc).__name__)
                try:
                    source = _browser_pdf_transport.browser_pdf(
                        page, url, destination, referer=browser_headers["Referer"],
                        verification_wait=0 if headless else 60,
                    )
                except PdfAccessError as exc:
                    record_attempt("browser", url, reason=str(exc),
                                   recovery="Retry with --show-browser or try another repository")
                    problems.append(str(exc))
                    emit("fetch", str(exc))
                    continue
                except PlaywrightError as exc:
                    record_attempt("browser", url, reason="Browser PDF transfer failed",
                                   recovery="Try another original PDF source")
                    logger.debug("Browser PDF transfer failed (%s)", type(exc).__name__)
                    problems.append("A publisher PDF browser transfer failed; try another source.")
                    continue
                if source:
                    try:
                        validate_pdf(destination, article)
                    except PdfValidationError as exc:
                        record_attempt("browser", source, reason=str(exc),
                                       recovery="Try another original PDF source")
                        destination.unlink(missing_ok=True)
                    else:
                        record_attempt("browser", source, outcome="success")
                        return source
                else:
                    record_attempt("browser", url, reason="Browser returned no PDF",
                                   recovery="Try another original PDF source")
            problem = access_problem(page.url, page.content())
            if problem:
                record_attempt("ucl", page.url, reason=problem,
                               recovery="Retry with --show-browser or try another repository")
                problems.append(problem)
        if problems:
            raise PdfAccessError(" ".join(dict.fromkeys(problems)))
    return None


def _save_pdf(content: bytes, destination: Path, article: Article, url: str) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    try:
        validate_pdf(destination, article)
    except PdfValidationError as exc:
        record_attempt("ucl", url, reason=str(exc), recovery="Try another original PDF source")
        destination.unlink(missing_ok=True)
        return False
    record_attempt("ucl", url, outcome="success")
    return True
