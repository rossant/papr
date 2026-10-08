import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from papr.config import Config
from papr.resolvers import zolit, zolit_sbs, zotero
from papr.resolvers.session import resolution_session


def _make_zotero_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table itemTypes (itemTypeID integer primary key, typeName text);
        create table items (itemID integer primary key, key text, itemTypeID integer);
        create table fields (fieldID integer primary key, fieldName text);
        create table itemDataValues (valueID integer primary key, value text);
        create table itemData (itemID integer, fieldID integer, valueID integer);
        create table creatorTypes (creatorTypeID integer primary key, creatorType text);
        create table creators (
            creatorID integer primary key, firstName text, lastName text, fieldMode integer
        );
        create table itemCreators (
            itemID integer, creatorID integer, creatorTypeID integer, orderIndex integer
        );
        create table itemAttachments (
            itemID integer, parentItemID integer, path text, contentType text
        );
        """
    )
    conn.execute("insert into itemTypes values (1, 'journalArticle')")
    conn.execute("insert into itemTypes values (2, 'attachment')")
    conn.execute("insert into items values (1, 'PARENT', 1)")
    conn.execute("insert into items values (2, 'ATTACH', 2)")
    fields = ["title", "date", "DOI", "publicationTitle"]
    values = ["A Jenny paper", "2006", "10.1000/JENNY", "Pediatrics"]
    for i, (field, value) in enumerate(zip(fields, values, strict=True), start=1):
        conn.execute("insert into fields values (?, ?)", (i, field))
        conn.execute("insert into itemDataValues values (?, ?)", (i, value))
        conn.execute("insert into itemData values (1, ?, ?)", (i, i))
    conn.execute("insert into creatorTypes values (1, 'author')")
    conn.execute("insert into creators values (1, 'Carole', 'Jenny', 0)")
    conn.execute("insert into itemCreators values (1, 1, 1, 0)")
    conn.execute(
        "insert into itemAttachments values (2, 1, 'storage:paper.pdf', 'application/pdf')"
    )
    conn.commit()
    conn.close()


def test_zotero_api_item_mapping():
    article = zotero.from_api_item(
        {
            "key": "ABC",
            "data": {
                "itemType": "journalArticle",
                "title": "Example",
                "date": "2020-04-01",
                "DOI": "10.1000/ABC",
                "publicationTitle": "Journal",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Smith"}],
            },
        }
    )
    assert article is not None
    assert article.first_creator == "Smith"
    assert article.year == 2020
    assert article.doi == "10.1000/abc"


@pytest.mark.parametrize(
    "source_type,expected",
    [
        ("conferencePaper", "paper-conference"),
        ("book", "book"),
        ("bookSection", "chapter"),
        ("preprint", "article"),
        ("journalArticle", "article-journal"),
        ("unexpected", "document"),
        (None, "document"),
    ],
)
def test_zotero_preserves_publication_type(source_type, expected):
    article = zotero.from_api_item({"itemType": source_type, "title": "Example", "key": "ABC"})
    assert article.item_type == expected
    assert article.source_item_type == source_type
    assert article.source_key == "ABC"


@pytest.mark.parametrize("deleted_id", [1, 2])
def test_zotero_sqlite_excludes_deleted_articles_and_pdfs(tmp_path, deleted_id):
    path = tmp_path / "zotero.sqlite"
    _make_zotero_db(path)
    storage = tmp_path / "storage" / "ATTACH"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    with sqlite3.connect(path) as conn:
        conn.execute("create table deletedItems (itemID integer primary key)")
        conn.execute("insert into deletedItems values (?)", (deleted_id,))
    cfg = Config(zotero_api_url="", zotero_data_dir=tmp_path)
    found = zotero.by_doi("10.1000/jenny", cfg)
    matches = zotero.search_sqlite("Jenny 2006", cfg)
    if deleted_id == 1:
        assert found is None and matches == []
    else:
        assert found is not None and len(matches) == 1
        assert found.local_pdf is None and matches[0].local_pdf is None


def test_zotero_api_excludes_deleted_item():
    assert zotero.from_api_item({"title": "Trashed", "deleted": 1}) is None


def test_zotero_sqlite_author_year_and_attachment(tmp_path: Path):
    data_dir = tmp_path / "Zotero"
    storage = data_dir / "storage" / "ATTACH"
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    _make_zotero_db(data_dir / "zotero.sqlite")

    cfg = Config(zotero_api_url="", zotero_data_dir=data_dir)
    result = zotero.search("Jenny 2006", cfg)

    assert len(result) == 1
    assert result[0].doi == "10.1000/jenny"
    assert result[0].score is not None and result[0].score > 0.9
    assert result[0].local_pdf == str(pdf)
    assert zotero.by_doi("10.1000/JENNY", cfg).local_pdf == str(pdf)


def test_zolit_database_search(tmp_path: Path):
    db = tmp_path / "zolit.sqlite"
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table items (
            item_key text primary key, title text, year text, creators_json text,
            doi text, zotero_json text
        );
        create table attachments (
            attachment_key text primary key, item_key text, path text,
            content_type text, exists_on_disk integer
        );
        """
    )
    creators = json.dumps(
        [{"creator_type": "author", "first_name": "Carole", "last_name": "Jenny"}]
    )
    zotero_json = json.dumps(
        {"item_type": "conferencePaper", "fields": {"publicationTitle": "Pediatrics"}}
    )
    conn.execute(
        "insert into items values (?, ?, ?, ?, ?, ?)",
        ("ABC", "A Jenny paper", "2006", creators, "10.1000/jenny", zotero_json),
    )
    conn.execute(
        "insert into attachments values (?, ?, ?, ?, ?)",
        ("PDF", "ABC", str(pdf), "application/pdf", 1),
    )
    conn.commit()
    conn.close()

    cfg = Config(zolit_db=db)
    result = zolit.search("Jenny 2006", cfg)

    assert len(result) == 1
    assert result[0].source == "zolit"
    assert result[0].item_type == "paper-conference"
    assert result[0].source_key == "ABC"
    assert result[0].local_pdf == str(pdf)
    assert zolit.by_doi("10.1000/JENNY", cfg).local_pdf == str(pdf)


