import json
import sqlite3
from pathlib import Path

from papr.config import Config
from papr.resolvers import zolit, zolit_sbs, zotero


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
                "creators": [
                    {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"}
                ],
            },
        }
    )
    assert article is not None
    assert article.first_creator == "Smith"
    assert article.year == 2020
    assert article.doi == "10.1000/abc"


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
    zotero_json = json.dumps({"fields": {"publicationTitle": "Pediatrics"}})
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
