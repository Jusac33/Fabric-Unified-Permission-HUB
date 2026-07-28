"""Tiny in-memory TTL cache for slow REST aggregations.

Used to make page loads feel instant after the first hit. Caller passes an
explicit cache key. Use `get(key)` -> Optional[value], `set(key, value)`,
or `cached(key, factory, ttl)` which re-uses a fresh value if available.
"""
from __future__ import annotations
import time
import threading
from typing import Any, Callable, Optional, Tuple

_LOCK = threading.RLock()
_STORE: dict[str, Tuple[float, Any]] = {}


def get(key: str) -> Optional[Any]:
    with _LOCK:
        rec = _STORE.get(key)
        if not rec:
            return None
        expires_at, value = rec
        if expires_at < time.time():
            _STORE.pop(key, None)
            return None
        return value


def set(key: str, value: Any, ttl: float = 60.0) -> None:  # noqa: A001
    with _LOCK:
        _STORE[key] = (time.time() + ttl, value)


def invalidate(prefix: str = "") -> int:
    """Drop everything (or every key starting with `prefix`). Returns count."""
    with _LOCK:
        if not prefix:
            n = len(_STORE)
            _STORE.clear()
            return n
        keys = [k for k in _STORE if k.startswith(prefix)]
        for k in keys:
            _STORE.pop(k, None)
        return len(keys)


def cached(key: str, factory: Callable[[], Any], ttl: float = 60.0) -> Any:
    """Return cached value or compute via factory and store it."""
    v = get(key)
    if v is not None:
        return v
    v = factory()
    set(key, v, ttl)
    return v
