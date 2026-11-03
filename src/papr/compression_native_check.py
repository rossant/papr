"""Independent native PDF inspection, always confined to a child process."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def check_native(source: Path, candidate: Path, *, timeout: float = 120) -> bool:
    if importlib.util.find_spec("pymupdf") is None:
        return False
    result = subprocess.run(
        [sys.executable, "-m", "papr.compression_native_check"],
        input=json.dumps([str(source.resolve()), str(candidate.resolve())]),
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise ValueError("Independent PDF inspection rejected the candidate")
    if json.loads(result.stdout) != {"valid": True}:
        raise ValueError("Independent PDF inspection returned invalid evidence")
    return True


def _same(left, right):
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(left - right) <= 0.0001
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _inspect(source: str, candidate: str) -> None:
    import pymupdf

    with pymupdf.open(source) as before, pymupdf.open(candidate) as after:
        if len(before) != len(after):
            raise ValueError("Page count changed")
        for left, right in zip(before, after, strict=True):
            if left.get_text() != right.get_text():
                raise ValueError("Text changed")
            def drawings(page):
                return [(item["type"], tuple(item["rect"])) for item in page.get_drawings()]

            def images(page):
                return [(tuple(item["bbox"]), item["colorspace"])
                        for item in page.get_image_info()]

            if not _same(drawings(left), drawings(right)) or not _same(images(left), images(right)):
                raise ValueError("Graphics changed")


if __name__ == "__main__":
    try:
        source, candidate = json.load(sys.stdin)
        _inspect(source, candidate)
        print(json.dumps({"valid": True}))
    except Exception:
        print(json.dumps({"valid": False}))
        raise SystemExit(1) from None
