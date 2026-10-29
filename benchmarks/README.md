# Fetch performance benchmark

The October 29, 2026 comparison measures eight papers per batch, with three
independent runs per case and fresh cache/data/output directories for every run.
Numbers below are median elapsed seconds on Linux with Python 3.13.3. The baseline
is commit `79ee1e0`; the optimized runs use `2b9ce64`.

| Scenario | Before | After, 1 worker | After, 2 workers | After, 4 workers | Speedup, 4 workers |
| --- | ---: | ---: | ---: | ---: | ---: |
| Known direct PDF URL | 4.4880 | 2.4310 | 1.6700 | 1.6473 | 2.72× |
| Unpaywall discovery needed | 4.4433 | 3.2485 | 2.0644 | 2.0421 | 2.18× |
| Local Zotero PDF | 0.9041 | 0.0951 | 0.0841 | 0.0838 | 10.79× |

Compared with the optimized single-worker path, four workers reduce elapsed time
by another 32.2% for known PDF URLs and 37.1% when discovery is needed. The earlier
gains come from concurrent metadata providers, lazy Unpaywall discovery, reusable
HTTP connections, and checking SQLite attachments before Zotero's desktop API.

The known-URL case makes 24 requests instead of 32: eight unnecessary Unpaywall
lookups disappear. Discovery still needs 32 requests, but overlaps them. Every
optimized remote run peaks at two active server requests. All fixture endpoints
share one host, so the two-response host limit constrains both 2- and 4-worker
runs; four workers are more useful when papers come from different hosts.

The local scenario makes zero HTTP requests, compared with eight desktop API
requests before. Its library is a synthetic Zotero snapshot and each old desktop
request has an injected 80 ms delay. The large local ratio therefore demonstrates
the removed calls rather than predicting speedup for a particular real library.

## Method

`fetch_performance.py` runs the real CLI retrieval pipeline, metadata parsers,
HTTP client, PDF fetcher, exports, history, and manifests against a delayed local
HTTP/1.1 server. Metadata takes 80 ms per request, PDF delivery 200 ms, and
Unpaywall discovery 100 ms. The Unpaywall request is routed to the fixture through
the same HTTP client; Zotero lookup receives a synthetic in-memory snapshot.
No publisher service, personal configuration, library, or user cache is accessed.

The fixture disables Nagle's algorithm to avoid the test server introducing
delayed-ACK penalties for persistent connections. The PDF is a small valid blank
page, compression is disabled, and there is no OCR, browser, or Zotero upload.
Timing covers `_run_get`, including HTTP setup and output registration; Python
startup, input argument parsing, and fixture construction are excluded. Every run
checks successful reports, the output count, and byte-identical PDFs.

These controlled results demonstrate the implementation's effect under fixed
latencies. They are not production publisher timings; actual improvement depends
on host distribution, rate limits, PDF sizes, and downstream processing.

## Reproduce

From the updated checkout:

```bash
uv sync --locked --dev
git worktree add --detach /tmp/papr-before 79ee1e0
PYTHONPATH=/tmp/papr-before/src uv run python benchmarks/fetch_performance.py \
  --output /tmp/papr-before.json
PYTHONPATH=src uv run python benchmarks/fetch_performance.py \
  --jobs 1 --output /tmp/papr-after-1.json
PYTHONPATH=src uv run python benchmarks/fetch_performance.py \
  --jobs 2 --output /tmp/papr-after-2.json
PYTHONPATH=src uv run python benchmarks/fetch_performance.py \
  --jobs 4 --output /tmp/papr-after-4.json
```

Run comparisons sequentially, without concurrent test or benchmark processes.
Use `--papers`, `--repeats`, `--metadata-delay`, `--pdf-delay`, and
`--discovery-delay` to change the workload. Raw per-run measurements, request
counts, Python versions, and source revisions are in [results/](results/).
