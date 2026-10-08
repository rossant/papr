"""Read actual Zolit-written indexes; fixtures contain no research source data."""

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("zolit.db", reason="install sibling Zolit to run the interoperability checks")

from zolit.db import connect, init_db
from zolit.source_status import check_sync_status, source_fingerprint
from zolit.zotero_local import upsert_attachments, upsert_items

from papr.config import Config
from papr.formats.csl import encode
from papr.resolvers import zolit as reader
from papr.sources import _zolit_status

pytestmark = pytest.mark.integration


@pytest.fixture
def index(tmp_path):
    db = tmp_path / "index.sqlite"
    init_db(db)
    pdf = tmp_path / "fixture.pdf"
    pdf.write_bytes(b"%PDF-1.7\nsynthetic fixture")
    works = []
    for key, kind, group in (
        ("JOURNAL", "journalArticle", 42),
        ("MEETING", "conferencePaper", 42),
        ("BOOK", "book", 99),
    ):
        works.append(
            {
                "item_key": key,
                "short_ref": "Example 2024",
                "title": f"Synthetic {key}",
                "year": 2024,
                "creators": [{"last_name": "Example", "first_name": "Ada"}],
                "doi": f"10.1000/{key.lower()}",
                "tags": [],
                "zotero": {
                    "item_type": kind,
                    "library_id": group,
                    "group_id": group,
                    "fields": {"publicationTitle": "Fixture venue"},
                },
            }
        )
    with connect(db) as conn:
        upsert_items(conn, works)
        upsert_attachments(
            conn,
            [
                {
                    "attachment_key": "PDF",
                    "item_key": "JOURNAL",
                    "path": str(pdf),
                    "content_type": "application/pdf",
                    "link_mode": 1,
                    "exists_on_disk": True,
                    "zotero": {},
                }
            ],
        )
    return db, pdf


def test_zolit_writer_to_papr_preserves_scope_types_and_attachment(index):
    db, pdf = index
    config = Config(zolit_db=db, zotero_group_id=42)
    articles = reader._all_articles(db, group_id=config.zotero_group_id)
    assert {a.source_key for a in articles} == {"JOURNAL", "MEETING"}
    assert all(a.source_library == "group:42" for a in articles)
    assert {encode(a)["type"] for a in articles} == {"article-journal", "paper-conference"}
    journal = next(a for a in articles if a.source_key == "JOURNAL")
    assert journal.local_pdf == str(pdf)
    assert journal.authors[0].family == "Example"
    assert journal.doi == "10.1000/journal"


def test_sync_fingerprint_contract_agrees_with_zolit(index, tmp_path):
    db, _ = index
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"synthetic source fingerprint")
    with connect(db) as conn:
        conn.execute(
            "insert into source_sync values ('zotero', ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                str(source),
                json.dumps(source_fingerprint(source)),
                json.dumps(
                    {
                        "items": 3,
                        "attachments": 1,
                        "annotations": 0,
                        "collections": 0,
                        "collection_items": 0,
                    }
                ),
            ),
        )
    config = Config(zolit_db=db)
    assert _zolit_status(config, check=True).state == check_sync_status(db)["state"] == "ready"
    source.write_bytes(b"changed synthetic source")
    assert _zolit_status(config, check=True).state == check_sync_status(db)["state"] == "changed"


def test_index_without_sync_record_has_unknown_freshness(index):
    db, _ = index
    assert _zolit_status(Config(zolit_db=db), check=True).state == "unknown"
    assert check_sync_status(db)["state"] == "unknown"
