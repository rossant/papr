"""Known PDF routes derived only from an identified publisher article URL."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


def publisher_pdf_urls(page_url: str) -> list[str]:
    """Return original-PDF routes without leaving the current publisher/proxy.

    Some Ovid article pages expose only a scripted PDF button. Springer article
    pages likewise use a stable content/pdf route. These are deterministic view
    changes of the current article, not guesses from a DOI or related links.
    """
    parsed = urlsplit(page_url)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
    ):
        return []
    host = parsed.hostname.lower()
    path = None
    if host in {"www.ovid.com", "www-ovid-com.libproxy.ucl.ac.uk"}:
        match = re.fullmatch(
            r"/jnls/([^/]+)/(?:abstract|fulltext|pdf)/"
            r"(\d{8}-\d{9}-\d{5}(?:~[^/]+)?)/?", parsed.path,
        )
        if match:
            path = f"/jnls/{match[1]}/pdf/{match[2]}"
    elif host in {"link.springer.com", "link-springer-com.libproxy.ucl.ac.uk"}:
        match = re.fullmatch(
            r"/(?:article/|content/pdf/)?(10\.\d{4,9}/[^/]+?)(?:\.pdf)?/?", parsed.path,
        )
        if match:
            path = f"/content/pdf/{match[1]}.pdf"
    if path is None:
        return []
    return [urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))]
