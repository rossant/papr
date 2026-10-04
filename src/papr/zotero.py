"""Save papers through Zotero 10's authorized local API, without database writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .config import Config
from .model import Article
from .progress import emit, pause
from .resolvers.common import extract_year, normalize_doi


class ZoteroError(RuntimeError):
    """An actionable local Zotero error, without authentication details."""


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def _data(item: dict) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("data", item), dict):
        raise ZoteroError("Zotero returned invalid item data")
    return item.get("data", item)


def _key(item: dict) -> str:
    data = _data(item)
    key = item.get("key") or data.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key):
        raise ZoteroError("Zotero returned an invalid object key")
    return key


def _object_response(response: httpx.Response) -> dict:
    value = response.json()
    if not isinstance(value, dict):
        raise ZoteroError("Zotero returned an invalid response object")
    return value


def _list_response(response: httpx.Response) -> list[dict]:
    value = response.json()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ZoteroError("Zotero returned an invalid list response")
    return value


class _API:
    def __init__(self, client: httpx.Client, config: Config, interactive: bool):
        self.client = client
        self.base = config.zotero_api_url.rstrip("/")
        parsed = urlparse(self.base)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ZoteroError("Zotero saving requires a local desktop API URL")
        self.interactive = interactive
        self.headers = {"Zotero-API-Version": "3"}
        response = self.request("GET", "/")
        server = response.headers.get("Zotero-Server-ID")
        if not server:
            raise ZoteroError("Saving requires Zotero 10 or later; upgrade the desktop application")
        self.headers["Zotero-Server-ID"] = server
        partition = hashlib.sha256(server.encode()).hexdigest()
        self.key_file = config.config_dir / "zotero" / f"{partition}.json"
        self.api_key = None
        self.group_id = config.zotero_group_id
        self.prefix = f"/groups/{self.group_id}" if self.group_id is not None else "/users/0"
        self.library_name = None
        if self.group_id is not None:
            try:
                response = self.request("GET", self.prefix, allow_missing=True)
            except ZoteroError:
                raise ZoteroError(
                    f"Zotero group {self.group_id} is unavailable or inaccessible; "
                    "check membership and sync the group in Zotero before saving"
                ) from None
            if response.status_code == 404:
                raise ZoteroError(
                    f"Zotero group {self.group_id} is not available locally; sync it in Zotero"
                )
            group = _object_response(response)
            data = _data(group)
            identifier = group.get("id", data.get("id"))
            name = data.get("name")
            if str(identifier) != str(self.group_id) or not isinstance(name, str) or not name:
                raise ZoteroError("Zotero returned invalid group metadata; saving was stopped")
            self.library_name = name
        try:
            saved = json.loads(self.key_file.read_text())
            if saved.get("server_id") == server and isinstance(saved.get("key"), str):
                self.api_key = saved["key"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def request(
        self, method: str, path: str, *, write: bool = False,
        allow_missing: bool = False, **kwargs,
    ):
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(2):
            if write and not self.api_key:
                self.authorize()
            headers = {**self.headers, **extra_headers}
            if write:
                headers["Zotero-API-Key"] = self.api_key
            response = self.client.request(method, self.base + path, headers=headers, **kwargs)
            if allow_missing and response.status_code == 404:
                return response
            if write and response.status_code == 401 and attempt == 0:
                self.api_key = None
                self.key_file.unlink(missing_ok=True)
                continue
            self.check(response)
            return response
        raise ZoteroError("Zotero authorization expired; run again and allow papr in Zotero")

    @staticmethod
    def check(response: httpx.Response) -> None:
        code = response.status_code
        if code < 400:
            return
        if code == 403:
            raise ZoteroError(
                "Zotero access denied. In Zotero Settings → Advanced, enable “Allow other "
                "applications on this computer to communicate with Zotero”, then allow papr"
            )
        if code == 401:
            raise ZoteroError("Zotero authorization expired; allow papr again in Zotero")
        if code == 412:
            raise ZoteroError("Zotero changed during saving; run again to use its current version")
        if code == 429:
            raise ZoteroError(
                "Zotero authorization was requested too often; wait a minute and retry"
            )
        raise ZoteroError(f"Zotero request failed (HTTP {code}); the downloaded PDF is preserved")

    def authorize(self) -> None:
        if not self.interactive:
            raise ZoteroError(
                "Zotero write permission is required. Run interactively and choose "
                "Always Allow in Zotero to enable unattended saving"
            )
        emit("zotero", "Waiting for permission in Zotero; choose Always Allow for future saves")
        with pause():
            print(
                "Allow papr in the Zotero permission dialog. Choose Always Allow "
                "to save future papers without another prompt.", file=sys.stderr,
            )
            response = self.client.post(
                self.base + "/local/authorize", headers=self.headers,
                json={"appName": "papr"}, timeout=120.0,
            )
        self.check(response)
        permission = _object_response(response)
        key = permission.get("key")
        if not isinstance(key, str) or not key:
            raise ZoteroError("Zotero did not return write permission")
        self.api_key = key
        if permission.get("remember") is True:
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.key_file.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump({"server_id": self.headers["Zotero-Server-ID"], "key": key}, stream)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.key_file)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def create(self, endpoint: str, data: dict) -> dict:
        response = self.request(
            "POST", endpoint, write=True, json=[data],
            headers={"Zotero-Write-Token": uuid.uuid4().hex},
        )
        result = _object_response(response)
        for field in ("successful", "success", "unchanged", "failed"):
            if not isinstance(result.get(field, {}), dict):
                raise ZoteroError("Zotero returned an invalid save result")
        if result.get("failed"):
            raise ZoteroError("Zotero could not save the item; the downloaded PDF is preserved")
        saved = result.get("successful", {}).get("0")
        if isinstance(saved, dict):
            _data(saved)
            _key(saved)
            return saved
        key = result.get("success", {}).get("0") or result.get("unchanged", {}).get("0")
        if key:
            item = _object_response(self.request("GET", f"{endpoint}/{key}"))
            _data(item)
            _key(item)
            return item
        raise ZoteroError("Zotero did not confirm saving the item")


def _find_parent(api: _API, article: Article, *, pdf_md5: str | None = None) -> dict | None:
    doi = normalize_doi(article.doi)

    def query(value: str) -> list[dict]:
        return _list_response(api.request(
            "GET", f"{api.prefix}/items/top",
            params={"q": value, "qmode": "everything", "format": "json"},
        ))

    def unique(matches: list[dict]) -> dict | None:
        if len(matches) > 1:
            raise ZoteroError(
                "Multiple Zotero items match this paper; merge the duplicates before saving"
            )
        return matches[0] if matches else None

    if doi:
        matched = unique([
            item for item in query(doi)
            if normalize_doi(_data(item).get("DOI")) == doi
            and _data(item).get("itemType") not in {"attachment", "note", "annotation"}
        ])
        if matched:
            return matched

    expected_authors = [_normalized(person.family) for person in article.authors]
    if not _normalized(article.title):
        return None
    incomplete = article.year is None or not expected_authors or not all(expected_authors)
    matches = []
    potential = []
    for item in query(article.title):
        data = _data(item)
        if data.get("itemType") in {"attachment", "note", "annotation"}:
            continue
        if _normalized(data.get("title", "")) != _normalized(article.title):
            continue
        existing_doi = normalize_doi(data.get("DOI"))
        if doi and existing_doi and existing_doi != doi:
            continue
        existing_year = extract_year(data.get("date"))
        if article.year is not None and existing_year is not None and article.year != existing_year:
            continue
        if incomplete:
            potential.append(item)
            # A content hash proves identity when bibliographic fields cannot do so.
            if pdf_md5 and _has_pdf(api, _key(item), pdf_md5):
                matches.append(item)
            continue
        creators = data.get("creators", [])
        if not isinstance(creators, list) or not all(
            isinstance(person, dict) for person in creators
        ):
            raise ZoteroError("Zotero returned invalid creator data")
        actual_authors = [
            _normalized(person.get("lastName") or person.get("name") or "")
            for person in creators if person.get("creatorType") == "author"
        ]
        if (
            existing_year == article.year
            and actual_authors == expected_authors
        ):
            matches.append(item)
    matched = unique(matches)
    if incomplete and potential and matched is None:
        raise ZoteroError(
            "Incomplete metadata and an existing Zotero title could describe different papers. "
            "Resolve the metadata or existing entry before saving; the PDF is preserved"
        )
    return matched


def _collection(api: _API, requested: str | None) -> tuple[str | None, str | None]:
    if not requested:
        return None, None
    collections = _list_response(api.request(
        "GET", f"{api.prefix}/collections", params={"format": "json"}
    ))
    matches = [item for item in collections if _key(item) == requested]
    if not matches:
        matches = [item for item in collections if _data(item).get("name") == requested]
    if len(matches) > 1:
        raise ZoteroError("Multiple Zotero collections have that name; specify a collection key")
    item = matches[0] if matches else api.create(
        f"{api.prefix}/collections", {"name": requested, "parentCollection": False}
    )
    return _key(item), _data(item).get("name", requested)


def _bibliography(article: Article, collection_key: str | None) -> dict:
    # Article primarily represents scholarly journal papers. Keep unsupported types in extra.
    data = {
        "itemType": "journalArticle", "title": article.title,
        "creators": [
            {"creatorType": "author", "firstName": person.given, "lastName": person.family}
            for person in article.authors
        ],
        "tags": [], "collections": [collection_key] if collection_key else [], "relations": {},
    }
    fields = {
        "date": str(article.year) if article.year else None,
        "publicationTitle": article.journal, "volume": article.volume, "issue": article.issue,
        "pages": article.pages, "DOI": normalize_doi(article.doi), "url": article.url,
        "abstractNote": article.abstract,
    }
    data.update({name: value for name, value in fields.items() if value})
    extra = []
    if article.pmid:
        extra.append(f"PMID: {article.pmid}")
    if article.pmcid:
        extra.append(f"PMCID: {article.pmcid}")
    if article.item_type != "article-journal":
        extra.append(f"Type: {article.item_type}")
    if extra:
        data["extra"] = "\n".join(extra)
    return data


def _local_attachment(api: _API, item: dict) -> Path | None:
    response = api.request(
        "GET", f"{api.prefix}/items/{_key(item)}/file/view/url", allow_missing=True
    )
    if response.status_code == 404:
        return None
    parsed = urlparse(response.text.strip())
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    path = Path(unquote(parsed.path))
    return path if path.is_file() else None


def _has_pdf(api: _API, parent_key: str, md5: str) -> bool:
    children = _list_response(api.request(
        "GET", f"{api.prefix}/items/{parent_key}/children", params={"format": "json"}
    ))
    for child in children:
        data = _data(child)
        if data.get("itemType") != "attachment" or data.get("contentType") != "application/pdf":
            continue
        path = _local_attachment(api, child)
        if path and _digest(path) == md5:
            return True
    return False


def _attachment(
    api: _API, parent_key: str, pdf: Path, md5: str, *, reuse_existing_pdf: bool = False,
) -> tuple[dict, bool]:
    children = _list_response(api.request(
        "GET", f"{api.prefix}/items/{parent_key}/children", params={"format": "json"}
    ))
    pending = None
    for child in children:
        data = _data(child)
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
        if local and _digest(local) == md5:
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


def _upload(api: _API, attachment: dict, pdf: Path, md5: str) -> None:
    endpoint = f"{api.prefix}/items/{_key(attachment)}/file"
    previous = _data(attachment).get("md5")
    precondition = {"If-Match": f'"{previous}"'} if previous else {"If-None-Match": "*"}
    response = api.request("POST", endpoint, write=True, headers=precondition, data={
        "md5": md5, "filename": pdf.name, "filesize": str(pdf.stat().st_size),
        "mtime": str(pdf.stat().st_mtime_ns // 1_000_000),
    })
    upload = _object_response(response)
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


def library_info(config: Config) -> dict:
    """Verify the configured library identity without authorization or mutations."""
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
            api = _API(client, config, interactive=False)
            return {"library": api.library_name or "My Library", "group_id": api.group_id}
    except httpx.HTTPError:
        raise ZoteroError(
            "Could not verify the Zotero library; open Zotero and enable its local API"
        ) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ZoteroError("Zotero returned invalid library metadata") from None


def save(
    article: Article, pdf: Path, config: Config, *, collection: str | None = None,
    interactive: bool = True,
) -> dict:
    """Add or reuse a bibliography entry and store the supplied (already compressed) PDF."""
    if not pdf.is_file():
        raise ZoteroError("The PDF to save in Zotero does not exist")
    if pdf.stat().st_size >= 4 * 1024**3:
        raise ZoteroError("Zotero requires attachments smaller than 4 GB")
    md5 = _digest(pdf)
    emit("zotero", "Checking Zotero for an existing paper")
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            api = _API(client, config, interactive)
            parent = _find_parent(api, article, pdf_md5=md5)
            collection_key, collection_name = _collection(
                api, collection if collection is not None else config.zotero_collection
            )
            added = parent is None
            if added:
                emit("zotero", "Adding bibliography entry to Zotero")
                parent = api.create(f"{api.prefix}/items", _bibliography(article, collection_key))
            elif collection_key and collection_key not in _data(parent).get("collections", []):
                current = _data(parent)
                version = current.get("version", parent.get("version"))
                if version is None:
                    raise ZoteroError("Zotero did not provide an item version; saving was stopped")
                api.request(
                    "PATCH", f"{api.prefix}/items/{_key(parent)}", write=True,
                    headers={"If-Unmodified-Since-Version": str(version)},
                    json={"collections": [*current.get("collections", []), collection_key]},
                )
            attachment, present = _attachment(
                api, _key(parent), pdf, md5,
                reuse_existing_pdf=api.group_id is not None and not added,
            )
            if not present:
                _upload(api, attachment, pdf, md5)
            result = {
                "status": "added" if added else ("already-present" if present else "attached"),
                "item_key": _key(parent), "attachment_key": _key(attachment),
                "collection": collection_name, "collection_key": collection_key,
            }
            if api.group_id is not None:
                result.update(group_id=api.group_id, library=api.library_name)
            return result
    except httpx.HTTPError:
        raise ZoteroError(
            "Could not communicate with Zotero. Open Zotero and enable its local API; "
            "the downloaded PDF is preserved"
        ) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ZoteroError(
            "Zotero returned an invalid response; the downloaded PDF is preserved"
        ) from None
