"""Thread-safe rate limiter for external APIs."""
from __future__ import annotations

import threading
import time

_LOCKS: dict[str, threading.Lock] = {}
_LAST_CALL: dict[str, float] = {}
_MUTEX = threading.Lock()


def _get_lock(backend: str) -> threading.Lock:
    with _MUTEX:
        if backend not in _LOCKS:
            _LOCKS[backend] = threading.Lock()
        return _LOCKS[backend]


def throttle(backend: str, min_interval_seconds: float = 3.0) -> None:
    """Enforce minimum interval between calls to the given backend across threads."""
    with _get_lock(backend):
        last = _LAST_CALL.get(backend, 0.0)
        now = time.time()
        elapsed = now - last
        if elapsed < min_interval_seconds:
            time.sleep(min_interval_seconds - elapsed)
        _LAST_CALL[backend] = time.time()
