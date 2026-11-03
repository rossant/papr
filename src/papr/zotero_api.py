"""Authorized Zotero desktop transport, response validation, and permission storage.

This is the single home for desktop API errors and authorization. Read-only
bibliographic resolution can also target a hosted API and does not use this
write-capable transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Config
from .progress import emit, pause


class ZoteroError(RuntimeError):
    """An actionable local Zotero error, without authentication details."""


def item_data(item: dict) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("data", item), dict):
        raise ZoteroError("Zotero returned invalid item data")
    return item.get("data", item)


def object_key(item: dict) -> str:
    data = item_data(item)
    key = item.get("key") or data.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key):
        raise ZoteroError("Zotero returned an invalid object key")
    return key


def object_response(response: httpx.Response) -> dict:
    value = response.json()
    if not isinstance(value, dict):
        raise ZoteroError("Zotero returned an invalid response object")
    return value


def list_response(response: httpx.Response) -> list[dict]:
    value = response.json()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ZoteroError("Zotero returned an invalid list response")
    return value


class DesktopAPI:
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
            group = object_response(response)
            data = item_data(group)
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
        permission = object_response(response)
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
        result = object_response(response)
        for field in ("successful", "success", "unchanged", "failed"):
            if not isinstance(result.get(field, {}), dict):
                raise ZoteroError("Zotero returned an invalid save result")
        if result.get("failed"):
            raise ZoteroError("Zotero could not save the item; the downloaded PDF is preserved")
        saved = result.get("successful", {}).get("0")
        if isinstance(saved, dict):
            item_data(saved)
            object_key(saved)
            return saved
        key = result.get("success", {}).get("0") or result.get("unchanged", {}).get("0")
        if key:
            item = object_response(self.request("GET", f"{endpoint}/{key}"))
            item_data(item)
            object_key(item)
            return item
        raise ZoteroError("Zotero did not confirm saving the item")
