"""Opt-in local-server regression for Chrome attachment downloads over CDP."""

import io
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from pypdf import PdfWriter

from papr.fetchers.browser import native_context
from papr.fetchers.ucl import _browser_executable, _browser_pdf

pytestmark = pytest.mark.skipif(
    os.environ.get("PAPR_RUN_BROWSER_TESTS") != "1",
    reason="set PAPR_RUN_BROWSER_TESTS=1 to open normal Chrome against a local test server",
)


def test_native_chrome_saves_original_attachment_bytes(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    executable = _browser_executable()
    if not executable:
        pytest.skip("Chrome/Chromium is not installed")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    original = buffer.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="original.pdf"')
            self.send_header("Content-Length", str(len(original)))
            self.end_headers()
            self.wfile.write(original)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            with native_context(p, tmp_path / "profile", executable) as context:
                page = context.pages[0] if context.pages else context.new_page()
                destination = tmp_path / "original.pdf"
                url = f"http://127.0.0.1:{server.server_port}/original.pdf"
                assert _browser_pdf(page, url, destination) == url
                assert destination.read_bytes() == original
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
