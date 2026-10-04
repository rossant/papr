import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest

from papr import zotero
from papr.config import Config
from papr.model import Article, Person


class LocalZotero:
    def __init__(self, directory):
        self.directory = directory
        self.requests = []
        self.items = {}
        self.collections = {}
        self.authorizations = 0
        self.remember = True
        self.permission = True
        self.enabled = True
        self.file_payload = None
        self.destination = "http://127.0.0.1:23119/api/local/uploads/UPLOAD123"
        self.auth_valid = False
        self.version = 1
        self.group_id = None
        self.group_available = True

    def item(self, key, data):
        item = {"key": key, "version": self.version, "data": {"key": key,
                "version": self.version, **data}}
        self.items[key] = item
        return item

    def respond(self, request, status=200, *, value=None, text=None):
        kwargs = {"text": text} if text is not None else {"json": value}
        return httpx.Response(status, request=request,
                              headers={"Zotero-Server-ID": "SERVER"}, **kwargs)

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api")
        if not self.enabled:
            return self.respond(request, 403)
        if path == "/" and request.method == "GET":
            return self.respond(request, value={})
        if self.group_id is not None:
            if path == f"/groups/{self.group_id}":
                if not self.group_available:
                    return self.respond(request, 404)
                return self.respond(request, value={"id": self.group_id, "data": {"name": "SBS"}})
            if path.startswith("/users/"):
                raise AssertionError("Group saving must never access the personal library")
            path = path.replace(f"/groups/{self.group_id}/", "/users/0/", 1)
        if path == "/local/authorize":
            self.authorizations += 1
            assert request.headers["Zotero-Server-ID"] == "SERVER"
            assert json.loads(request.content) == {"appName": "papr"}
            if not self.permission:
                return self.respond(request, 403, value={"denied": True})
            self.auth_valid = True
            return self.respond(request, value={"key": "SECRET", "remember": self.remember})
        if request.method != "GET" and not path.startswith("/local/uploads/"):
            assert request.headers["Zotero-Server-ID"] == "SERVER"
            assert request.headers.get("Zotero-API-Key") == "SECRET"
            if not self.auth_valid:
                return self.respond(request, 401)
            if not self.remember:
                self.auth_valid = False
        if path == "/users/0/items/top":
            query = request.url.params["q"].casefold()
            return self.respond(request, value=[item for item in self.items.values()
                                               if item["data"]["itemType"] != "attachment"
                                               and query in json.dumps(item["data"]).casefold()])
        if path == "/users/0/collections":
            if request.method == "GET":
                return self.respond(request, value=list(self.collections.values()))
            data = json.loads(request.content)[0]
            assert "Zotero-Write-Token" in request.headers
            saved = {"key": "COLL1234", "data": {"key": "COLL1234", **data}}
            self.collections["COLL1234"] = saved
            return self.respond(request, value={"successful": {"0": saved}, "failed": {}})
        if path == "/users/0/items" and request.method == "POST":
            data = json.loads(request.content)[0]
            assert "Zotero-Write-Token" in request.headers
            key = "ATTACH12" if data["itemType"] == "attachment" else "PARENT12"
            saved = self.item(key, data)
            return self.respond(request, value={"successful": {"0": saved}, "failed": {}})
        if path.endswith("/children"):
            parent = path.split("/")[-2]
            return self.respond(request, value=[item for item in self.items.values()
                                               if item["data"].get("parentItem") == parent])
        if path.endswith("/file/view/url"):
            attachment = self.items[path.split("/")[-4]]
            local = attachment["data"].get("local")
            if not local:
                return self.respond(request, 404)
            return self.respond(request, text=Path(local).as_uri())
        if path.endswith("/file") and request.method == "POST":
            body = dict(httpx.QueryParams(request.content.decode()))
            assert request.headers.get("If-None-Match") == "*"
            if "upload" in body:
                assert body["upload"] == "UPLOAD123"
                local = self.directory / "stored.pdf"
                local.write_bytes(self.file_payload)
                self.items["ATTACH12"]["data"]["local"] = str(local)
                self.items["ATTACH12"]["data"]["md5"] = hashlib.md5(self.file_payload).hexdigest()
                return self.respond(request, 204)
            expected_md5 = hashlib.md5((self.directory / "compressed.pdf").read_bytes()).hexdigest()
            assert body["md5"] == expected_md5
            assert body["filename"] == "compressed.pdf"
            return self.respond(request, value={"url": self.destination, "uploadKey": "UPLOAD123",
                                               "contentType": "application/pdf", "prefix": "",
                                               "suffix": ""})
        if path.startswith("/local/uploads/"):
            self.file_payload = request.content
            assert "Zotero-API-Key" not in request.headers
            return self.respond(request, 201)
        if request.method == "PATCH":
            key = path.split("/")[-1]
            assert request.headers["If-Unmodified-Since-Version"] == str(self.items[key]["version"])
            self.items[key]["data"].update(json.loads(request.content))
            return self.respond(request, 204)
        raise AssertionError(f"Unexpected request: {request.method} {path}")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "config_dir", property(lambda self: tmp_path / "config"))
    server = LocalZotero(tmp_path)
    original = httpx.Client
    monkeypatch.setattr(zotero.httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(server.handle)
    ))
    pdf = tmp_path / "compressed.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncompressed PDF contents")
    article = Article("A paper", [Person("Smith", "Jane")], year=2020,
                      doi="10.1000/paper", journal="Journal", pmid="123456")
    return server, pdf, article, Config(zotero_collection="SBS")


