"""The keyspace: one dictionary from key to value (the heart of MiniRedis).

Keys are `bytes`, because Redis keys are binary safe. Values are `bytes` (strings), a
`deque` (lists) or a `dict` (hashes).

Expiry: a second dictionary, `_expires`, maps a key to the moment it dies, as a unix time in
milliseconds. Every access first checks whether that moment has passed and, if so, removes the key
first ("lazy" expiry), so an expired key is never visible.
"""

import heapq
import sys
import time
from collections import deque
from collections.abc import Callable


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _value_size(value: object) -> int:
    """Bytes Python uses for one stored value, including what is inside lists and hashes."""
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in value.items())
    elif isinstance(value, deque):
        size += sum(sys.getsizeof(item) for item in value)
    return size


class Store:
    def __init__(self, clock: Callable[[], int] = now_ms) -> None:
        self._clock = clock  # milliseconds since 1970; tests pass a fake one
        self._data: dict[bytes, object] = {}
        self._expires: dict[bytes, int] = {}  # key -> unix time (ms) when it dies
        # (dies-at, key) pairs with the earliest on top, so a background task can find the next
        # key to kill without scanning. An entry goes stale when its key is deleted or given a
        # different expiry; `_expires` is the truth, and stale entries are skipped when popped.
        self._heap: list[tuple[int, bytes]] = []
        self.expired_keys = 0  # how many keys have died of old age

    def now(self) -> int:
        return self._clock()

    # ---- reading and writing ---------------------------------------------------------------

    def get(self, key: bytes) -> object | None:
        """The value of a key, or None if it does not exist or has expired."""
        if self._expire_if_due(key):
            return None
        return self._data.get(key)

    def set(
        self,
        key: bytes,
        value: object,
        *,
        expire_at: int | None = None,
        keep_ttl: bool = False,
    ) -> None:
        """Store a value. Without `expire_at` the key lives forever, unless `keep_ttl` keeps the
        expiry it already had (INCR does that; a plain SET does not)."""
        self._expire_if_due(key)  # an old, already-expired value must not lend its expiry
        self._data[key] = value
        if expire_at is not None:
            self._schedule(key, expire_at)
        elif not keep_ttl:
            self._expires.pop(key, None)

    def delete(self, key: bytes) -> bool:
        """Remove a key. Returns True if it existed (and had not already expired)."""
        if self._expire_if_due(key):
            return False
        self._expires.pop(key, None)
        return self._data.pop(key, None) is not None

    def exists(self, key: bytes) -> bool:
        return self.get(key) is not None

    # ---- expiry ----------------------------------------------------------------------------

    def expire_at(self, key: bytes) -> int | None:
        """When the key dies (unix ms), or None if it has no expiry or does not exist."""
        if self._expire_if_due(key):
            return None
        return self._expires.get(key)

    def set_expiry(self, key: bytes, at_ms: int) -> bool:
        """Make an existing key die at `at_ms`. Returns False if the key does not exist."""
        if not self.exists(key):
            return False
        self._schedule(key, at_ms)
        return True

    def persist(self, key: bytes) -> bool:
        """Remove a key's expiry. Returns True if it had one."""
        if not self.exists(key):
            return False
        return self._expires.pop(key, None) is not None

    def ttl_ms(self, key: bytes) -> int:
        """Milliseconds left to live; -2 if the key does not exist, -1 if it never expires."""
        if not self.exists(key):
            return -2
        at = self._expires.get(key)
        return -1 if at is None else max(0, at - self._clock())

    def _schedule(self, key: bytes, at_ms: int) -> None:
        self._expires[key] = at_ms
        heapq.heappush(self._heap, (at_ms, key))
        # a key whose expiry is changed again and again leaves stale entries behind; when they
        # outnumber the real ones, rebuild the heap from the truth
        if len(self._heap) > 2 * len(self._expires) + 1024:
            self._heap = [(at, k) for k, at in self._expires.items()]
            heapq.heapify(self._heap)

    def purge_expired(self, limit: int = 1000) -> int:
        """Remove keys whose time has come (the background cleaner). Looks at no more than
        `limit` heap entries, so one call never holds up the server for long. Returns how many
        keys it removed."""
        removed = 0
        now = self._clock()
        for _ in range(limit):
            if not self._heap or self._heap[0][0] > now:
                break
            at, key = heapq.heappop(self._heap)
            if self._expires.get(key) == at:  # otherwise the entry is stale: skip it
                self._kill(key)
                removed += 1
        return removed

    def has_due_expiry(self) -> bool:
        """True if the heap's earliest entry is already due (more purging is waiting)."""
        return bool(self._heap) and self._heap[0][0] <= self._clock()

    def _expire_if_due(self, key: bytes) -> bool:
        """Remove the key if its time has come. Returns True if it did."""
        at = self._expires.get(key)
        if at is None or at > self._clock():
            return False
        self._kill(key)
        return True

    def _kill(self, key: bytes) -> None:
        """Remove a key whose time has come."""
        del self._expires[key]
        self._data.pop(key, None)
        self.expired_keys += 1

    # ---- sizes -----------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._data)

    def expiring_count(self) -> int:
        return len(self._expires)

    def average_ttl_ms(self) -> int:
        """The average time left, in ms, of the keys that have an expiry (0 if there are none)."""
        now = self._clock()
        remaining = [at - now for at in self._expires.values() if at > now]
        return sum(remaining) // len(remaining) if remaining else 0

    def memory_estimate(self) -> int:
        """A rough count of the bytes Python uses for the data. It looks at every key, so it
        takes time in proportion to the number of keys: fine for INFO, not for every command."""
        total = sys.getsizeof(self._data) + sys.getsizeof(self._expires)
        total += sys.getsizeof(self._heap)
        for key, value in self._data.items():
            total += sys.getsizeof(key) + _value_size(value)
        return total
