from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(slots=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """Thread-safe in-memory TTL cache.

    Values are copied on read/write so callers cannot mutate cached state by
    accident. The cache is process-local and is cleared whenever Streamlit is
    restarted.
    """

    def __init__(self, default_ttl_seconds: int = 300) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be greater than zero.")
        self._default_ttl_seconds = default_ttl_seconds
        self._entries: dict[str, _CacheEntry[T]] = {}
        self._lock = RLock()

    def get(self, key: str) -> T | None:
        normalized_key = key.strip()
        if not normalized_key:
            return None

        now = monotonic()
        with self._lock:
            entry = self._entries.get(normalized_key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(normalized_key, None)
                return None
            return deepcopy(entry.value)

    def set(
        self,
        key: str,
        value: T,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("Cache key cannot be empty.")

        ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be greater than zero.")

        with self._lock:
            self._entries[normalized_key] = _CacheEntry(
                value=deepcopy(value),
                expires_at=monotonic() + ttl,
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key.strip(), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def purge_expired(self) -> int:
        now = monotonic()
        with self._lock:
            expired_keys = [
                key
                for key, entry in self._entries.items()
                if entry.expires_at <= now
            ]
            for key in expired_keys:
                self._entries.pop(key, None)
            return len(expired_keys)

    def __len__(self) -> int:
        self.purge_expired()
        with self._lock:
            return len(self._entries)
