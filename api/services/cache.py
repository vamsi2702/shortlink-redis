import threading
import time
from collections.abc import Callable
from typing import Protocol


class Cache(Protocol):
    """The small interface the API needs. The in-process cache below and the MiniRedis client
    (redis_cache.py) both implement it."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl: float | None = None) -> None: ...

    def set_nx(self, key: str, value: str, ttl: float) -> bool:
        """Set the key only if it does not exist yet. Returns True if it was set."""
        ...

    def delete(self, key: str) -> None: ...

    def incr(self, key: str) -> int: ...

    def expire(self, key: str, ttl: float) -> bool: ...


class InMemoryCache:
    """A dict with expiry times. Thread-safe, because FastAPI runs routes in a thread pool."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_entries: int = 10_000
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        # key -> (value, expires_at or None); dicts keep insertion order, which we use for eviction
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._live(key)

    def set(self, key: str, value: str, ttl: float | None = None) -> None:
        with self._lock:
            if key not in self._data and len(self._data) >= self._max_entries:
                del self._data[next(iter(self._data))]  # evict the oldest entry
            self._data[key] = (value, self._expiry(ttl))

    def set_nx(self, key: str, value: str, ttl: float) -> bool:
        with self._lock:
            if self._live(key) is not None:
                return False
            self._data[key] = (value, self._expiry(ttl))
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def incr(self, key: str) -> int:
        """Add 1 to an integer value (a missing key counts as 0). Keeps any existing expiry."""
        with self._lock:
            current = self._live(key)
            new_value = int(current or 0) + 1  # ValueError if the value is not an integer
            expires_at = self._data[key][1] if key in self._data else None
            self._data[key] = (str(new_value), expires_at)
            return new_value

    def expire(self, key: str, ttl: float) -> bool:
        """Set a time-to-live on an existing key. Returns False if the key does not exist."""
        with self._lock:
            if self._live(key) is None:
                return False
            self._data[key] = (self._data[key][0], self._expiry(ttl))
            return True

    def _expiry(self, ttl: float | None) -> float | None:
        return None if ttl is None else self._clock() + ttl

    def _live(self, key: str) -> str | None:
        """The value if present and not expired. Expired entries are removed here (lazy expiry)."""
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self._clock() >= expires_at:
            del self._data[key]
            return None
        return value


class NullCache:
    """A cache that remembers nothing, for running without one (CACHE_ENABLED=false).

    Every lookup is a miss. The rate limiter's counters live in the cache, so with this class it
    always counts 1 and never refuses a request.
    """

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str, ttl: float | None = None) -> None:
        pass

    def set_nx(self, key: str, value: str, ttl: float) -> bool:
        return True

    def delete(self, key: str) -> None:
        pass

    def incr(self, key: str) -> int:
        return 1

    def expire(self, key: str, ttl: float) -> bool:
        return True


# The one cache shared by the whole running app
default_cache = InMemoryCache()
