"""Optional RAG: embeddings via any configured provider, two storage backends.

Backends (RAG_BACKEND env var):
  pgvector  (default) - requires Postgres + pgvector extension + PG_DSN
  sqlite    (optional) - requires sqlite-vec; single-file, no server needed

Disabled by default in the pipeline.  Enable per-run with use_rag=True.
"""

import argparse
import asyncio
import logging

from . import config
from .llm import _client  # reuse the shared httpx factory (DRY)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Embeddings (shared by both backends)
# ---------------------------------------------------------------------------

async def embed(texts: list[str]) -> list[list[float]]:
    provider = config.get_provider(config.EMBED_PROVIDER)
    payload = {"model": config.EMBED_MODEL, "input": texts}
    async with _client(provider) as c:
        r = await c.post("/embeddings", json=payload)
        r.raise_for_status()
        data = r.json()
    return [d["embedding"] for d in data["data"]]


def _chunk(text: str, size: int = 1000, overlap: int = 200):
    out, i, idx = [], 0, 0
    while i < len(text):
        out.append((idx, text[i: i + size]))
        i += size - overlap
        idx += 1
    return out


# ---------------------------------------------------------------------------
# Backend: pgvector (default)
# ---------------------------------------------------------------------------

def _pg_conn():
    import psycopg
    from pgvector.psycopg import register_vector
    conn = psycopg.connect(config.PG_DSN)
    register_vector(conn)
    return conn


def _pg_init_schema() -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                chunk_idx INT NOT NULL,
                content TEXT NOT NULL,
                embedding vector({config.EMBED_DIM}),
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS rag_chunks_embedding_idx "
            "ON rag_chunks USING hnsw (embedding vector_cosine_ops);"
        )
        conn.commit()


async def _pg_index(directory: str) -> None:
    from pathlib import Path
    _pg_init_schema()
    root = Path(directory)
    files = [p for p in root.rglob("*") if p.suffix in {".md", ".txt", ".py", ".rst"}]
    if not files:
        print(f"no indexable files under {directory}")
        return
    for fp in files:
        text = fp.read_text(encoding="utf-8", errors="ignore")
        chunks = _chunk(text)
        vecs = await embed([c for _, c in chunks])
        with _pg_conn() as conn, conn.cursor() as cur:
            for (cidx, content), vec in zip(chunks, vecs, strict=False):
                cur.execute(
                    "INSERT INTO rag_chunks (source, chunk_idx, content, embedding) "
                    "VALUES (%s, %s, %s, %s)",
                    (str(fp), cidx, content, vec),
                )
            conn.commit()
        print(f"indexed {fp} ({len(chunks)} chunks)")


async def _pg_search(query: str, k: int, min_score: float) -> list[dict]:
    qvec = (await embed([query]))[0]
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source, chunk_idx, content, 1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks ORDER BY embedding <=> %s::vector LIMIT %s",
                (qvec, qvec, k),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - missing table / empty store
        log.warning("pgvector RAG search skipped (%s: %s)", type(exc).__name__, exc)
        return []
    hits = [
        {"source": s, "chunk_idx": i, "content": c, "score": float(sc)}
        for s, i, c, sc in rows
    ]
    return [h for h in hits if h["score"] >= min_score]


# ---------------------------------------------------------------------------
# Backend: sqlite-vec (optional)
# ---------------------------------------------------------------------------

async def _sqlite_index(directory: str) -> None:
    import sqlite3
    import struct
    from pathlib import Path

    import sqlite_vec  # type: ignore[import-untyped]

    db = sqlite3.connect(config.SQLITE_VEC_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks USING vec0("
        f"  source TEXT, chunk_idx INT, content TEXT,"
        f"  embedding float[{config.EMBED_DIM}]"
        f")"
    )
    root = Path(directory)
    files = [p for p in root.rglob("*") if p.suffix in {".md", ".txt", ".py", ".rst"}]
    if not files:
        print(f"no indexable files under {directory}")
        return
    for fp in files:
        text = fp.read_text(encoding="utf-8", errors="ignore")
        chunks = _chunk(text)
        vecs = await embed([c for _, c in chunks])
        for (cidx, content), vec in zip(chunks, vecs, strict=False):
            packed = struct.pack(f"{len(vec)}f", *vec)
            db.execute(
                "INSERT INTO rag_chunks(source, chunk_idx, content, embedding) VALUES (?,?,?,?)",
                (str(fp), cidx, content, packed),
            )
        db.commit()
        print(f"indexed {fp} ({len(chunks)} chunks)")
    db.close()


async def _sqlite_search(query: str, k: int, min_score: float) -> list[dict]:
    import sqlite3
    import struct

    import sqlite_vec  # type: ignore[import-untyped]

    qvec = (await embed([query]))[0]
    packed = struct.pack(f"{len(qvec)}f", *qvec)
    try:
        db = sqlite3.connect(config.SQLITE_VEC_PATH)
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        rows = db.execute(
            f"SELECT source, chunk_idx, content, distance "
            f"FROM rag_chunks WHERE embedding MATCH ? AND k={k} "
            f"ORDER BY distance",
            (packed,),
        ).fetchall()
        db.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("sqlite-vec RAG search skipped (%s: %s)", type(exc).__name__, exc)
        return []
    hits = [
        {"source": s, "chunk_idx": i, "content": c, "score": max(0.0, 1.0 - float(d))}
        for s, i, c, d in rows
    ]
    return [h for h in hits if h["score"] >= min_score]


# ---------------------------------------------------------------------------
# Public API (dispatch to configured backend)
# ---------------------------------------------------------------------------

async def index_directory(directory: str) -> None:
    if config.RAG_BACKEND == "sqlite":
        await _sqlite_index(directory)
    else:
        await _pg_index(directory)


async def search(query: str, k: int = 5, min_score: float | None = None) -> list[dict]:
    """Return top-k chunks above min_score. Robust: returns [] instead of raising."""
    if min_score is None:
        min_score = config.RAG_MIN_SCORE
    if config.RAG_BACKEND == "sqlite":
        return await _sqlite_search(query, k, min_score)
    return await _pg_search(query, k, min_score)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", metavar="DIR")
    ap.add_argument("--search", metavar="QUERY")
    args = ap.parse_args()
    if args.index:
        asyncio.run(index_directory(args.index))
    elif args.search:
        for h in asyncio.run(search(args.search)):
            print(f"[{h['score']:.3f}] {h['source']}#{h['chunk_idx']}: {h['content'][:160]}")
    else:
        ap.print_help()
