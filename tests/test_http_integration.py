"""Exercise shared-client lifetime through the real CLI prefetch boundary."""

import threading

import httpx

from papr import cli, http
from papr.config import Config
from papr.model import Article


def test_cli_reuses_one_client_until_workers_finish(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    clients = []
    requests = []

    def respond(request):
        assert not clients[0].is_closed
        requests.append(request.url.path)
        barrier.wait(timeout=3)
        return httpx.Response(200, content=b"%PDF-1.7\npaper")

    def new_client():
        client = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(http, "_new_client", new_client)
    monkeypatch.setattr(
        cli,
        "_resolve_item",
        lambda item, *args, **kwargs: (
            Article(item, oa_pdf_url=f"https://publisher.example/{item}.pdf"),
            None,
        ),
    )
    args = cli._get_parser().parse_args(
        ["one", "two", "--jobs", "2", "--non-interactive", "--quiet"]
    )
    config = Config(download_dir=tmp_path / "out", pdf_compress=False)
    assert cli._run_get(args, config) == 0
    assert len(clients) == 1
    assert clients[0].is_closed
    assert sorted(requests) == ["/one.pdf", "/two.pdf"]
    assert len(list(config.download_dir.glob("*.pdf"))) == 2
