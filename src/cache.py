"""Response cache: avoid redundant LLM calls for identical inputs.

Two complementary strategies:

1. Prompt prefix stability (passive, no code needed here):
   Always place stable content first in the messages list:
     [system, skills, tool-definitions, ..., task (volatile)]
   This maximises the provider's prefix cache hit rate (Anthropic, some
   OpenAI-compatible providers return cached_tokens in usage).
   The context builder (context.py, phase 5) enforces this order.

2. Local response cache (active, implemented here):
   Hash the full messages list -> store/retrieve the response string.
   Two backends:
     memory  - dict in process, lost on restart (default, no deps)
     sqlite  - persisted to disk across restarts (CACHE_BACKEND=sqlite)

   Opt-in: disabled unless RESPONSE_CACHE=1.
   Useful for:
     - repeated identical runs during development
     - idempotent panel calls on the same code
     - integration tests without hitting a real provider

Environment variables:
  RESPONSE_CACHE=1          enable the cache (default: off)
  CACHE_BACKEND=memory|sqlite
  CACHE_DB_PATH=cache.db    sqlite file path
  CACHE_MAX_ENTRIES=1000    LRU cap for the memory backend
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from collections import OrderedDict

log = logging.getLogger(__name__)

RESPONSE_CACHE = os.environ.get("RESPONSE_CACHE", "0").strip() in ("1", "true", "yes")
CACHE_BACKEND  = os.environ.get("CACHE_BACKEND", "memory").lower()
CACHE_DB_PATH  = os.environ.get("CACHE_DB_PATH", "cache.db")
CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "1000"))


def _hash(messages: list[dict], model: str) -> str:
    """Deterministic SHA-256 key for a (model, messages) pair."""
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class CacheBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def __len__(self) -> int: ...

    @abstractmethod
    def clear(self) -> None: ...


# ---------------------------------------------------------------------------
# Memory backend (LRU cap via OrderedDict)
# ---------------------------------------------------------------------------

class MemoryBackend(CacheBackend):
    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()


# ---------------------------------------------------------------------------
# SQLite backend (persisted, no extra deps - stdlib sqlite3)
# ---------------------------------------------------------------------------

class SQLiteBackend(CacheBackend):
    def __init__(self, path: str = CACHE_DB_PATH) -> None:
        import sqlite3
        self._path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, value TEXT, ts REAL DEFAULT (unixepoch()))"
        )
        self._db.commit()

    def get(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value FROM cache WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", (key, value)
        )
        self._db.commit()

    def __len__(self) -> int:
        (n,) = self._db.execute("SELECT count(*) FROM cache").fetchone()
        return int(n)

    def clear(self) -> None:
        self._db.execute("DELETE FROM cache")
        self._db.commit()


# ---------------------------------------------------------------------------
# Module-level cache instance
# ---------------------------------------------------------------------------

def _make_backend() -> CacheBackend:
    if CACHE_BACKEND == "sqlite":
        return SQLiteBackend()
    return MemoryBackend()


_backend: CacheBackend = _make_backend()


def get(messages: list[dict], model: str) -> str | None:
    """Return a cached response or None.  No-op when RESPONSE_CACHE is off."""
    if not RESPONSE_CACHE:
        return None
    key = _hash(messages, model)
    hit = _backend.get(key)
    if hit is not None:
        log.debug("cache hit for model=%s key=%s...", model, key[:8])
    return hit


def put(messages: list[dict], model: str, response: str) -> None:
    """Store a response.  No-op when RESPONSE_CACHE is off."""
    if not RESPONSE_CACHE:
        return
    _backend.set(_hash(messages, model), response)


def size() -> int:
    return len(_backend)


def clear() -> None:
    _backend.clear()


def stats() -> dict:
    return {"backend": CACHE_BACKEND, "enabled": RESPONSE_CACHE, "entries": size()}
