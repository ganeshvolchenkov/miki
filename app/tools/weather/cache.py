from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TTLCache:
    """Minimal in-memory cache with a per-instance time-to-live.

    Deliberately not a general-purpose caching library -- just enough to
    stop the Weather panel (or repeated natural-language questions) from
    hitting the API on every redraw/message (spec section 12). A `ttl_seconds
    <= 0` disables caching entirely (every get() is a miss).
    """

    def __init__(self, *, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        if self.ttl_seconds <= 0:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl_seconds <= 0:
            return
        self._store[key] = (self._clock() + self.ttl_seconds, value)

    def clear(self) -> None:
        self._store.clear()
