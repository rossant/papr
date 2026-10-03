from __future__ import annotations

import os
from pathlib import Path

import httpx

from ..config import Config

BASE = "https://api.mistral.ai/v1"


class MistralOcrError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.getenv("MISTRAL_API_KEY"))


def to_markdown(pdf: Path, config: Config) -> str:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise MistralOcrError("MISTRAL_API_KEY is not set")
    headers = {"Authorization": f"Bearer {api_key}"}
    file_id: str | None = None
    with httpx.Client(headers=headers, timeout=httpx.Timeout(180.0, connect=30.0)) as c:
        try:
            with pdf.open("rb") as f:
                r = c.post(
                    f"{BASE}/files",
                    data={"purpose": "ocr"},
                    files={"file": (pdf.name, f, "application/pdf")},
                )
            r.raise_for_status()
            file_id = r.json()["id"]

            r = c.get(f"{BASE}/files/{file_id}/url", params={"expiry": 1})
            r.raise_for_status()
            signed_url = r.json()["url"]

            r = c.post(
                f"{BASE}/ocr",
                json={
                    "model": config.mistral_model,
                    "document": {"type": "document_url", "document_url": signed_url},
                },
            )
            r.raise_for_status()
            pages = r.json().get("pages", [])
            if not pages:
                raise MistralOcrError("Mistral OCR returned no pages")
            return "\n\n".join(page.get("markdown", "") for page in pages).strip() + "\n"
        except httpx.HTTPError as exc:
            raise MistralOcrError(str(exc)) from exc
        finally:
            if file_id:
                try:
                    c.delete(f"{BASE}/files/{file_id}")
                except Exception:
                    pass
