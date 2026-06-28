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


@pytest.mark.asyncio
async def test_search_min_score_zero_keeps_all(monkeypatch):
    """min_score=0 must return every row regardless of their score."""
    rows = [
        ("a.md", 0, "alpha", 0.99),
        ("b.md", 1, "beta",  0.01),
        ("c.md", 2, "gamma", 0.0),
    ]

    async def fake_embed(texts):
        return [[0.0]]

    monkeypatch.setattr(rag, "embed", fake_embed)
    monkeypatch.setattr(rag, "_pg_conn", lambda: _FakeConn(rows))

    hits = await rag.search("q", k=10, min_score=0.0)
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_search_returns_score_in_hits(monkeypatch):
    """Each hit dict must carry a 'score' float field."""
    rows = [("x.md", 0, "content", 0.75)]

    async def fake_embed(texts):
        return [[0.0]]

    monkeypatch.setattr(rag, "embed", fake_embed)
    monkeypatch.setattr(rag, "_pg_conn", lambda: _FakeConn(rows))

    hits = await rag.search("q", k=5, min_score=0.0)
    assert len(hits) == 1
    assert isinstance(hits[0]["score"], float)
    assert hits[0]["score"] == pytest.approx(0.75)