def test_add_bibliography_collection_and_stored_pdf(environment):
    server, pdf, article, config = environment
    result = zotero.save(article, pdf, config)
    assert result == {"status": "added", "item_key": "PARENT12", "attachment_key": "ATTACH12",
                      "collection": "SBS", "collection_key": "COLL1234"}
    parent = server.items["PARENT12"]["data"]
    assert parent["DOI"] == "10.1000/paper"
    assert parent["publicationTitle"] == "Journal"
    assert parent["extra"] == "PMID: 123456"
    assert parent["collections"] == ["COLL1234"]
    assert parent["creators"][0]["lastName"] == "Smith"
    assert server.file_payload == pdf.read_bytes()
    key_file = next((config.config_dir / "zotero").glob("*.json"))
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert json.loads(key_file.read_text())["server_id"] == "SERVER"
    assert server.authorizations == 1


def test_repeated_save_reuses_parent_attachment_and_remembered_permission(environment):
    server, pdf, article, config = environment
    zotero.save(article, pdf, config)
    server.requests.clear()
    result = zotero.save(article, pdf, config, interactive=False)
    assert result["status"] == "already-present"
    assert len(server.items) == 2
    assert all(request.method == "GET" for request in server.requests)
    assert server.authorizations == 1


def test_existing_parent_preserves_other_collections(environment):
    server, pdf, article, config = environment
    server.item("PARENT12", {"itemType": "journalArticle", "DOI": article.doi,
                              "title": article.title, "collections": ["EXIST123"]})
    result = zotero.save(article, pdf, config)
    assert result["status"] == "attached"
    assert server.items["PARENT12"]["data"]["collections"] == ["EXIST123", "COLL1234"]


def test_noninteractive_save_does_not_open_permission_dialog(environment):
    server, pdf, article, config = environment
    with pytest.raises(zotero.ZoteroError, match="Run interactively"):
        zotero.save(article, pdf, config, interactive=False)
    assert server.authorizations == 0
    assert not server.items and not server.collections


def test_one_use_keys_authorize_again_without_saving_credentials(environment):
    server, pdf, article, config = environment
    server.remember = False
    assert zotero.save(article, pdf, config)["status"] == "added"
    assert server.authorizations == 5  # collection, parent, attachment, upload, registration
    assert not (config.config_dir / "zotero").exists()


def test_denied_authorization_leaves_library_unchanged(environment):
    server, pdf, article, config = environment
    server.permission = False
    with pytest.raises(zotero.ZoteroError, match="access denied"):
        zotero.save(article, pdf, config)
    assert not server.items and not server.collections
    assert pdf.is_file()


def test_disabled_api_has_actionable_error(environment):
    server, pdf, article, config = environment
    server.enabled = False
    with pytest.raises(zotero.ZoteroError, match="Settings.*Advanced"):
        zotero.save(article, pdf, config)
    assert len(server.requests) == 1


def test_ambiguous_doi_does_not_write(environment):
    server, pdf, article, config = environment
    for key in ("PARENT12", "PARENT34"):
        server.item(key, {"itemType": "journalArticle", "DOI": article.doi})
    with pytest.raises(zotero.ZoteroError, match="Multiple Zotero items"):
        zotero.save(article, pdf, config)
    assert server.authorizations == 0
    assert not server.collections


