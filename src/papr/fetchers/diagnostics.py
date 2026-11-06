"""Recognize PDF verification failures without exposing signed download URLs."""

import re
from urllib.parse import parse_qs, urlsplit


class PdfAccessError(RuntimeError):
    """An original PDF was located, but its access verification failed."""


def access_problem(url: str, text: str = "") -> str | None:
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    lower = text.casefold()
    if parsed.hostname in {"login.microsoftonline.com", "login.live.com", "shib-idp.ucl.ac.uk"}:
        return "Institutional sign-in is required before PDF access. Run papr login ucl."
    content_error = re.search(r"\b(CPE\d{5})\b", text, re.I)
    if (
        parsed.hostname in {"www.sciencedirect.com", "sciencedirect.com",
                            "www-sciencedirect-com.libproxy.ucl.ac.uk"}
        and "there was a problem providing the content you requested" in lower
        and content_error
    ):
        return (
            f"ScienceDirect returned a content error ({content_error[1].upper()}) "
            "instead of the article. Retry with --show-browser; if it persists, "
            "contact the library or publisher with the error code."
        )
    challenge_title = re.search(
        r"<title>\s*(?:just a moment|un instant)[\s.!…]*</title>", lower
    )
    if "_cf_chl_opt" in lower or (challenge_title and any(
        marker in lower for marker in ("cloudflare", "/cdn-cgi/challenge-platform/", "cf-chl-")
    )):
        return (
            "Publisher returned a Cloudflare browser verification challenge. "
            "Complete verification in a user-assisted browser or try another original source."
        )
    if "<title>client challenge</title>" in lower and "/_fs-ch-" in lower:
        return (
            "Publisher requires a JavaScript browser challenge before PDF access. "
            "Retry with --show-browser to complete verification in normal Chrome."
        )
    if params.get("c") == ["pdf_country_code_mismatch"] or "pdf_country_code_mismatch" in lower:
        return (
            "PDF server rejected a country mismatch between the institutional proxy and "
            "the PDF CDN. Try a repository copy or check the library's PDF-host proxy routing."
        )
    if params.get("fr") == ["RR-16"]:
        return (
            "Publisher PDF verification rejected browser automation (RR-16). "
            "Retry with --show-browser to use normally launched Chrome."
        )
    if "recaptcha/challengepage" in lower:
        return (
            "PDF download requires browser verification (reCAPTCHA). "
            "Retry with --show-browser and complete any verification in Chrome."
        )
    if "/craft/capi/cfts/" in parsed.path or "craft/challenge/pdf" in lower:
        return (
            "Publisher returned a PDF verification page instead of a PDF. "
            "Retry with --show-browser or try another original PDF source."
        )
    return None


def response_problem(
    url: str, text: str, status_code: int | None = None, *, expect_pdf: bool = False,
) -> str | None:
    """Distinguish blocked/login responses from readable article or viewer HTML."""
    problem = access_problem(url, text)
    if problem:
        return problem
    if isinstance(status_code, int) and status_code >= 400:
        return f"HTTP {status_code}"
    if expect_pdf and re.search(r"<(?:iframe|embed|object)\b", text, re.I):
        return "PDF endpoint returned an HTML viewer instead of PDF bytes"
    return None


def recovery_for(problem: str) -> str:
    if "sign-in is required" in problem:
        return "Run papr login ucl and complete institutional sign-in"
    if "ScienceDirect returned a content error" in problem:
        return "Retry with --show-browser; if it persists, contact the library or publisher"
    if "country mismatch" in problem:
        return "Try a repository copy or ask the library to check PDF-host proxy routing"
    if "HTML viewer" in problem:
        return "Follow the viewer's same-article PDF destination or try another original source"
    if "HTTP" in problem:
        return "Check publisher access or try a verified repository copy"
    return "Retry with --show-browser and complete browser verification"


def navigation_failure(exc: Exception) -> dict:
    """Retain recognized browser error codes without exposing raw signed URLs."""
    match = re.search(r"\bnet::ERR_[A-Z_]+\b", str(exc))
    code = match.group(0) if match else None
    return {
        "reason": f"Browser navigation failed ({code})" if code
        else "Browser could not open landing page",
        "recovery": (
            "Check the publisher URL and network/proxy routing; try another original source"
        ),
        **({"error_code": code} if code else {}),
    }