def test_zolit_sbs_seed_parser(tmp_path: Path):
    repo = tmp_path / "zolit-sbs"
    domain = repo / "domains" / "sbs"
    domain.mkdir(parents=True)
    (domain / "key-publications.md").write_text(
        "# Seeds\n\n"
        "- Echenne, Couture, Sebire, \x60Le syndrome du bebe secoue\x60, 2020.\n"
        "- Guthkelch, 1971.\n",
        encoding="utf-8",
    )

    result = zolit_sbs.search("Echenne 2020", Config(zolit_sbs_repo=repo))

    assert len(result) == 1
    assert result[0].title == "Le syndrome du bebe secoue"
    assert result[0].source == "zolit-sbs"


def test_zolit_sbs_structured_references_override_legacy_seeds(tmp_path):
    domain = tmp_path / "domains" / "sbs"
    domain.mkdir(parents=True)
    (domain / "key-publications.md").write_text("- Jenny, `Old title`, 2006.\n")
    (domain / "references.csl.json").write_text(
        json.dumps(
            [
                {
                    "id": "seed",
                    "title": "Jenny paper",
                    "type": "paper-conference",
                    "DOI": "10.1000/jenny",
                    "issued": {"date-parts": [[2006]]},
                    "author": [{"family": "Jenny"}],
                    "papr-status": "candidate",
                }
            ]
        )
    )
    result = zolit_sbs.search("Jenny 2006", Config(zolit_sbs_repo=tmp_path))
    assert [article.title for article in result] == ["Jenny paper"]
    assert result[0].item_type == "paper-conference"
    assert result[0].doi == "10.1000/jenny"
    assert result[0].source_key == "seed"


def test_invalid_structured_seeds_do_not_silently_use_markdown(tmp_path):
    (tmp_path / "key-publications.md").write_text("- Jenny, `Old title`, 2006.\n")
    (tmp_path / "references.csl.json").write_text("{}")
    with pytest.raises(ValueError, match="list of CSL"):
        zolit_sbs.load_references(tmp_path)