def test_no_doi_matches_exact_title_year_and_authors(environment):
    server, pdf, article, config = environment
    article.doi = None
    server.item("PARENT12", {"itemType": "journalArticle", "title": "A Paper!",
                              "date": "2020-03-04", "collections": [], "creators": [
                                  {"creatorType": "author", "lastName": "Smith"}]})
    assert zotero.save(article, pdf, config)["status"] == "attached"
    assert len(server.items) == 2


def test_same_title_with_other_year_is_not_reused(environment):
    server, pdf, article, config = environment
    article.doi = None
    server.item("OTHER123", {"itemType": "journalArticle", "title": article.title,
                              "date": "2019", "creators": [
                                  {"creatorType": "author", "lastName": "Smith"}]})
    assert zotero.save(article, pdf, config)["status"] == "added"
    assert len(server.items) == 3


def test_duplicate_collection_names_fail_before_authorization(environment):
    server, pdf, article, config = environment
    server.collections = {key: {"key": key, "data": {"name": "SBS"}}
                          for key in ("COLL1234", "COLL5678")}
    with pytest.raises(zotero.ZoteroError, match="collection key"):
        zotero.save(article, pdf, config)
    assert server.authorizations == 0


def test_upload_refuses_external_destination_and_retries_pending_attachment(environment):
    server, pdf, article, config = environment
    server.destination = "https://evil.example/api/local/uploads/UPLOAD123"
    with pytest.raises(zotero.ZoteroError, match="unexpected upload destination"):
        zotero.save(article, pdf, config)
    assert server.file_payload is None
    assert len(server.items) == 2
    server.destination = "http://127.0.0.1:23119/api/local/uploads/UPLOAD123"
    assert zotero.save(article, pdf, config)["status"] == "attached"
    assert len(server.items) == 2


def test_expired_remembered_key_reauthorizes_for_next_write(environment):
    server, pdf, article, config = environment
    zotero.save(article, pdf, config)
    server.auth_valid = False
    server.items["ATTACH12"]["data"].pop("local")
    # The checksum describes the remote metadata but its file is missing locally.
    server.items["ATTACH12"]["data"].pop("md5")
    assert zotero.save(article, pdf, config)["status"] == "attached"
    assert server.authorizations == 2
    assert len(server.items) == 2


def test_expired_permission_in_noninteractive_mode_never_opens_dialog(environment):
    server, pdf, article, config = environment
    zotero.save(article, pdf, config)
    server.auth_valid = False
    server.items["ATTACH12"]["data"].pop("local")
    server.items["ATTACH12"]["data"].pop("md5")
    with pytest.raises(zotero.ZoteroError, match="Run interactively"):
        zotero.save(article, pdf, config, interactive=False)
    assert server.authorizations == 1
    assert not list((config.config_dir / "zotero").glob("*.json"))


def test_explicit_collection_key_disambiguates_duplicate_names(environment):
    server, pdf, article, config = environment
    server.collections = {key: {"key": key, "data": {"name": "SBS"}}
                          for key in ("COLL1234", "COLL5678")}
    result = zotero.save(article, pdf, config, collection="COLL5678")
    assert result["collection_key"] == "COLL5678"
    assert server.items["PARENT12"]["data"]["collections"] == ["COLL5678"]


def test_unsafe_local_upload_path_is_rejected(environment):
    server, pdf, article, config = environment
    server.destination = "http://127.0.0.1:23119/api/local/uploads/%2e%2e/authorize"
    with pytest.raises(zotero.ZoteroError, match="unexpected upload destination"):
        zotero.save(article, pdf, config)
    assert server.file_payload is None


def test_network_error_does_not_include_credentials(environment, monkeypatch):
    server, pdf, article, config = environment

    def fail(self, *args, **kwargs):
        raise httpx.ConnectError("SECRET")

    monkeypatch.setattr(server, "handle", fail)
    with pytest.raises(zotero.ZoteroError, match="Open Zotero") as error:
        zotero.save(article, pdf, config)
    assert "SECRET" not in str(error.value)
    assert pdf.is_file()


def test_doi_paper_reuses_matching_parent_without_doi_via_title_query(environment):
    server, pdf, article, config = environment
    server.item("PARENT12", {"itemType": "journalArticle", "title": article.title,
                              "date": "2020", "collections": [], "creators": [
                                  {"creatorType": "author", "lastName": "Smith"}]})
    assert zotero.save(article, pdf, config)["status"] == "attached"
    queries = [request.url.params["q"] for request in server.requests
               if request.url.path.endswith("/items/top")]
    assert queries == [article.doi, article.title]
    assert len(server.items) == 2


