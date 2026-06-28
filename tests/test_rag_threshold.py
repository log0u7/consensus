import pytest
from src import rag


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_search_filters_below_threshold(monkeypatch):
    # rows: (source, chunk_idx, content, score)
    rows = [
        ("a.md", 0, "high", 0.9),
        ("b.md", 1, "mid", 0.3),
        ("c.md", 2, "low", 0.05),
    ]

    async def fake_embed(texts):
        return [[0.0]]

    monkeypatch.setattr(rag, "embed", fake_embed)
    monkeypatch.setattr(rag, "_pg_conn", lambda: _FakeConn(rows))

    hits = await rag.search("q", k=5, min_score=0.2)
    sources = [h["source"] for h in hits]
    assert sources == ["a.md", "b.md"]  # c.md dropped (0.05 < 0.2)


@pytest.mark.asyncio
async def test_search_robust_to_db_error(monkeypatch):
    async def fake_embed(texts):
        return [[0.0]]

    def boom():
        raise RuntimeError("no table")

    monkeypatch.setattr(rag, "embed", fake_embed)
    monkeypatch.setattr(rag, "_pg_conn", boom)

    assert await rag.search("q") == []
