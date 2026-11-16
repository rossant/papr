"""Discover original PDFs through PMC's public, machine-readable AWS dataset."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import httpx

from ..acquisition import record_attempt
from ..http import remaining_time
from ..model import Article
from ..resolvers.common import normalize_doi

BASE = "https://pmc-oa-opendata.s3.amazonaws.com/"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def pdf_urls(c: httpx.Client, pmcid: str, article: Article) -> list[str]:
    """Inspect version metadata; version numbers alone do not identify publication status.

    PMC retired its OA package API in August 2026. The replacement provides
    anonymous HTTPS access, without scraping browser download challenges.
    https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/
    """
    prefixes: set[str] = set()
    token = None
    # A bounded, delimiter-based listing avoids downloading every article asset.
    for _ in range(4):
        params = {"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"}
        if token:
            params["continuation-token"] = token
        response = c.get(BASE, params=params, timeout=remaining_time())
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("PMC dataset listing is too large")
        root = ElementTree.fromstring(response.content)
        for entry in root.findall("s3:CommonPrefixes/s3:Prefix", NS):
            if entry.text and re.fullmatch(rf"{re.escape(pmcid)}\.\d+/", entry.text):
                prefixes.add(entry.text)
        if root.findtext("s3:IsTruncated", namespaces=NS) != "true":
            break
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)
        if not token:
            raise ValueError("PMC dataset listing omitted its continuation token")
    urls = []
    for prefix in sorted(prefixes, key=lambda p: int(p.split(".")[1].rstrip("/")))[:16]:
        version_id = prefix.rstrip("/")
        metadata_url = f"{BASE}{prefix}{version_id}.json"
        try:
            response = c.get(metadata_url, timeout=remaining_time())
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("pmcid") != pmcid:
                continue
            # Do not silently substitute an author manuscript for the published paper.
            if data.get("is_manuscript") not in (False, "no"):
                continue
            doi = normalize_doi(article.doi)
            found_doi = data.get("doi")
            if doi and (not isinstance(found_doi, str) or normalize_doi(found_doi) != doi):
                continue
            value = data.get("pdf_url")
            if not isinstance(value, str):
                continue
            parsed = urlsplit(value)
            key = parsed.path.lstrip("/")
            # Metadata must point to this version's main PDF, never a supplement
            # or an arbitrary external address. The md5 query is metadata only.
            if (
                parsed.scheme != "s3" or parsed.netloc != "pmc-oa-opendata"
                or key != f"{prefix}{version_id}.pdf"
            ):
                continue
            urls.append(BASE + quote(key, safe="/"))
        except (httpx.HTTPError, ValueError, TypeError):
            record_attempt("pmc_cloud", metadata_url, reason="PMC version metadata unavailable",
                           recovery="Try another article version or source")
    record_attempt("pmc_cloud", BASE, outcome="discovered" if urls else "unavailable",
                   reason="Published PMC dataset PDF locations found" if urls
                   else "No matching published PDF in the PMC dataset")
    return urls
