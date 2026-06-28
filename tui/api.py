"""Async httpx client for the consensus API."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx


class APIClient:
    """Thin async client for every public endpoint.

    SSE endpoints expose an async generator that yields parsed JSON dicts.
    """

    def __init__(self, base_url: str = "http://localhost:8800", timeout: float = 300) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout))

    async def close(self) -> None:
        await self._client.aclose()

    # -- health / quota ----------------------------------------------------------

    async def health(self, check_provider: str | None = None) -> dict[str, Any]:
        params = {"check_provider": check_provider} if check_provider else {}
        resp = await self._client.get("/api/health", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_quota(self) -> dict[str, Any]:
        resp = await self._client.get("/api/quota")
        resp.raise_for_status()
        return resp.json()

    async def set_quota(self, low_quota: bool) -> dict[str, Any]:
        resp = await self._client.post("/api/quota", json={"low_quota": low_quota})
        resp.raise_for_status()
        return resp.json()

    # -- run ----------------------------------------------------------------------

    async def run_stream(self, spec: str, use_rag: bool = False) -> AsyncGenerator[dict[str, Any], None]:
        async with self._client.stream(
            "POST",
            "/api/run/stream",
            json={"spec": spec, "use_rag": use_rag},
        ) as resp:
            resp.raise_for_status()
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith("data: "):
                            yield json.loads(line[6:])
                        elif line.startswith(":") or not line:
                            continue
                        else:
                            yield json.loads(line)

    # -- chat ---------------------------------------------------------------------

    async def chat_stream(
        self, session_id: str, message: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with self._client.stream(
            "POST",
            "/api/chat/stream",
            json={"session_id": session_id, "message": message},
        ) as resp:
            resp.raise_for_status()
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith("data: "):
                            yield json.loads(line[6:])
                        elif line.startswith(":") or not line:
                            continue
                        else:
                            yield json.loads(line)

    async def chat(self, session_id: str, message: str) -> dict[str, Any]:
        resp = await self._client.post("/api/chat", json={"session_id": session_id, "message": message})
        resp.raise_for_status()
        return resp.json()

    async def regen_artifacts(self, session_id: str) -> dict[str, Any]:
        resp = await self._client.post("/api/chat/regen-artifacts", json={"session_id": session_id})
        resp.raise_for_status()
        return resp.json()

    # -- archive ------------------------------------------------------------------

    async def archive_formats(self) -> list[str]:
        resp = await self._client.get("/api/archive/formats")
        resp.raise_for_status()
        return resp.json()["formats"]

    async def download_archive(
        self,
        files: list[dict[str, str]],
        fmt: str = "zip",
        root: str = "project",
    ) -> bytes:
        resp = await self._client.post(
            "/api/archive",
            json={"files": files, "format": fmt, "root": root},
        )
        resp.raise_for_status()
        return resp.content

    # -- session ------------------------------------------------------------------

    async def delete_session(self, session_id: str) -> None:
        resp = await self._client.delete(f"/api/session/{session_id}")
        resp.raise_for_status()
