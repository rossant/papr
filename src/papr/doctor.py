"""Read-only installation and local environment diagnostics."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import Config


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _directory_status(path: Path) -> tuple[str, str]:
    target = path.expanduser()
    candidate = target
    while True:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if candidate == candidate.parent:
                return "error", f"no existing ancestor for {target}"
            candidate = candidate.parent
            continue
        except OSError as exc:
            return "error", f"cannot inspect directory path {candidate} ({type(exc).__name__})"
        if stat.S_ISLNK(info.st_mode) and not candidate.exists():
            return "error", f"{candidate} is a dangling symlink in the directory path"
        if not candidate.is_dir():
            return "error", f"nearest existing ancestor {candidate} is not a directory"
        break
    if not os.access(candidate, os.W_OK | os.X_OK):
        return "error", f"no writable directory at {target}; nearest writable ancestor unavailable"
    if target.is_dir():
        return "healthy", f"{target} exists and appears writable"
    return "healthy", f"{target} can be created under writable ancestor {candidate}"


def _installation() -> dict[str, str]:
    import papr

    location = Path(papr.__file__).resolve()
    try:
        dist_version = importlib.metadata.version("papr")
    except importlib.metadata.PackageNotFoundError:
        dist_version = __version__
    if not location.is_file():
        return _check("installation", "error", f"package module is missing: {location}")
    return _check(
        "installation",
        "healthy",
        f"papr {dist_version} at {location}; "
        f"Python {sys.version_info.major}.{sys.version_info.minor}",
    )


def _tool(name: str, executable: str | None, timeout: float) -> dict[str, str]:
    if executable is None:
        return _check(name, "warning", "not installed; optional compression backend")
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return _check(name, "warning", "version check timed out")
    except OSError as exc:
        return _check(name, "warning", f"could not run version check ({type(exc).__name__})")
    output = (result.stdout or result.stderr).strip().splitlines()
    version = output[0][:200] if output else "version unavailable"
    if result.returncode:
        return _check(name, "warning", f"version command exited {result.returncode}: {version}")
    return _check(name, "healthy", f"{version} ({executable})")


def _pypdf_check(name: str) -> dict[str, str]:
    if importlib.util.find_spec("pypdf") is None:
        return _check(name, "error", "pypdf is not installed")
    return _check(name, "healthy", "pypdf available")


def _sources(config: Config) -> list[dict[str, str]]:
    from .sources import statuses

    checks: list[dict[str, str]] = []
    for source in statuses(config, check=True):
        if source.name in {"crossref", "openalex"}:
            checks.append(
                _check(source.name, "warning", "configured remote source; connectivity not checked")
            )
        elif source.name == "mistral":
            checks.append(
                _check(
                    "mistral OCR",
                    "warning" if not source.available else "healthy",
                    "API key configured; connectivity not checked"
                    if source.available
                    else "not configured; optional OCR service",
                )
            )
        elif source.name == "ucl":
            checks.append(
                _check(
                    "UCL access",
                    "warning" if not source.available else "healthy",
                    "saved browser profile present; login not checked"
                    if source.available
                    else "no saved browser profile; optional access",
                )
            )
        elif source.available and source.state in {"ready", "detected"}:
            checks.append(_check(source.name, "healthy", source.detail))
        elif source.available:
            checks.append(_check(source.name, "warning", f"{source.detail}; status {source.state}"))
        else:
            checks.append(_check(source.name, "warning", source.detail))
    return checks


def diagnose(config: Config, *, strict: bool = False, tool_timeout: float = 2.0) -> dict:
    """Collect diagnostics without creating files or contacting remote services."""
    checks = [_installation()]
    for label, path, required in (
        ("output directory", config.download_dir, True),
        ("config directory", config.config_dir, True),
        ("data directory", config.data_dir, False),
        ("cache directory", config.cache_dir, False),
    ):
        status, detail = _directory_status(path)
        if not required and status == "error":
            status = "warning"
        checks.append(_check(label, status, detail))

    checks.extend(
        [
            _pypdf_check("PDF compression"),
            _tool("Ghostscript", shutil.which("gs"), tool_timeout),
            _tool("qpdf", shutil.which("qpdf"), tool_timeout),
            _pypdf_check("PDF text extraction"),
        ]
    )
    try:
        checks.extend(_sources(config))
    except Exception as exc:
        checks.append(
            _check("local sources", "warning", f"source checks failed ({type(exc).__name__})")
        )
    statuses = {item["status"] for item in checks}
    overall = "error" if "error" in statuses else "warning" if "warning" in statuses else "healthy"
    return {"status": overall, "checks": checks, "strict": strict}
