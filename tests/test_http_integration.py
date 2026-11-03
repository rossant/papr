"""Exercise shared-client lifetime through the real CLI prefetch boundary."""

import threading

import httpx
import pytest
from pdf_helpers import pdf_bytes

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
        return httpx.Response(200, content=pdf_bytes('paper'))

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


@pytest.mark.parametrize("options", [[], ["--dry-run"], ["--rename"]])
def test_local_commands_do_not_initialize_http(tmp_path, monkeypatch, options):
    local = tmp_path / "input.pdf"
    local.write_bytes(pdf_bytes('local paper'))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    monkeypatch.setattr(cli, "_resolve_item", lambda *a, **kw: (Article("Local"), local))

    def unexpected_client():
        raise AssertionError("Local processing must not initialize an HTTP client")

    monkeypatch.setattr(http, "_new_client", unexpected_client)
    args = cli._get_parser().parse_args([str(local), "--quiet", *options])
    config = Config(download_dir=tmp_path / "out", pdf_compress=False)
    assert cli._run_get(args, config) == 0
