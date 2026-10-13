"""The keyspace: one dictionary from key to value (the heart of MiniRedis).

Keys and values are `bytes`, because Redis keys and values are binary safe. In Phase 3 values can
also be lists and hashes, and keys can expire.
"""


class Store:
    def __init__(self) -> None:
        self._data: dict[bytes, bytes] = {}

    def get(self, key: bytes) -> bytes | None:
        return self._data.get(key)

    def set(self, key: bytes, value: bytes) -> None:
        self._data[key] = value

    def delete(self, key: bytes) -> bool:
        """Remove a key. Returns True if it existed."""
        return self._data.pop(key, None) is not None

    def exists(self, key: bytes) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)
