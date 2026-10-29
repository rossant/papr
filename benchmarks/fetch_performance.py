"""Repeatable, isolated CLI fetch benchmark against a delayed local HTTP server.

Run with PYTHONPATH pointing at the checkout to measure. No publisher services,
personal configuration, browser profiles, or user caches are accessed.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import platform
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pypdf import PdfWriter

from papr import cli, http, resolvers
from papr.config import Config
from papr.fetchers import oa
from papr.model import Article
from papr.resolvers import crossref, openalex, zotero
from papr.resolvers.session import resolution_session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--papers", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--metadata-delay", type=float, default=0.08)
    parser.add_argument("--pdf-delay", type=float, default=0.20)
    parser.add_argument("--discovery-delay", type=float, default=0.10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.jobs, args.papers, args.repeats) < 1:
        parser.error("jobs, papers and repeats must be positive")

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    pdf_bytes = buffer.getvalue()
    counts: Counter = Counter()
    guard = threading.Lock()
    active = 0
    peak = 0
    scenario = "known"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            # Avoid the test server's Nagle/delayed-ACK interaction penalizing
            # persistent connections by ~40 ms for each small response.
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *values):
            pass

        def do_GET(self):
            nonlocal active, peak
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            kind = path.strip("/").split("/")[0]
            with guard:
                counts[kind] += 1
                active += 1
                peak = max(peak, active)
            try:
                if kind == "pdf":
                    time.sleep(args.pdf_delay)
                    body, content_type = pdf_bytes, "application/pdf"
                else:
                    time.sleep(
                        args.discovery_delay if kind == "unpaywall" else args.metadata_delay
                    )
                    doi = path.removeprefix("/crossref/")
                    if kind == "openalex":
                        doi = parse_qs(parsed.query)["filter"][0].removeprefix("doi:")
                    index = doi.rsplit("-", 1)[-1]
                    pdf_url = f"{base}/pdf/{index}.pdf"
                    if kind == "crossref":
                        item = {
                            "DOI": doi,
                            "title": [f"Benchmark paper {index}"],
                            "author": [{"family": "Benchmark"}],
                            "published": {"date-parts": [[2024]]},
                            "type": "journal-article",
                        }
                        if scenario == "known":
                            item["link"] = [{"URL": pdf_url, "content-type": "application/pdf"}]
                        data = {"message": item}
                    elif kind == "openalex":
                        item = {
                            "doi": doi,
                            "title": f"Benchmark paper {index}",
                            "publication_year": 2024,
                            "type": "article",
                        }
                        if scenario == "known":
                            item["best_oa_location"] = {"pdf_url": pdf_url}
                        data = {"results": [item]}
                    elif kind == "unpaywall":
                        data = {"best_oa_location": {"url_for_pdf": pdf_url}}
                    else:
                        data = []
                    body, content_type = json.dumps(data).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with guard:
                    active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    crossref.BASE = f"{base}/crossref"
    openalex.BASE = f"{base}/openalex"
    # Restrict local DOI lookup to the synthetic Zotero snapshot below; never
    # discover sibling repositories or libraries in the invoking user's home.
    resolvers.LOCAL_DOI_LOOKUPS = (zotero.by_doi,)

    def unpaywall(article, config):
        # Same request and response shape as production, routed exclusively to
        # this fixture so its eager/lazy scheduling is what is being measured.
        with http.client() as client:
            response = client.get(f"{base}/unpaywall/{article.doi}")
            return response.json()["best_oa_location"]["url_for_pdf"]

    oa._unpaywall_url = unpaywall
    rows = []
    try:
        for scenario in ("known", "discovery", "local-zotero"):
            runs = []
            for _ in range(args.repeats):
                with tempfile.TemporaryDirectory(prefix="papr-benchmark-") as directory:
                    root = Path(directory)

                    class IsolatedConfig(Config):
                        @property
                        def cache_dir(self):
                            return self.benchmark_root / "cache"

                        @property
                        def data_dir(self):
                            return self.benchmark_root / "data"

                    config = IsolatedConfig(
                        download_dir=root / "output",
                        local_sources=scenario == "local-zotero",
                        zotero_api_url=f"{base}/zotero",
                        unpaywall_email="benchmark@example.org",
                        pdf_compress=False,
                    )
                    config.benchmark_root = root
                    inputs = [f"10.9999/bench-{index:03}" for index in range(args.papers)]
                    if scenario == "local-zotero":
                        local_pdf = root / "local.pdf"
                        local_pdf.write_bytes(pdf_bytes)
                        articles = [
                            Article(
                                title=f"Benchmark paper {index:03}",
                                doi=doi,
                                local_pdf=str(local_pdf),
                                source="zotero",
                                year=2024,
                            )
                            for index, doi in enumerate(inputs)
                        ]
                        zotero._sqlite_all = lambda cfg, items=articles: copy.deepcopy(items)
                    options = [
                        *inputs,
                        "--oa-only",
                        "--no-compress",
                        "--non-interactive",
                        "--quiet",
                        "--report",
                        str(root / "report.json"),
                    ]
                    cli_parser = cli._get_parser()
                    if "--jobs" in cli_parser._option_string_actions:
                        options += ["--jobs", str(args.jobs)]
                    elif args.jobs != 1:
                        raise ValueError("This checkout does not support --jobs")
                    parsed_args = cli_parser.parse_args(options)
                    with guard:
                        counts.clear()
                        peak = 0
                    output = io.StringIO()
                    started = time.perf_counter()
                    with contextlib.redirect_stdout(output), resolution_session():
                        result = cli._run_get(parsed_args, config)
                    elapsed = time.perf_counter() - started
                    report = json.loads((root / "report.json").read_text())
                    assert result == 0, output.getvalue()
                    assert len(report) == args.papers
                    assert all(row["status"] == "ok" for row in report)
                    exported = sorted((root / "output").glob("*.pdf"))
                    assert len(exported) == args.papers
                    assert all(path.read_bytes() == pdf_bytes for path in exported)
                    with guard:
                        requests = dict(counts)
                        peak_requests = peak
                    if scenario == "local-zotero":
                        assert not any(requests.get(k) for k in ("crossref", "openalex", "pdf"))
                    runs.append(
                        {
                            "seconds": round(elapsed, 4),
                            "requests": requests,
                            "peak_active_requests": peak_requests,
                        }
                    )
            rows.append(
                {
                    "scenario": scenario,
                    "median_seconds": round(statistics.median(r["seconds"] for r in runs), 4),
                    "runs": runs,
                }
            )
    finally:
        server.shutdown()
        server.server_close()
        serving.join()
    revision = subprocess.check_output(
        ["git", "-C", str(Path(cli.__file__).resolve().parents[2]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "revision": revision,
                "python": platform.python_version(),
                "platform": platform.system(),
                "jobs": args.jobs,
                "papers": args.papers,
                "repeats": args.repeats,
                "fixture_delays_seconds": {
                    "metadata": args.metadata_delay,
                    "pdf": args.pdf_delay,
                    "discovery": args.discovery_delay,
                },
                "results": rows,
            },
            indent=2,
        )
        + "\n"
    )
    for row in rows:
        print(f"{row['scenario']}: {row['median_seconds']:.4f}s (jobs={args.jobs})")


if __name__ == "__main__":
    main()
