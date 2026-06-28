# RAG (Retrieval-Augmented Generation)

RAG is **opt-in and off by default**. When enabled, relevant chunks from an
indexed corpus are injected into the coder's context before the pipeline runs.
The panel and lead never see raw RAG chunks directly - only the coder does.

## Why off by default

- Enabling RAG without an indexed corpus does nothing useful: `search()` returns
  `[]` and no context is added.
- Every RAG-enabled run pays one embeddings API call (billed tokens) even when
  the store is empty.
- The pgvector side container is already bundled in the compose stack, so there
  is no extra infrastructure cost to enabling it later.

The right time to enable RAG is when you have a meaningful corpus to index and
want the coder to be aware of your project's existing code, architecture docs,
or domain knowledge.

## Backends

| Backend     | Requires                  | Config                          |
|-------------|---------------------------|---------------------------------|
| `pgvector`  | Postgres + pgvector ext.  | `RAG_BACKEND=pgvector` (default) |
| `sqlite-vec`| `pip install sqlite-vec`  | `RAG_BACKEND=sqlite`            |

The `pgvector` backend is bundled in `docker-compose.yml` as a side container
(`pgvector/pgvector:pg17`). It starts automatically with `make up` and is
always available when using the default stack.

The `sqlite-vec` backend stores vectors in a single local file
(`SQLITE_VEC_PATH=rag.db` by default) and requires no server. Useful for
lightweight local development without Docker.

## Quickstart

### 1. Add documents

Drop `.md`, `.txt`, `.py`, or `.rst` files under `docs-projet/`:

```
docs-projet/
  architecture.md
  api-reference.md
  src/
    main.py
```

### 2. Index

```
make index
```

This runs `python -m src.rag --index docs-projet/` inside the app container,
chunks each file (1000 chars, 200-char overlap), embeds the chunks, and stores
them in the configured backend.

To index from the host directly (requires deps installed):

```
ZEN_API_KEY=... python -m src.rag --index path/to/docs
```

### 3. Enable per run

**API:**
```json
POST /api/run
{
  "spec": "add an endpoint that lists users inactive for 90 days",
  "use_rag": true
}
```

**CLI:**
```
make run SPEC="add user inactivity endpoint" USE_RAG=1
```

**Web UI:** toggle the "RAG" switch in the run form.

## How it works

1. The pipeline calls `rag.search(spec, k=RAG_TOP_K)` before dispatching to
   the topology.
2. Chunks whose cosine similarity to the spec is `>= RAG_MIN_SCORE` (default:
   0.2) are returned, up to `RAG_TOP_K` (default: 3).
3. The matching chunks are concatenated into a `context` string and passed to
   the coder as an "Internal context" prefix.
4. The topology receives `rag_sources` metadata (source file, chunk index,
   score) which appears in the result event and can be displayed by the UI.

If the backend is unavailable or the store is empty, `search()` returns `[]`
gracefully and the run continues without context. No error is raised.

## Configuration reference

| Variable        | Default                     | Purpose                                      |
|-----------------|-----------------------------|----------------------------------------------|
| `RAG_BACKEND`   | `pgvector`                  | `pgvector` or `sqlite`                       |
| `EMBED_PROVIDER`| `zen`                       | Provider used for embedding calls            |
| `EMBED_MODEL`   | `text-embedding-3-large`    | Embedding model                              |
| `EMBED_DIM`     | `3072`                      | Vector dimension (must match the model)      |
| `RAG_TOP_K`     | `3`                         | Number of chunks to retrieve                 |
| `RAG_MIN_SCORE` | `0.2`                       | Cosine similarity threshold (0..1)           |
| `PG_DSN`        | (set by compose)            | Postgres connection string (pgvector backend)|
| `SQLITE_VEC_PATH`| `rag.db`                   | SQLite file path (sqlite-vec backend)        |

## CLI tools

Search the index directly (useful for debugging relevance):

```
python -m src.rag --search "database connection pooling"
```

Re-index after document updates:

```
make index
# or
python -m src.rag --index docs-projet/
```

Chunks are inserted with `INSERT` (no deduplication). Clear the table manually
if you need a clean re-index:

```
psql $PG_DSN -c "TRUNCATE rag_chunks;"
```
