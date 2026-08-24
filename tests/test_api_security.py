"""Security regression tests for the API surface.

Covers:
  - ArchiveRequest.root constraint (Content-Disposition header injection)
  - generic client-facing error payloads (no exception text leakage)
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from src.api import app, with_heartbeat

client = TestClient(app)


def _archive_payload(root: str) -> dict:
    return {
        "files": [{"path": "hello.py", "language": "python", "content": "print(1)"}],
        "format": "zip",
        "root": root,
    }


# ---------------------------------------------------------------------------
# Archive root: header-injection guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_root",
    [
        "../evil",
        "a/b",
        'x" onload="y',
        "line1\r\nX-Injected: yes",
        "",
    ],
)
def test_archive_rejects_unsafe_root(bad_root):
    r = client.post("/api/archive", json=_archive_payload(bad_root))
    assert r.status_code == 422, bad_root


def test_archive_accepts_safe_root_and_sets_filename():
    r = client.post("/api/archive", json=_archive_payload("my_project-1.0"))
    assert r.status_code == 200
    assert 'filename="my_project-1.0.zip"' in r.headers["content-disposition"]
    assert "\r" not in r.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Error payloads must not carry exception text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_error_event_is_generic():
    """A failing inner generator yields only the exception type name."""

    async def failing():
        yield 'data: {"delta": "partial"}\n\n'
        raise RuntimeError("connect to http://internal-host:9999 key abc")

    blob = "".join([e async for e in with_heartbeat(failing())])

    err_payload = None
    for line in blob.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            if isinstance(payload, dict) and "error" in payload:
                err_payload = payload["error"]

    assert err_payload == "RuntimeError"
    assert "internal-host" not in blob
    assert "abc" not in blob


def test_health_probe_error_is_type_only(monkeypatch):
    from src import llm

    class _Boom:
        async def get(self, path):
            raise RuntimeError("leaky http://secret-host/x?token=zzz")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: _Boom())
    out = asyncio.run(llm.provider_reachable("zen"))
    assert out["reachable"] is False
    assert out["error"] == "RuntimeError"