def test_title_fallback_does_not_reuse_parent_with_conflicting_doi(environment):
    server, pdf, article, config = environment
    server.item("OTHER123", {"itemType": "journalArticle", "title": article.title,
                              "DOI": "10.1000/other", "date": "2020", "creators": [
                                  {"creatorType": "author", "lastName": "Smith"}]})
    assert zotero.save(article, pdf, config)["status"] == "added"
    assert len(server.items) == 3


@pytest.mark.parametrize("missing", ["year", "authors"])
def test_title_only_or_incomplete_metadata_never_reuses_weak_parent(environment, missing):
    server, pdf, article, config = environment
    article.doi = None
    if missing == "year":
        article.year = None
    else:
        article.authors = []
    server.item("OTHER123", {"itemType": "journalArticle", "title": article.title,
                              "date": "2020" if article.year else "", "creators": [
                                  {"creatorType": "author", "lastName": person.family}
                                  for person in article.authors]})
    with pytest.raises(zotero.ZoteroError, match="Incomplete metadata"):
        zotero.save(article, pdf, config)
    assert len(server.items) == 1
    assert server.authorizations == 0


def test_duplicate_exact_metadata_fallback_fails_before_writes(environment):
    server, pdf, article, config = environment
    for key in ("PARENT12", "OTHER123"):
        server.item(key, {"itemType": "journalArticle", "title": article.title,
                          "date": "2020", "creators": [
                              {"creatorType": "author", "lastName": "Smith"}]})
    with pytest.raises(zotero.ZoteroError, match="Multiple Zotero items"):
        zotero.save(article, pdf, config)
    assert server.authorizations == 0


@pytest.mark.parametrize(("method", "path", "value"), [
    ("GET", "/users/0/items/top", {}),
    ("GET", "/users/0/collections", {}),
    ("POST", "/local/authorize", []),
    ("POST", "/users/0/items", []),
    ("POST", "/users/0/collections", {"successful": []}),
    ("GET", "/users/0/items/PARENT12/children", {}),
    ("POST", "/users/0/items/ATTACH12/file", []),
])
def test_malformed_response_shapes_raise_actionable_errors(environment, monkeypatch,
                                                           method, path, value):
    server, pdf, article, config = environment
    original = server.handle

    def malformed(request):
        if request.method == method and request.url.path == "/api" + path:
            return server.respond(request, value=value)
        return original(request)

    monkeypatch.setattr(server, "handle", malformed)
    with pytest.raises(zotero.ZoteroError, match="invalid"):
        zotero.save(article, pdf, config)
    assert pdf.is_file()


def test_native_permission_instructions_remain_visible_while_display_pauses(environment, capsys):
    _, pdf, article, config = environment
    zotero.save(article, pdf, config)
    assert "Zotero permission dialog" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["year", "authors", "both"])
def test_sparse_metadata_repeat_reuses_parent_by_actual_pdf_hash(environment, missing):
    server, pdf, article, config = environment
    article.doi = None
    if missing in {"year", "both"}:
        article.year = None
    if missing in {"authors", "both"}:
        article.authors = []
    assert zotero.save(article, pdf, config)["status"] == "added"
    server.requests.clear()
    assert zotero.save(article, pdf, config, interactive=False)["status"] == "already-present"
    assert len(server.items) == 2
    assert all(request.method == "GET" for request in server.requests)


def test_sparse_metadata_same_title_different_pdf_fails_without_new_writes(environment):
    server, pdf, article, config = environment
    article.doi = None
    article.year = None
    article.authors = []
    zotero.save(article, pdf, config)
    pdf.write_bytes(b"%PDF-1.7\na different paper under the same filename")
    server.requests.clear()
    with pytest.raises(zotero.ZoteroError, match="Incomplete metadata.*existing Zotero title"):
        zotero.save(article, pdf, config)
    assert len(server.items) == 2
    assert all(request.method == "GET" for request in server.requests)


