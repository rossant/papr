from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from ..config import Config
from ..progress import emit

BASE = "https://api.mistral.ai/v1"


class MistralOcrError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.getenv("MISTRAL_API_KEY"))


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("retry-after", "").strip()
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = 2**attempt
        if not 0 <= delay <= 30:
            if delay < 0:
                return 0
            return 31
        return delay
    return 2**attempt


def _status_error(response: httpx.Response, api_key: str, signed_url: str | None) -> str:
    message = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            detail = data.get("message") or data.get("detail")
            if isinstance(detail, str):
                message = detail
    except ValueError:
        pass
    message = message.replace(api_key, "[redacted]")
    if signed_url:
        message = message.replace(signed_url, "[redacted URL]")
    message = re.sub(r"https?://\S+", "[redacted URL]", message)[:300]
    result = f"Mistral API returned HTTP {response.status_code}"
    if message:
        result += f": {message}"
    if response.status_code == 429:
        result += ". Retry later and check your Mistral account's rate limits and quota."
    elif response.status_code == 503:
        result += ". The service is unavailable; retry later."
    return result


def to_markdown(pdf: Path, config: Config) -> str:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise MistralOcrError("MISTRAL_API_KEY is not set")
    headers = {"Authorization": f"Bearer {api_key}"}
    file_id: str | None = None
    signed_url: str | None = None
    with httpx.Client(headers=headers, timeout=httpx.Timeout(180.0, connect=30.0)) as c:
        try:
            emit("process", "Uploading PDF to Mistral")
            with pdf.open("rb") as f:
                r = c.post(
                    f"{BASE}/files",
                    data={"purpose": "ocr"},
                    files={"file": (pdf.name, f, "application/pdf")},
                )
            r.raise_for_status()
            file_id = r.json()["id"]

            emit("process", "Preparing Mistral OCR request")
            r = c.get(f"{BASE}/files/{file_id}/url", params={"expiry": 1})
            r.raise_for_status()
            signed_url = r.json()["url"]

            for attempt in range(3):
                emit("process", "Mistral OCR · waiting for response")
                r = c.post(
                    f"{BASE}/ocr",
                    json={
                        "model": config.mistral_model,
                        "document": {"type": "document_url", "document_url": signed_url},
                    },
                )
                if r.status_code not in {429, 503} or attempt == 2:
                    break
                delay = _retry_delay(r, attempt)
                if delay > 30:
                    break
                emit("process", f"Mistral HTTP {r.status_code}; retrying in {delay:g}s")
                time.sleep(delay)
            r.raise_for_status()
            pages = r.json().get("pages", [])
            if not pages:
                raise MistralOcrError("Mistral OCR returned no pages")
            emit("process", f"Received OCR for {len(pages)} pages")
            return "\n\n".join(page.get("markdown", "") for page in pages).strip() + "\n"
        except httpx.HTTPStatusError as exc:
            raise MistralOcrError(_status_error(exc.response, api_key, signed_url)) from exc
        except httpx.HTTPError as exc:
            raise MistralOcrError(
                f"Mistral API request failed ({type(exc).__name__}); check your connection "
                "and retry."
            ) from exc
        finally:
            if file_id:
                try:
                    emit("process", "Removing temporary Mistral upload")
                    c.delete(f"{BASE}/files/{file_id}")
                except Exception:
                    pass
