"""Integration tests for the sqlite-vec RAG backend (offline, no server).

Proves the documented alternative backend actually works end to end:
index_directory + search roundtrip against a real sqlite-vec virtual table.
embed() is monkeypatched so no embedding provider is needed.
"""

import pytest
from src import config, rag

_DIM = 4


async def _fake_embed(texts):
    """Deterministic one-hot embedding: dim 0 = 'alpha', dim 1 = 'beta'."""
    out = []
    for t in texts:
        v = [0.0] * _DIM
        v[0] = 1.0 if "alpha" in t else 0.0
        v[1] = 1.0 if "beta" in t else 0.0
        out.append(v)
    return out


@pytest.fixture
def sqlite_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAG_BACKEND", "sqlite")
    monkeypatch.setattr(config, "SQLITE_VEC_PATH", str(tmp_path / "rag-test.db"))
    monkeypatch.setattr(config, "EMBED_DIM", _DIM)
    monkeypatch.setattr(rag, "embed", _fake_embed)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "alpha.md").write_text("alpha rules everything\n" * 40)
    (docs / "beta.txt").write_text("beta versus alpha\n" * 40)
    return docs


@pytest.mark.asyncio
async def test_sqlite_index_then_search_roundtrip(sqlite_backend):
    await rag.index_directory(str(sqlite_backend))
    hits = await rag.search("alpha", k=5, min_score=0.0)
    assert hits, "sqlite-vec backend returned no hits after indexing"
    best = hits[0]
    assert best["source"].endswith("alpha.md")
    assert isinstance(best["score"], float)
    assert all(h["score"] >= 0.0 for h in hits)


@pytest.mark.asyncio
async def test_sqlite_search_empty_db_returns_empty(tmp_path, monkeypatch):
    # Different, never-indexed db path: search must not raise (robustness).
    monkeypatch.setattr(config, "RAG_BACKEND", "sqlite")
    monkeypatch.setattr(config, "SQLITE_VEC_PATH", str(tmp_path / "never-indexed.db"))
    monkeypatch.setattr(rag, "embed", _fake_embed)
    assert await rag.search("alpha") == []
