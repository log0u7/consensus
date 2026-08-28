"""Dynamic model pricing and context windows.

Single source of truth for "what does a token cost" and "how large is the
context window", derived at runtime from OpenRouter's public catalog
(https://openrouter.ai/api/v1/models - no API key required). Nothing is
hardcoded per model: free models report price 0, paid models report
USD-per-token rates.

Disk cache reuses the sqlite pattern of cache.py: the full catalog is stored
under one key with a fetch timestamp. Lookups are synchronous and never
block on the network; when the cache is stale (older than PRICING_TTL_HOURS)
a background refresh is kicked off fire-and-forget the first time pricing is
consulted from a running event loop. If no data is available, lookups return
None (cost unknown) - pricing never fails a run.

Model id mapping (OpenRouter ids are "vendor/model"):
  openai/gpt-4o          -> exact id "openai/gpt-4o"
  openrouter/vendor/id   -> model part is already "vendor/model"
  anthropic/claude-...   -> exact id "anthropic/claude-..."
  zen/big-pickle         -> unique suffix match ("z-ai/big-pickle" etc.)
  local/<anything>       -> free (0.0), no lookup

Environment variables:
  PRICING_URL=https://openrouter.ai/api/v1/models
  PRICING_DB_PATH=pricing.db  sqlite file path
  PRICING_TTL_HOURS=24        refresh threshold for the cached catalog
  PRICING_REFRESH=1           set 0 to disable network refresh (offline tests)
  CONTEXT_WINDOW_<PROVIDER>   manual context window override (tokens)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time

import httpx

log = logging.getLogger(__name__)

PRICING_URL = os.environ.get("PRICING_URL", "https://openrouter.ai/api/v1/models")
PRICING_DB_PATH = os.environ.get("PRICING_DB_PATH", "pricing.db")
PRICING_TTL_HOURS = float(os.environ.get("PRICING_TTL_HOURS", "24"))
PRICING_TIMEOUT = float(os.environ.get("PRICING_TIMEOUT", "15"))
PRICING_REFRESH = os.environ.get("PRICING_REFRESH", "1").strip() not in ("0", "false", "no")

_FREE_VALUE_IGNORED = ("", None, "-1")


def _num(raw: object) -> float | None:
    """Parse an OpenRouter pricing field (string) into USD/token, or None."""
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v < 0:  # -1 means "variable / not reported"
        return None
    return v


def parse_catalog(payload: dict) -> dict[str, dict]:
    """Extract {id: {prompt, completion, cache_read, context}} from an
    OpenRouter /models response. Tolerant: rows missing fields are dropped,
    malformed entries are skipped."""
    out: dict[str, dict] = {}
    for entry in payload.get("data", []):
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        p = entry.get("pricing") or {}
        ctx = entry.get("context_length")
        out[model_id] = {
            "prompt": _num(p.get("prompt")),
            "completion": _num(p.get("completion")),
            "cache_read": _num(p.get("input_cache_read")),
            "context": int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None,
        }
    return out


# ---------------------------------------------------------------------------
# sqlite-backed catalog cache
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(PRICING_DB_PATH, check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS pricing "
        "(key TEXT PRIMARY KEY, value TEXT, fetched REAL DEFAULT 0)"
    )
    return db


class _Catalog:
    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._fetched: float = 0.0
        self._loaded = False
        self._by_suffix: dict[str, list[str]] = {}
        self._refresh_task: asyncio.Task | None = None

    # -- loading ---------------------------------------------------------

    def _load_disk(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        # Read-only when absent: never create the db file from a mere lookup
        # (offline tests and cost-only inspection stay side-effect free).
        if not os.path.exists(PRICING_DB_PATH):
            return
        try:
            db = _connect()
            row = db.execute(
                "SELECT value, fetched FROM pricing WHERE key = 'openrouter'"
            ).fetchone()
            db.close()
        except sqlite3.Error as exc:
            log.debug("pricing disk cache unavailable: %s", exc)
            return
        if not row:
            return
        try:
            self._rows = json.loads(row[0])
            self._fetched = float(row[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("pricing disk cache corrupt, ignoring")
            self._rows = {}
            self._fetched = 0.0
        self._reindex()

    def _reindex(self) -> None:
        self._by_suffix = {}
        for model_id in self._rows:
            suffix = model_id.split("/", 1)[1] if "/" in model_id else model_id
            self._by_suffix.setdefault(suffix, []).append(model_id)

    def _store(self, rows: dict[str, dict]) -> None:
        self._rows = rows
        self._fetched = time.time()
        self._reindex()
        try:
            db = _connect()
            db.execute(
                "INSERT OR REPLACE INTO pricing (key, value, fetched) VALUES (?, ?, ?)",
                ("openrouter", json.dumps(rows), self._fetched),
            )
            db.commit()
            db.close()
        except sqlite3.Error as exc:
            log.warning("pricing disk cache write failed: %s", exc)

    # -- refresh -----------------------------------------------------------

    @property
    def stale(self) -> bool:
        return not self._rows or (time.time() - self._fetched) > PRICING_TTL_HOURS * 3600

    def refresh_sync(self, timeout: float = PRICING_TIMEOUT) -> bool:
        """Blocking fetch (setup wizard). Returns True on success."""
        try:
            resp = httpx.get(PRICING_URL, timeout=timeout)
            resp.raise_for_status()
            rows = parse_catalog(resp.json())
        except Exception as exc:  # noqa: BLE001 - never fail a run over pricing
            log.warning("pricing fetch failed: %s", exc)
            return False
        if not rows:
            log.warning("pricing fetch returned an empty catalog")
            return False
        self._store(rows)
        log.info("pricing catalog refreshed: %d models", len(rows))
        return True

    def _kick_async(self) -> None:
        """Fire-and-forget refresh from a running event loop (never blocks)."""
        if not PRICING_REFRESH:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _refresh() -> None:
            await asyncio.to_thread(self.refresh_sync)

        self._refresh_task = loop.create_task(_refresh())

    def ensure(self) -> None:
        """Load disk cache once; kick a background refresh when stale."""
        self._load_disk()
        if self.stale:
            self._kick_async()

    # -- lookups -----------------------------------------------------------

    def row_for(self, provider: str, model: str) -> dict | None:
        exact = f"{provider}/{model}"
        if exact in self._rows:
            return self._rows[exact]
        # zen (and any provider whose ids are not OpenRouter vendor/model):
        # match on the model-id suffix when unambiguous.
        candidates = self._by_suffix.get(model, [])
        if len(candidates) == 1:
            return self._rows[candidates[0]]
        return None

    def stats(self) -> dict:
        self._load_disk()
        age = time.time() - self._fetched if self._fetched else None
        return {
            "path": PRICING_DB_PATH,
            "entries": len(self._rows),
            "fetched_at": self._fetched or None,
            "age_hours": round(age / 3600, 2) if age is not None else None,
            "stale": self.stale,
        }


_catalog = _Catalog()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cost_for(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float | None:
    """USD cost of one call, or None when unknown. Local models are free.

    Cached input tokens are billed at the provider's cache-read rate
    (falling back to the prompt rate when the provider does not report one).
    """
    if provider == "local":
        return 0.0
    _catalog.ensure()
    row = _catalog.row_for(provider, model)
    if row is None:
        return None
    prompt, completion = row["prompt"], row["completion"]
    if prompt is None or completion is None:
        return None
    cached = max(0, min(cached_tokens, input_tokens))
    cache_rate = row["cache_read"] if row["cache_read"] is not None else prompt
    cost = (input_tokens - cached) * prompt + cached * cache_rate + output_tokens * completion
    return cost


def context_window_for(ref: str) -> int | None:
    """Context window (tokens) for a 'provider/model' reference.

    Order: pricing catalog, then an explicit CONTEXT_WINDOW_<PROVIDER> env
    override, then the static capability table (providers.caps). None when
    everything is unknown.
    """
    provider, _, model = ref.partition("/")
    _catalog.ensure()
    row = _catalog.row_for(provider, model) if model else None
    if row and row["context"]:
        return row["context"]
    env = os.environ.get(f"CONTEXT_WINDOW_{provider.upper()}", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    # Lazy import: providers pulls in config, whose import-time provider
    # validation must not be a dependency of a standalone pricing lookup.
    from . import providers

    caps = providers.caps(provider, model)
    return caps.get("context_window")


def refresh_sync(timeout: float = PRICING_TIMEOUT) -> bool:
    """Blocking catalog fetch (setup wizard, tests)."""
    return _catalog.refresh_sync(timeout=timeout)


def stats() -> dict:
    return _catalog.stats()