def test_zotero_batch_reuses_snapshot_and_bulk_attachment_query(tmp_path, monkeypatch):
    data_dir = tmp_path / "Zotero"
    data_dir.mkdir()
    _make_zotero_db(data_dir / "zotero.sqlite")
    original = zotero._snapshot
    snapshots = []
    queries = []

    def snapshot(path):
        snapshots.append(path)
        conn = original(path)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(zotero, "_snapshot", snapshot)
    config = Config(zotero_api_url="", zotero_data_dir=data_dir)
    with resolution_session():
        first = zotero.search_sqlite("Jenny 2006", config)
        first[0].pmid = "123456"
        found = zotero.by_doi("10.1000/jenny", config)
        assert found.pmid is None
        assert len(snapshots) == 1
        assert sum("from itemAttachments" in query for query in queries) == 1
    with resolution_session():
        zotero.search_sqlite("Jenny 2006", config)
    assert len(snapshots) == 2


def test_zotero_locked_snapshot_stops_retrying(tmp_path, monkeypatch):
    path = tmp_path / "zotero.sqlite"
    _make_zotero_db(path)
    writer = sqlite3.connect(path)
    writer.execute("begin exclusive")
    monkeypatch.setattr(zotero, "SNAPSHOT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(zotero, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.01)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="snapshot timed out"):
            zotero._snapshot(path)
        assert time.monotonic() - started < 1.0
    finally:
        writer.rollback()
        writer.close()
    # A failed snapshot must release its connection and allow subsequent reads.
    conn = zotero._snapshot(path)
    assert conn.execute("select count(*) from items").fetchone()[0] == 2
    conn.close()


def test_zotero_api_only_loads_attachments_for_ranked_candidates(monkeypatch):
    requested = []
    items = [
        {"key": str(index), "data": {"title": "Jenny paper", "date": "2006"}} for index in range(20)
    ]

    def get(self, url, **kwargs):
        requested.append(url)
        if url.endswith("/top"):
            return httpx.Response(200, json=items, request=httpx.Request("GET", url))
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    results = zotero.search_api("Jenny paper", Config(max_candidates=2))
    assert len(results) == 2
    assert len(requested) == 3
    assert all("/children" in url for url in requested[1:])


def test_zotero_batch_does_not_retry_unavailable_snapshot(tmp_path, monkeypatch):
    (tmp_path / "zotero.sqlite").touch()
    attempts = []

    def unavailable(path):
        attempts.append(path)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(zotero, "_snapshot", unavailable)
    config = Config(zotero_api_url="", zotero_data_dir=tmp_path)
    with resolution_session():
        assert zotero.search_sqlite("Jenny 2006", config) == []
        assert zotero.by_doi("10.1000/jenny", config) is None
    assert len(attempts) == 1
    with resolution_session():
        assert zotero.search_sqlite("Jenny 2006", config) == []
    assert len(attempts) == 2


def test_zotero_api_attachment_budget_preserves_metadata(monkeypatch):
    clock = [0.0]
    requested = []
    monkeypatch.setattr(zotero.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(zotero, "API_BUDGET_SECONDS", 0.5)

    def get(self, url, **kwargs):
        requested.append(url)
        assert kwargs["timeout"] <= 0.5
        if url.endswith("/top"):
            return httpx.Response(
                200,
                json=[
                    {"key": str(index), "data": {"title": "Jenny paper", "date": "2006"}}
                    for index in range(5)
                ],
                request=httpx.Request("GET", url),
            )
        clock[0] = 0.6
        raise httpx.ReadTimeout("Desktop API stalled")

    monkeypatch.setattr(httpx.Client, "get", get)
    results = zotero.search_api("Jenny paper", Config())
    assert len(results) == 5
    assert all(article.local_pdf is None for article in results)
    assert len(requested) == 2


def _add_zotero_groups(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            alter table items add column libraryID integer default 1;
            create table groups (groupID integer primary key, libraryID integer);
            insert into groups values (5593385, 8);
            insert into groups values (12345, 9);
            """
        )
        for item_id, attachment_id, library_id, title in [
            (3, 4, 8, "SBS Jenny paper"),
            (5, 6, 9, "Other Jenny paper"),
        ]:
            conn.execute(
                "insert into items values (?, ?, 1, ?)",
                (item_id, f"PARENT{library_id}", library_id),
            )
            conn.execute(
                "insert into items values (?, ?, 2, ?)",
                (attachment_id, f"ATTACH{library_id}", library_id),
            )
            conn.execute("insert into itemDataValues values (?, ?)", (item_id + 10, title))
            conn.execute("insert into itemData values (?, 1, ?)", (item_id, item_id + 10))
            for field_id in (2, 3, 4):
                conn.execute("insert into itemData values (?, ?, ?)", (item_id, field_id, field_id))
            conn.execute("insert into itemCreators values (?, 1, 1, 0)", (item_id,))
            conn.execute(
                "insert into itemAttachments values (?, ?, 'storage:paper.pdf', 'application/pdf')",
                (attachment_id, item_id),
            )


def test_zotero_group_api_scopes_metadata_and_attachment_requests(tmp_path, monkeypatch):
    pdf = tmp_path / "group-paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    requested = []

    def get(self, url, **kwargs):
        requested.append(url)
        if url.endswith("/top"):
            data = [{"key": "PARENT", "data": {"title": "Jenny paper", "date": "2006"}}]
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))
        if url.endswith("/children"):
            data = [{"key": "PDF", "data": {"itemType": "attachment", "filename": "paper.pdf"}}]
            return httpx.Response(200, json=data, request=httpx.Request("GET", url))
        return httpx.Response(200, text=pdf.as_uri(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    config = Config()
    config.zotero_group_id = 5593385
    results = zotero.search_api("Jenny paper", config)
    assert len(results) == 1 and results[0].local_pdf == str(pdf)
    base = f"{config.zotero_api_url}/groups/5593385/items"
    assert requested == [f"{base}/top", f"{base}/PARENT/children", f"{base}/PDF/file/view/url"]


def test_zotero_group_sqlite_is_scoped_and_read_only(tmp_path):
    path = tmp_path / "zotero.sqlite"
    _make_zotero_db(path)
    _add_zotero_groups(path)
    for key in ("ATTACH", "ATTACH8", "ATTACH9"):
        storage = tmp_path / "storage" / key
        storage.mkdir(parents=True)
        (storage / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    original = path.read_bytes()
    config = Config(zotero_api_url="", zotero_data_dir=tmp_path)
    config.zotero_group_id = 5593385
    result = zotero.search_sqlite("Jenny 2006", config)
    assert [article.title for article in result] == ["SBS Jenny paper"]
    assert result[0].local_pdf == str(tmp_path / "storage" / "ATTACH8" / "paper.pdf")
    assert result[0].source_key == "PARENT8"
    assert result[0].source_library == "library:8"
    assert zotero.by_doi("10.1000/JENNY", config).title == "SBS Jenny paper"
    assert path.read_bytes() == original


@pytest.mark.parametrize("groups_exist", [False, True])
def test_missing_zotero_group_never_reads_other_libraries(tmp_path, groups_exist):
    path = tmp_path / "zotero.sqlite"
    _make_zotero_db(path)
    if groups_exist:
        _add_zotero_groups(path)
    config = Config(zotero_api_url="", zotero_data_dir=tmp_path)
    config.zotero_group_id = 999999
    assert zotero.search_sqlite("Jenny 2006", config) == []
    assert zotero.by_doi("10.1000/jenny", config) is None


def test_zotero_session_caches_each_group_separately(tmp_path):
    path = tmp_path / "zotero.sqlite"
    _make_zotero_db(path)
    _add_zotero_groups(path)
    config = Config(zotero_api_url="", zotero_data_dir=tmp_path)
    with resolution_session():
        config.zotero_group_id = 5593385
        assert zotero.search_sqlite("Jenny 2006", config)[0].title == "SBS Jenny paper"
        config.zotero_group_id = 12345
        assert zotero.search_sqlite("Jenny 2006", config)[0].title == "Other Jenny paper"
        config.zotero_group_id = None
        assert len(zotero.search_sqlite("Jenny 2006", config)) == 3


def test_zotero_group_without_library_mapping_does_not_read_all_records(tmp_path):
    path = tmp_path / "zotero.sqlite"
    _make_zotero_db(path)
    _add_zotero_groups(path)
    with sqlite3.connect(path) as conn:
        conn.execute("insert into groups values (999999, null)")
    config = Config(zotero_api_url="", zotero_data_dir=tmp_path)
    config.zotero_group_id = 999999
    assert zotero.search_sqlite("Jenny 2006", config) == []
