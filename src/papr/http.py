from __future__ import annotations

import httpx

USER_AGENT = "papr/0.1 (+https://github.com/rossant/papr)"


def client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0),
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
