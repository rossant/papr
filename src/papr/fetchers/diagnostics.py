"""Recognize PDF verification failures without exposing signed download URLs."""

from urllib.parse import parse_qs, urlsplit


class PdfAccessError(RuntimeError):
    """An original PDF was located, but its access verification failed."""


def access_problem(url: str, text: str = "") -> str | None:
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    lower = text.casefold()
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
