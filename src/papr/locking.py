"""Bounded, reentrant process locking for cache and export protection records."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from .config import Config

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextmanager
def state_lock(config: Config, *, timeout: float = 5.0):
    """Serialize local state writers and pruning; nesting in one thread is safe.

    The persistent lock file must not be removed between operations: processes
    need to lock the same inode. Read-only commands and previews need no lock.
    """
    import fcntl

    if timeout <= 0:
        raise ValueError("State lock timeout must be positive")
    path = config.data_dir.expanduser().absolute() / ".state.lock"
    key = str(path)
    with _registry_guard:
        local_lock = _locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not local_lock.acquire(timeout=timeout):
        raise TimeoutError("Papr state is busy; retry after the current operation")
    fd = None
    acquired = False
    try:
        active = getattr(_held, "keys", set())
        if key in active:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Papr state is busy; retry after the current operation"
                    ) from None
                time.sleep(0.02)
        _held.keys = active | {key}
        try:
            yield
        finally:
            _held.keys = active
    finally:
        if fd is not None:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        local_lock.release()
