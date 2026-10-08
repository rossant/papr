"""CLI presentation for read-only environment diagnostics."""

from __future__ import annotations

import argparse
import json

from .config import Config
from .doctor import diagnose


def run(argv: list[str], config: Config) -> int:
    parser = argparse.ArgumentParser(
        prog="papr doctor", description="Check papr and local services."
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable diagnostics")
    parser.add_argument("--strict", action="store_true", help="return nonzero for warnings too")
    args = parser.parse_args(argv)
    report = diagnose(config, strict=args.strict)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for check in report["checks"]:
            mark = {"healthy": "✓", "warning": "!", "error": "✗"}[check["status"]]
            print(f"{mark} {check['name']}: {check['detail']}")
        print(f"Overall: {report['status']}")
    return int(report["status"] == "error" or (args.strict and report["status"] == "warning"))
