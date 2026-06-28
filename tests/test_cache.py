"""Tests for src/cache.py: hash, memory backend, SQLite backend, llm integration."""

from src.cache import MemoryBackend, SQLiteBackend, _hash

# ---------------------------------------------------------------------------
# Hash stability
# ---------------------------------------------------------------------------

def test_hash_deterministic():
    msgs = [{"role": "user", "content": "hello"}]
    assert _hash(msgs, "model-a") == _hash(msgs, "model-a")


def test_hash_differs_by_model():
    msgs = [{"role": "user", "content": "hello"}]
    assert _hash(msgs, "model-a") != _hash(msgs, "model-b")


def test_hash_differs_by_content():
    m1 = [{"role": "user", "content": "hello"}]
    m2 = [{"role": "user", "content": "world"}]
    assert _hash(m1, "m") != _hash(m2, "m")


# ---------------------------------------------------------------------------
# MemoryBackend
# ---------------------------------------------------------------------------

def test_memory_miss():
    b = MemoryBackend()
    assert b.get("nonexistent") is None


def test_memory_set_get():
    b = MemoryBackend()
    b.set("k", "value")
    assert b.get("k") == "value"


def test_memory_lru_eviction():
    b = MemoryBackend(max_entries=2)
    b.set("a", "1")
    b.set("b", "2")
    b.set("c", "3")  # evicts "a"
    assert b.get("a") is None
    assert b.get("b") == "2"
    assert b.get("c") == "3"
    assert len(b) == 2


def test_memory_clear():
    b = MemoryBackend()
    b.set("x", "y")
    b.clear()
    assert len(b) == 0


# ---------------------------------------------------------------------------
# SQLiteBackend
# ---------------------------------------------------------------------------

def test_sqlite_set_get(tmp_path):
    b = SQLiteBackend(str(tmp_path / "cache.db"))
    b.set("k", "v")
    assert b.get("k") == "v"


def test_sqlite_miss(tmp_path):
    b = SQLiteBackend(str(tmp_path / "cache.db"))
    assert b.get("nope") is None


def test_sqlite_replace(tmp_path):
    b = SQLiteBackend(str(tmp_path / "cache.db"))
    b.set("k", "v1")
    b.set("k", "v2")
    assert b.get("k") == "v2"
    assert len(b) == 1


def test_sqlite_clear(tmp_path):
    b = SQLiteBackend(str(tmp_path / "cache.db"))
    b.set("k", "v")
    b.clear()
    assert len(b) == 0


# ---------------------------------------------------------------------------
# Module-level cache API (with RESPONSE_CACHE=1 monkeypatching)
# ---------------------------------------------------------------------------

def test_cache_get_put_roundtrip(monkeypatch):
    import src.cache as cache_mod
    monkeypatch.setattr(cache_mod, "RESPONSE_CACHE", True)
    monkeypatch.setattr(cache_mod, "_backend", MemoryBackend())

    msgs = [{"role": "user", "content": "test"}]
    assert cache_mod.get(msgs, "model") is None
    cache_mod.put(msgs, "model", "response")
    assert cache_mod.get(msgs, "model") == "response"


def test_cache_disabled_by_default(monkeypatch):
    import src.cache as cache_mod
    monkeypatch.setattr(cache_mod, "RESPONSE_CACHE", False)
    monkeypatch.setattr(cache_mod, "_backend", MemoryBackend())

    msgs = [{"role": "user", "content": "test"}]
    cache_mod.put(msgs, "model", "response")  # should no-op
    assert cache_mod.get(msgs, "model") is None
    assert cache_mod.size() == 0


def test_cache_stats(monkeypatch):
    import src.cache as cache_mod
    monkeypatch.setattr(cache_mod, "RESPONSE_CACHE", True)
    monkeypatch.setattr(cache_mod, "_backend", MemoryBackend())
    s = cache_mod.stats()
    assert "enabled" in s
    assert "entries" in s
