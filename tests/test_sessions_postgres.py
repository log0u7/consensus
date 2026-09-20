"""Integration tests for the durable PostgresStore backend.

Requires a reachable postgres at PG_DSN (the compose pgvector container
works). Skipped when PG_DSN is unset, so `make test` stays offline.
"""

import os

import pytest
from src.models import ConsensusReport, PipelineResult
from src.sessions import PostgresStore


def _session(code: str) -> dict:
    """A realistic session payload (same shape api.py creates)."""
    return {
        "result": PipelineResult(
            spec="write a hello world",
            code=code,
            consensus=ConsensusReport(panel=[], issues=[], summary="ok"),
        ),
        "system": "You are the Tech Lead.",
        "history": [{"role": "user", "content": "hi"}],
        "members": [],
    }


PG_DSN = os.environ.get("PG_DSN", "")

pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="PG_DSN not set; postgres backend untestable offline"
)


@pytest.fixture
def store():
    s = PostgresStore(PG_DSN, ttl_seconds=3600, max_sessions=100)
    yield s


def test_postgres_store_crud_roundtrip(store):
    value = _session("print(1)")
    sid = store.create(value)
    assert sid
    got = store.get(sid)
    assert got["result"].code == "print(1)"
    assert got["system"] == "You are the Tech Lead."
    assert got["history"] == [{"role": "user", "content": "hi"}]

    store.save(sid, _session("print(2)"))
    assert store.get(sid)["result"].code == "print(2)"

    assert store.delete(sid) is True
    assert store.get(sid) is None
    assert store.delete(sid) is False


def test_postgres_store_get_unknown_returns_none(store):
    assert store.get("00000000-0000-0000-0000-000000000000") is None


def test_postgres_store_len_counts_sessions(store):
    before = len(store)
    sid = store.create({"a": 1})
    assert len(store) == before + 1
    store.delete(sid)
    assert len(store) == before
