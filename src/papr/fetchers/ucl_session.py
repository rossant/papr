"""Persist authenticated browser state, including cookies scoped to a session."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from ..artifacts import atomic_json

VERIFY_URL = "https://www.nature.com/articles/nature12373"


def verified_article(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.hostname == "www-nature-com.libproxy.ucl.ac.uk"
        and parsed.path.rstrip("/") == "/articles/nature12373"
    )


def save_session(context, profile: Path) -> None:
    # Chrome does not reliably restore session cookies after a clean shutdown.
    # Export them explicitly before closing the authenticated context. Atomic
    # JSON records are created with owner-only permissions (0600).
    atomic_json(profile / "session.json", context.storage_state())


def restore_session(context, profile: Path) -> None:
    path = profile / "session.json"
    if not path.exists():
        return
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError
        context.add_cookies(state["cookies"])
        origins = state.get("origins", [])
        if not isinstance(origins, list):
            raise ValueError
        # Restore origin-scoped local storage when that origin is visited.
        context.add_init_script("""(() => {
            const origins = """ + json.dumps(origins) + """;
            for (const entry of origins) {
                if (entry.origin === location.origin) {
                    for (const item of entry.localStorage || [])
                        localStorage.setItem(item.name, item.value);
                }
            }
        })();""")
    except Exception:
        # Browser API errors can contain cookie values. Do not expose them in
        # terminal or batch diagnostics when rejecting an invalid state record.
        raise RuntimeError(
            "Saved UCL session state is unreadable; run papr login ucl to replace it"
        ) from None
