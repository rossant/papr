"""Build an isolated source archive to catch accidental private-file inclusion."""

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv build unavailable")
def test_sdist_includes_public_source_only(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    (project / "src" / "papr").mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "uv.lock"):
        shutil.copyfile(root / name, project / name)
    (project / "src" / "papr" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    (project / "private.docx").write_bytes(b"private note")
    (project / "data.local").mkdir()
    (project / "data.local" / "private.pdf").write_bytes(b"private PDF")
    result = subprocess.run(
        ["uv", "build", "--sdist", "--offline", "--out-dir", str(tmp_path / "dist")],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    archive = next((tmp_path / "dist").glob("*.tar.gz"))
    with tarfile.open(archive) as stream:
        names = stream.getnames()
    assert any(name.endswith("/src/papr/__init__.py") for name in names)
    assert any(name.endswith("/pyproject.toml") for name in names)
    assert any(name.endswith("/uv.lock") for name in names)
    assert not any("private" in name or "data.local" in name for name in names)
