"""Optional image optimizer, isolated from the main process and base dependencies."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_worker(request: dict, timeout: float) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "papr.compression_worker"],
        input=json.dumps(request), text=True, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"image compression worker exited with status {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError("image compression worker returned invalid metadata") from error
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError("image compression worker failed")
    return data


def backend_info(*, timeout: float = 15) -> dict | None:
    """Probe native dependencies in a child, including the tested engine eligibility."""
    try:
        return _run_worker({"operation": "info"}, timeout=timeout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None


def compress_images(source: Path, output: Path, policy: dict, timeout: float) -> dict:
    """Optimize selected image streams; timeout/crashes permit the caller's fallback."""
    if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
        raise ValueError("image compression output must differ from source")
    return _run_worker({
        "operation": "compress", "source": str(source.resolve()),
        "output": str(output.resolve()), "policy": policy,
    }, timeout)
