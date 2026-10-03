from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from ..config import Config
from ..model import Article
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
    target = f"https://doi.org/{article.doi}" if article.doi else article.url
    if not target:
        raise RuntimeError("UCL fallback needs a DOI or landing-page URL")
    return f"{UCL_LOGIN}?url={quote(target, safe=':/?=&%')}"


def _candidate_urls(page) -> list[str]:
    urls = page.evaluate(
        r"""() => {
            const out = [];
            const m = document.querySelector('meta[name="citation_pdf_url"]');
            if (m?.content) out.push(m.content);
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.href || '';
                const t = (a.textContent || '').toLowerCase();
                if (/\.pdf(?:$|[?#])/i.test(h) || t.includes('pdf') ||
                    t.includes('download')) out.push(h);
            }
            return [...new Set(out)];
        }"""
    )
    return urls[:20]


def fetch(article: Article, destination: Path, config: Config) -> str | None:
    profile = config.ucl_profile_dir
    if not profile.exists():
        return None
    sync_playwright = _playwright()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=True, **_launch_kwargs()
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            response = page.goto(_proxy_url(article), wait_until="domcontentloaded", timeout=90_000)
            if response:
                ctype = response.headers.get("content-type", "")
                if "application/pdf" in ctype.lower():
                    body = response.body()
                    if is_pdf(body, ctype):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                        return response.url
            for url in _candidate_urls(page):
                try:
                    r = context.request.get(url, timeout=60_000)
                    body = r.body()
                    if r.ok and is_pdf(body, r.headers.get("content-type", "")):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                        return r.url
                except Exception:
                    logger.debug("UCL PDF candidate failed: %s", url, exc_info=True)
                    continue
        finally:
            context.close()
    return None
