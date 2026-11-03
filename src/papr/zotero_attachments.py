"""Reconcile local PDF attachments and upload only to a verified desktop endpoint."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .progress import emit
from .zotero_api import (
    DesktopAPI,
    ZoteroError,
    item_data,
    list_response,
    object_key,
    object_response,
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def _local_attachment(api: DesktopAPI, item: dict) -> Path | None:
    response = api.request(
        "GET", f"{api.prefix}/items/{object_key(item)}/file/view/url", allow_missing=True
    )
    if response.status_code == 404:
        return None
    parsed = urlparse(response.text.strip())
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    path = Path(unquote(parsed.path))
    return path if path.is_file() else None


def has_pdf(api: DesktopAPI, parent_key: str, md5: str) -> bool:
    children = list_response(api.request(
        "GET", f"{api.prefix}/items/{parent_key}/children", params={"format": "json"}
    ))
    for child in children:
        data = item_data(child)
        if data.get("itemType") != "attachment" or data.get("contentType") != "application/pdf":
            continue
        path = _local_attachment(api, child)
        if path and digest(path) == md5:
            return True
    return False


def ensure_attachment(
    api: DesktopAPI, parent_key: str, pdf: Path, md5: str, *, reuse_existing_pdf: bool = False,
) -> tuple[dict, bool]:
    children = list_response(api.request(
        "GET", f"{api.prefix}/items/{parent_key}/children", params={"format": "json"}
    ))
    pending = None
    for child in children:
        data = item_data(child)
        if data.get("itemType") != "attachment":
            continue
        if data.get("contentType") != "application/pdf":
            continue
        # Stored MD5 may describe a remote file; verify the actual local attachment too.
        local = _local_attachment(api, child)
        if local and reuse_existing_pdf:
            try:
                with local.open("rb") as stream:
                    if b"%PDF-" in stream.read(1024):
                        # A group's existing PDF is authoritative, even if this export
                        # was compressed differently. Avoid adding a duplicate attachment.
                        return child, True
            except OSError:
                local = None
        if local and digest(local) == md5:
            return child, True
        if (
            local is None and data.get("linkMode") == "imported_file"
            and data.get("filename") == pdf.name and data.get("md5") in {None, "", md5}
        ):
            if pending is not None:
                raise ZoteroError(
                    "Multiple incomplete Zotero attachments match; resolve them first"
                )
            pending = child
    if pending:
        return pending, False
    return api.create(f"{api.prefix}/items", {
        "itemType": "attachment", "parentItem": parent_key, "linkMode": "imported_file",
        "title": pdf.name, "filename": pdf.name, "contentType": "application/pdf",
        "tags": [], "collections": [], "relations": {},
    }), False


def upload(api: DesktopAPI, attachment: dict, pdf: Path, md5: str) -> None:
    endpoint = f"{api.prefix}/items/{object_key(attachment)}/file"
    previous = item_data(attachment).get("md5")
    precondition = {"If-Match": f'"{previous}"'} if previous else {"If-None-Match": "*"}
    response = api.request("POST", endpoint, write=True, headers=precondition, data={
        "md5": md5, "filename": pdf.name, "filesize": str(pdf.stat().st_size),
        "mtime": str(pdf.stat().st_mtime_ns // 1_000_000),
    })
    upload = object_response(response)
    if upload.get("exists"):
        return
    url = upload.get("url", "")
    if not isinstance(url, str) or not isinstance(upload.get("uploadKey"), str):
        raise ZoteroError("Zotero returned invalid upload authorization")
    parsed, base = urlparse(url), urlparse(api.base)
    def expected_port(value):
        return value.port or (443 if value.scheme == "https" else 80)

    if (
        (parsed.scheme, parsed.hostname, expected_port(parsed))
        != (base.scheme, base.hostname, expected_port(base))
        or not re.fullmatch(re.escape(base.path) + r"/local/uploads/[\w-]+", parsed.path)
        or parsed.username or parsed.password or parsed.fragment or parsed.query
        or not upload.get("uploadKey")
        or upload.get("prefix") or upload.get("suffix")
    ):
        raise ZoteroError("Zotero returned an unexpected upload destination; PDF was not sent")
    emit("zotero", "Uploading compressed PDF to Zotero")
    with pdf.open("rb") as stream:
        response = api.client.post(
            url, content=stream,
            headers={"Content-Type": upload.get("contentType", "application/pdf")},
        )
    api.check(response)
    if response.status_code != 201:
        raise ZoteroError("Zotero did not confirm receiving the PDF; run again to retry")
    response = api.request(
        "POST", endpoint, write=True, headers=precondition, data={"upload": upload["uploadKey"]}
    )
    if response.status_code != 204:
        raise ZoteroError("Zotero did not confirm registering the PDF; run again to retry")
