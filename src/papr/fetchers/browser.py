"""Own a normal Chrome process for visible, user-assisted PDF acquisition."""

from __future__ import annotations

import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def native_context(playwright, profile: Path, executable: str):
    """Attach to a dedicated Chrome profile without Playwright's launch flags.

    Browser verification may still require user interaction; no CAPTCHA is solved
    or page verification code changed. Only the process launched here is closed.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            executable, f"--user-data-dir={profile.resolve()}",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
            "--no-first-run", "--no-default-browser-check", "about:blank",
        ],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    browser = None
    try:
        from playwright.sync_api import Error as PlaywrightError

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "Chrome exited before PDF retrieval. Close other windows using papr's "
                    "browser profile and retry --show-browser."
                )
            try:
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}", timeout=1000
                )
                break
            except PlaywrightError:
                time.sleep(0.1)
        if browser is None:
            raise RuntimeError("Chrome did not become ready for PDF retrieval within 15 seconds")
        # Let Playwright manage its own download artifact directory. Overriding
        # Browser.setDownloadBehavior here disconnects save_as() from the file.
        yield browser.contexts[0]
    finally:
        if browser is not None:
            try:
                browser.new_browser_cdp_session().send("Browser.close")
            except Exception:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
