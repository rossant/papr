from __future__ import annotations

import httpx

from . import __version__

USER_AGENT = f"papr/{__version__} (+https://github.com/rossant/papr)"


def client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=15.0),
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
