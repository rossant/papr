from pathlib import Path

from papr.config import Config
from papr.filename import basename, collision_safe_path, render_template
from papr.model import Article, Person


def article():
    return Article(
        title="A title: with / awkward characters?",
        authors=[Person(family="Jenny", given="Carole")],
        year=2006,
        doi="10.1234/test",
    )


def test_zotero_style_template_subset():
    value = render_template(
        article(),
        '{{ firstCreator suffix="_" }}{{ year suffix="_" }}{{ title truncate="7" }}',
    )
    assert value == "Jenny_2006_A title"


def test_basename_sanitizes():
    cfg = Config(filename_template='{{ firstCreator suffix="_" }}{{ year suffix="_" }}{{ title }}')
    name = basename(article(), cfg)
    assert name == "Jenny_2006_A_title_with_awkward_characters"


def test_collision_safe_path(tmp_path: Path):
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"x")
    assert collision_safe_path(p).name == "paper_2.pdf"
    assert collision_safe_path(p, overwrite=True) == p
