"""Session store with a TTL and an LRU cap, in two interchangeable backends.

A session holds the pipeline result, the Lead system prompt, and the running
chat history. Two guards keep a long-running process bounded:

  - TTL: a session untouched for SESSION_TTL_SECONDS is dropped.
  - LRU cap: at most SESSION_MAX sessions are kept; the least recently used is
    evicted first.

Backends (SESSION_BACKEND):
  - "memory" (default): in-process dict, lost on restart.
  - "postgres": rows in the pgvector database, durable across restarts and
    shareable between workers.

API contract: callers that mutate a returned session dict must call
`store.save(sid, sess)` afterwards. For the memory backend this is a no-op
(the dict is the live object); for postgres it writes the row back.
"""

import json
import logging
import time
import uuid
from collections import OrderedDict

from . import config
from .models import PipelineResult

log = logging.getLogger(__name__)


# --- serialization helpers -------------------------------------------------
# A session value is {"result": PipelineResult, "system": str, "history": list}.
# For storage we serialize result to its JSON dump.

def _dump(value: dict) -> dict:
    result = value.get("result")
    return {
        "result": result.model_dump() if isinstance(result, PipelineResult) else result,
        "system": value.get("system", ""),
        "history": value.get("history", []),
    }


def _load(row: dict) -> dict:
    result = row.get("result")
    return {
        "result": PipelineResult.model_validate(result) if isinstance(result, dict) else result,
        "system": row.get("system", ""),
        "history": row.get("history", []),
    }


class MemoryStore:
    def __init__(self, ttl_seconds: int, max_sessions: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._data: OrderedDict[str, dict] = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def _purge_expired(self) -> None:
        cutoff = self._now() - self._ttl
        expired = [sid for sid, s in self._data.items() if s["last_access"] < cutoff]
        for sid in expired:
            del self._data[sid]
        if expired:
            log.info("purged %d expired session(s)", len(expired))

    def _evict_overflow(self) -> None:
        while len(self._data) > self._max:
            sid, _ = self._data.popitem(last=False)  # least recently used
            log.info("evicted LRU session %s (cap %d)", sid, self._max)

    def create(self, value: dict) -> str:
        self._purge_expired()
        sid = str(uuid.uuid4())
        value["last_access"] = self._now()
        self._data[sid] = value
        self._evict_overflow()
        return sid

    def get(self, sid: str) -> dict | None:
        self._purge_expired()
        sess = self._data.get(sid)
        if sess is None:
            return None
        sess["last_access"] = self._now()
        self._data.move_to_end(sid)  # mark as most recently used
        return sess

    def save(self, sid: str, value: dict) -> None:
        # The returned dict is the live object; nothing to write back.
        if sid in self._data:
            self._data[sid]["last_access"] = self._now()

    def delete(self, sid: str) -> bool:
        return self._data.pop(sid, None) is not None

    def __len__(self) -> int:
        return len(self._data)


class PostgresStore:
    """Durable backend. TTL via last_access, LRU cap enforced on insert."""

    def __init__(self, dsn: str, ttl_seconds: int, max_sessions: int) -> None:
        self._dsn = dsn
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._init_schema()

    def _conn(self):
        import psycopg

        return psycopg.connect(self._dsn)

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    last_access TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            conn.commit()

    def _purge_expired(self, cur) -> None:
        cur.execute(
            "DELETE FROM sessions WHERE last_access < now() - make_interval(secs => %s)",
            (self._ttl,),
        )

    def _evict_overflow(self, cur) -> None:
        cur.execute(
            """
            DELETE FROM sessions WHERE id IN (
                SELECT id FROM sessions ORDER BY last_access DESC OFFSET %s
            )
            """,
            (self._max,),
        )

    def create(self, value: dict) -> str:
        sid = str(uuid.uuid4())
        payload = json.dumps(_dump(value))
        with self._conn() as conn, conn.cursor() as cur:
            self._purge_expired(cur)
            cur.execute(
                "INSERT INTO sessions (id, data, last_access) VALUES (%s, %s, now())",
                (sid, payload),
            )
            self._evict_overflow(cur)
            conn.commit()
        return sid

    def get(self, sid: str) -> dict | None:
        with self._conn() as conn, conn.cursor() as cur:
            self._purge_expired(cur)
            cur.execute("SELECT data FROM sessions WHERE id = %s", (sid,))
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return None
            cur.execute("UPDATE sessions SET last_access = now() WHERE id = %s", (sid,))
            conn.commit()
        data = row[0]
        if isinstance(data, str):
            data = json.loads(data)
        return _load(data)

    def save(self, sid: str, value: dict) -> None:
        payload = json.dumps(_dump(value))
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET data = %s, last_access = now() WHERE id = %s",
                (payload, sid),
            )
            conn.commit()

    def delete(self, sid: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE id = %s", (sid,))
            deleted = cur.rowcount
            conn.commit()
        return bool(deleted)

    def __len__(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sessions")
            (n,) = cur.fetchone()
            conn.commit()
        return int(n)


def _make_store():
    backend = config.SESSION_BACKEND.lower()
    if backend == "postgres":
        if not config.PG_DSN:
            log.warning("SESSION_BACKEND=postgres but PG_DSN is empty; using memory")
        else:
            try:
                s = PostgresStore(config.PG_DSN, config.SESSION_TTL_SECONDS, config.SESSION_MAX)
                log.info("session backend: postgres")
                return s
            except Exception as exc:  # noqa: BLE001 - fall back rather than crash
                log.warning("postgres session backend unavailable (%s); using memory", exc)
    return MemoryStore(config.SESSION_TTL_SECONDS, config.SESSION_MAX)


store = _make_store()
