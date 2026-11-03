"""Stable fingerprints for lossless and legacy PDF command-line tools."""
from __future__ import annotations

import importlib.metadata
import subprocess
from functools import lru_cache


@lru_cache(maxsize=16)
def tool_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"], text=True, capture_output=True, timeout=3, check=False,
        )
        lines = (result.stdout or result.stderr).strip().splitlines()
        return lines[0][:200] if result.returncode == 0 and lines else "unknown"
    except Exception:
        return "unknown"


def pypdf_version() -> str:
    return importlib.metadata.version("pypdf")