def test_sparse_metadata_known_year_conflict_not_reused_even_if_pdf_identical(environment):
    server, pdf, article, config = environment
    article.doi = None
    article.authors = []
    zotero.save(article, pdf, config)
    server.items["PARENT12"]["data"]["date"] = "2019"
    # The old parent is excluded because known years conflict; a new parent is allowed.
    original = server.items.pop("PARENT12")
    server.items["OTHER123"] = {**original, "key": "OTHER123",
                               "data": {**original["data"], "key": "OTHER123"}}
    server.items["ATTACH12"]["data"]["parentItem"] = "OTHER123"
    assert zotero.save(article, pdf, config)["status"] == "added"


def test_group_existing_duhaime_pdf_uses_only_group_reads_and_no_writes(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    config.zotero_collection = None
    server.group_id = config.zotero_group_id
    article.title = "The shaken baby syndrome"
    article.authors = [Person("Duhaime", "Ann-Christine")]
    article.year = 1987
    server.item("7J33HWRW", {"itemType": "journalArticle", "title": article.title,
                              "DOI": article.doi, "date": "1987", "collections": []})
    server.item("ATTACH12", {"itemType": "attachment", "parentItem": "7J33HWRW",
                              "contentType": "application/pdf", "filename": pdf.name,
                              "linkMode": "imported_file", "local": str(pdf),
                              "md5": zotero._digest(pdf)})
    result = zotero.save(article, pdf, config, interactive=False)
    assert result["status"] == "already-present"
    assert result["item_key"] == "7J33HWRW"
    assert result["group_id"] == 5593385
    assert result["library"] == "SBS"
    assert server.authorizations == 0
    assert all(request.method == "GET" for request in server.requests)
    paths = [request.url.path for request in server.requests]
    assert "/api/groups/5593385/items/top" in paths
    assert "/api/groups/5593385/items/7J33HWRW/children" in paths
    assert "/api/groups/5593385/items/ATTACH12/file/view/url" in paths
    assert not any("/users/" in path for path in paths)


def test_group_missing_fails_before_authorization_without_personal_fallback(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    server.group_id = config.zotero_group_id
    server.group_available = False
    with pytest.raises(zotero.ZoteroError, match="group 5593385.*not available locally"):
        zotero.save(article, pdf, config)
    assert server.authorizations == 0
    assert not server.items and not server.collections
    assert [request.url.path for request in server.requests] == ["/api/", "/api/groups/5593385"]


def test_group_new_entry_collection_and_upload_never_touch_personal_routes(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    server.group_id = config.zotero_group_id
    assert zotero.save(article, pdf, config)["status"] == "added"
    result = zotero.save(article, pdf, config, interactive=False)
    assert result["status"] == "already-present"
    paths = [request.url.path for request in server.requests]
    assert "/api/groups/5593385/collections" in paths
    assert "/api/groups/5593385/items" in paths
    assert "/api/groups/5593385/items/ATTACH12/file" in paths
    assert "/api/local/authorize" in paths
    assert "/api/local/uploads/UPLOAD123" in paths
    assert not any("/users/" in path for path in paths)


def test_group_existing_parent_membership_patch_stays_in_group(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    server.group_id = config.zotero_group_id
    server.item("PARENT12", {"itemType": "journalArticle", "DOI": article.doi,
                              "title": article.title, "collections": ["EXIST123"]})
    assert zotero.save(article, pdf, config)["status"] == "attached"
    patches = [request.url.path for request in server.requests if request.method == "PATCH"]
    assert patches == ["/api/groups/5593385/items/PARENT12"]
    assert server.items["PARENT12"]["data"]["collections"] == ["EXIST123", "COLL1234"]


def test_personal_library_matching_doi_cannot_influence_group_lookup(environment, monkeypatch):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    config.zotero_collection = None
    server.group_id = config.zotero_group_id
    personal_item = {"key": "PERSON12", "data": {"DOI": article.doi, "itemType": "journalArticle"}}
    original = server.handle

    def routed(request):
        if request.url.path == "/api/users/0/items/top":
            return server.respond(request, value=[personal_item])
        return original(request)

    monkeypatch.setattr(server, "handle", routed)
    result = zotero.save(article, pdf, config)
    assert result["status"] == "added"
    assert result["item_key"] != "PERSON12"
    assert not any("/users/" in request.url.path for request in server.requests)


def test_group_forbidden_metadata_stops_without_authorization(environment, monkeypatch):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    server.group_id = config.zotero_group_id
    original = server.handle

    def forbidden(request):
        if request.url.path == "/api/groups/5593385":
            server.requests.append(request)
            return server.respond(request, 403)
        return original(request)

    monkeypatch.setattr(server, "handle", forbidden)
    with pytest.raises(zotero.ZoteroError, match="group 5593385.*inaccessible"):
        zotero.save(article, pdf, config)
    assert server.authorizations == 0
    assert len(server.requests) == 2


def test_library_info_verifies_group_name_without_auth_or_library_mutation(environment):
    server, _, _, config = environment
    config.zotero_group_id = 5593385
    server.group_id = config.zotero_group_id
    assert zotero.library_info(config) == {"library": "SBS", "group_id": 5593385}
    assert server.authorizations == 0
    assert [request.url.path for request in server.requests] == ["/api/", "/api/groups/5593385"]
    assert all(request.method == "GET" for request in server.requests)
    assert not config.config_dir.exists()
    assert not server.items and not server.collections


def test_library_info_personal_route_does_not_request_group_metadata(environment):
    server, _, _, config = environment
    assert zotero.library_info(config) == {"library": "My Library", "group_id": None}
    assert [request.url.path for request in server.requests] == ["/api/"]
    assert server.authorizations == 0


def test_group_existing_original_pdf_remains_authoritative_despite_compressed_hash(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    config.zotero_collection = None
    server.group_id = config.zotero_group_id
    original = pdf.parent / "original-uncompressed.pdf"
    original.write_bytes(b"%PDF-1.7\noriginal uncompressed PDF with different bytes")
    server.item("7J33HWRW", {"itemType": "journalArticle", "title": article.title,
                              "DOI": article.doi, "date": "2020", "collections": []})
    server.item("EXISTPDF", {"itemType": "attachment", "parentItem": "7J33HWRW",
                              "contentType": "application/pdf", "filename": original.name,
                              "linkMode": "imported_file", "local": str(original),
                              "md5": zotero._digest(original)})
    assert zotero._digest(original) != zotero._digest(pdf)
    result = zotero.save(article, pdf, config, interactive=False)
    assert result["status"] == "already-present"
    assert result["attachment_key"] == "EXISTPDF"
    assert server.authorizations == 0
    assert all(request.method == "GET" for request in server.requests)
    assert len(server.items) == 2
    assert original.read_bytes() == b"%PDF-1.7\noriginal uncompressed PDF with different bytes"


def test_group_membership_change_reuses_authoritative_pdf_without_upload(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    config.zotero_collection = None
    server.group_id = config.zotero_group_id
    original = pdf.parent / "original.pdf"
    original.write_bytes(b"%PDF-1.7\noriginal PDF content")
    server.item("PARENT12", {"itemType": "journalArticle", "title": article.title,
                              "DOI": article.doi, "collections": ["EXIST123"]})
    server.item("EXISTPDF", {"itemType": "attachment", "parentItem": "PARENT12",
                              "contentType": "application/pdf", "linkMode": "imported_file",
                              "local": str(original), "filename": original.name})
    server.collections["COLL1234"] = {"key": "COLL1234", "data": {"name": "Relevant"}}
    result = zotero.save(article, pdf, config, collection="Relevant")
    assert result["status"] == "already-present"
    assert result["attachment_key"] == "EXISTPDF"
    writes = [request for request in server.requests if request.method != "GET"]
    assert [request.url.path for request in writes] == [
        "/api/local/authorize", "/api/groups/5593385/items/PARENT12"
    ]
    assert server.items["PARENT12"]["data"]["collections"] == ["EXIST123", "COLL1234"]
    assert len(server.items) == 2


def test_group_sparse_metadata_still_requires_hashproof_before_authoritative_pdf_reuse(environment):
    server, pdf, article, config = environment
    config.zotero_group_id = 5593385
    config.zotero_collection = None
    server.group_id = config.zotero_group_id
    article.doi = None
    article.year = None
    article.authors = []
    original = pdf.parent / "other.pdf"
    original.write_bytes(b"%PDF-1.7\na different paper")
    server.item("PARENT12", {"itemType": "journalArticle", "title": article.title,
                              "collections": []})
    server.item("EXISTPDF", {"itemType": "attachment", "parentItem": "PARENT12",
                              "contentType": "application/pdf", "linkMode": "imported_file",
                              "local": str(original), "filename": original.name})
    with pytest.raises(zotero.ZoteroError, match="Incomplete metadata"):
        zotero.save(article, pdf, config)
    assert server.authorizations == 0
    assert all(request.method == "GET" for request in server.requests)
    assert len(server.items) == 2
