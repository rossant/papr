from __future__ import annotations

import base64
import logging
import tempfile
import time
from pathlib import Path

from ..pdf import is_pdf
from ..progress import emit
from .diagnostics import PdfAccessError, access_problem

logger = logging.getLogger(__name__)


def browser_fetch_pdf(page, url: str, destination: Path) -> str | None:
    """Fetch PDF bytes with the browser's cookies and network stack."""
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


def browser_pdf(
    page, url: str, destination: Path, *, referer: str | None = None,
    verification_wait: float = 0,
) -> str | None:
    """Navigate to a PDF and save inline responses or browser downloads."""
    from playwright.sync_api import Error as PlaywrightError

    downloads = []

    def capture_download(download):
        downloads.append(download)

    emit("fetch", "Downloading PDF in browser")
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
                body = b""  # Native Chrome may have opened its inline PDF viewer.
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
                source = browser_fetch_pdf(page, url, destination)
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
