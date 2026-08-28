"""Central configuration. Everything comes from the environment.

Providers and their transports:
  zen        OpenAI-compatible  https://opencode.ai/zen/v1       Authorization: Bearer
  openai     OpenAI-compatible  configurable base_url             Authorization: Bearer
  openrouter OpenAI-compatible  https://openrouter.ai/api/v1      Authorization: Bearer
  anthropic  Anthropic Messages https://api.anthropic.com/v1      x-api-key + anthropic-version
  local      OpenAI-compatible  configurable base_url             Authorization: Bearer
             (llama.cpp / Ollama / vLLM)

Each provider entry maps a logical name to (base_url, auth header, transport).
Models reference a provider by name in PANEL and role env vars.
"""

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provider:
    """One configured provider (endpoint + auth + wire format)."""

    name: str
    base_url: str
    # transport: "openai-compatible" | "anthropic"
    transport: str
    auth_header: str  # header name  ("Authorization" or "x-api-key")
    auth_value: str  # header value (e.g. "Bearer sk-..." or raw key)
    verify_tls: "bool | str" = True  # True | False | "/path/to/ca.pem"
    extra_headers: dict = field(default_factory=dict)
    # Extra JSON merged into every completion payload for this provider
    # (provider-specific knobs, e.g. llama.cpp "cache_prompt": true).
    extra_payload: dict = field(default_factory=dict)
    # Ask the provider to return usage/cost in the response (OpenRouter).
    request_usage: bool = False


def _tls(raw: str) -> "bool | str":
    """Parse a TLS verification value: path | true | false | __insecure__."""
    raw = raw.strip()
    if raw.lower() in ("false", "0", "no", "__insecure__"):
        log.warning("TLS verification disabled (value=%r) - traffic is not verified.", raw)
        return False
    if raw.lower() in ("true", "1", "yes", ""):
        return True
    return raw  # filesystem path to a CA bundle


def _bearer(key: str) -> tuple[str, str]:
    return "Authorization", f"Bearer {key}"


def _apikey(key: str) -> tuple[str, str]:
    return "x-api-key", key


def _extra_payload(name: str, default: dict | None = None) -> dict:
    """Parse PROVIDER_EXTRA_PAYLOAD_<NAME> (JSON) merged over `default`."""
    payload: dict = dict(default or {})
    raw = os.environ.get(f"PROVIDER_EXTRA_PAYLOAD_{name.upper()}", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload.update(parsed)
            else:
                log.warning("PROVIDER_EXTRA_PAYLOAD_%s: not a JSON object, ignored", name.upper())
        except json.JSONDecodeError as exc:
            log.warning("PROVIDER_EXTRA_PAYLOAD_%s: invalid JSON (%s), ignored", name.upper(), exc)
    return payload


# ---------------------------------------------------------------------------
# Build the provider registry from environment variables.
# ---------------------------------------------------------------------------


def _build_providers() -> dict[str, Provider]:
    """Build the active provider map from env.  Only providers whose key is
    set are registered; missing keys produce a warning (not a crash) so the
    app starts even when only some providers are configured."""
    providers: dict[str, Provider] = {}

    # --- Zen (default, free) -----------------------------------------------
    zen_key = os.environ.get("ZEN_API_KEY", "")
    zen_url = os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/v1").rstrip("/")
    if zen_key:
        hdr, val = _bearer(zen_key)
        providers["zen"] = Provider(
            name="zen",
            base_url=zen_url,
            transport="openai-compatible",
            auth_header=hdr,
            auth_value=val,
            verify_tls=_tls(os.environ.get("ZEN_CA_BUNDLE", "true")),
        )
    else:
        log.warning("ZEN_API_KEY not set; 'zen' provider unavailable")

    # --- OpenAI-compatible (generic: OpenAI, vLLM, any /v1 endpoint) --------
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if openai_key:
        hdr, val = _bearer(openai_key)
        providers["openai"] = Provider(
            name="openai",
            base_url=openai_url,
            transport="openai-compatible",
            auth_header=hdr,
            auth_value=val,
            extra_payload=_extra_payload("openai"),
        )

    # --- OpenRouter (OpenAI-compatible gateway) -----------------------------
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    openrouter_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip(
        "/"
    )
    if openrouter_key:
        hdr, val = _bearer(openrouter_key)
        providers["openrouter"] = Provider(
            name="openrouter",
            base_url=openrouter_url,
            transport="openai-compatible",
            auth_header=hdr,
            auth_value=val,
            extra_payload=_extra_payload("openrouter"),
            request_usage=True,  # "usage": {"include": true} -> cost in response
        )

    # --- Anthropic (Messages API, native streaming) -------------------------
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
    if anthropic_key:
        hdr, val = _apikey(anthropic_key)
        providers["anthropic"] = Provider(
            name="anthropic",
            base_url=anthropic_url,
            transport="anthropic",
            auth_header=hdr,
            auth_value=val,
            extra_headers={"anthropic-version": "2023-06-01"},
        )

    # --- Local (llama.cpp / Ollama / vLLM, OpenAI-compatible) ---------------
    # cache_prompt keeps the KV prefix warm server-side between calls (llama.cpp);
    # override with PROVIDER_EXTRA_PAYLOAD_LOCAL='{}' for servers that reject it.
    local_url = os.environ.get("LOCAL_BASE_URL", "")
    if local_url:
        local_key = os.environ.get("LOCAL_API_KEY", "ollama")
        hdr, val = _bearer(local_key)
        providers["local"] = Provider(
            name="local",
            base_url=local_url.rstrip("/"),
            transport="openai-compatible",
            auth_header=hdr,
            auth_value=val,
            verify_tls=_tls(os.environ.get("LOCAL_CA_BUNDLE", "true")),
            extra_payload=_extra_payload("local", default={"cache_prompt": True}),
        )

    if not providers:
        raise RuntimeError(
            "No provider configured. Set at least ZEN_API_KEY (free) or "
            "OPENROUTER_API_KEY or OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "or LOCAL_BASE_URL."
        )

    log.info("providers: %s", list(providers))
    return providers


PROVIDERS: dict[str, Provider] = _build_providers()


def get_provider(name: str) -> Provider:
    """Return a configured provider by name, raise clearly if missing."""
    p = PROVIDERS.get(name)
    if p is None:
        available = list(PROVIDERS)
        raise KeyError(
            f"Provider {name!r} is not configured. "
            f"Available: {available}. Set the matching *_API_KEY env var."
        )
    return p


# ---------------------------------------------------------------------------
# HTTP / timeouts
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "300"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
ANTHROPIC_VERSION = "2023-06-01"


def setup_logging() -> None:
    """Idempotent logging setup for app entrypoints (API and CLI)."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))
SESSION_MAX = int(os.environ.get("SESSION_MAX", "200"))
SESSION_BACKEND = os.environ.get("SESSION_BACKEND", "memory")

# ---------------------------------------------------------------------------
# Input caps (reject before any billable call)
# ---------------------------------------------------------------------------

MAX_SPEC_CHARS = int(os.environ.get("MAX_SPEC_CHARS", "20000"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "8000"))
MAX_ARCHIVE_FILES = int(os.environ.get("MAX_ARCHIVE_FILES", "500"))
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES", str(50 * 1024 * 1024)))

# ---------------------------------------------------------------------------
# SSE / concurrency
# ---------------------------------------------------------------------------

SSE_HEARTBEAT_SECONDS = float(os.environ.get("SSE_HEARTBEAT_SECONDS", "10"))
MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "2"))

# ---------------------------------------------------------------------------
# Rate-limit / retry (handled by the governor)
# ---------------------------------------------------------------------------

RATE_LIMIT_MAX_RETRIES = int(os.environ.get("RATE_LIMIT_MAX_RETRIES", "4"))
RATE_LIMIT_BASE_DELAY = float(os.environ.get("RATE_LIMIT_BASE_DELAY", "2"))
RATE_LIMIT_MAX_DELAY = float(os.environ.get("RATE_LIMIT_MAX_DELAY", "60"))
RATE_LIMIT_RETRY_STATUSES = {
    int(s) for s in os.environ.get("RATE_LIMIT_RETRY_STATUSES", "429,503").split(",") if s.strip()
}
# Provider RPM cap for aiolimiter (per provider name, env PROVIDER_RPM_<NAME>).
# Zen free tier is generous; set to 0 to disable the limiter for a provider.
_DEFAULT_RPM = int(os.environ.get("DEFAULT_RPM", "60"))


def provider_rpm(name: str) -> int:
    return int(os.environ.get(f"RPM_{name.upper()}", str(_DEFAULT_RPM)))


AUTO_LOW_QUOTA = os.environ.get("AUTO_LOW_QUOTA", "1").strip().lower() in ("1", "true", "yes")

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:8800").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# Models per role
# ---------------------------------------------------------------------------

# Provider:model pairs.  Format: "provider/model-id"  e.g. "zen/big-pickle".
# Defaults favour free-tier models; override per environment. `make setup`
# probes your configured providers and writes the exact ids into .env.
CODER_MODEL = os.environ.get("CODER_MODEL", "zen/big-pickle")
CONSENSUS_MODEL = os.environ.get("CONSENSUS_MODEL", "zen/deepseek-v4-flash-free")
LEAD_MODEL = os.environ.get("LEAD_MODEL", "zen/deepseek-v4-flash-free")
# Chat/exploration model: empty -> falls back to LEAD_MODEL. Point it at a
# local model (e.g. "local/qwen3-8b") for free, private exploration chats.
CHAT_MODEL = os.environ.get("CHAT_MODEL", "")

CODER_MAX_TOKENS = int(os.environ.get("CODER_MAX_TOKENS", "8000"))
REVIEW_MAX_TOKENS = int(os.environ.get("REVIEW_MAX_TOKENS", "8000"))
CONSENSUS_MAX_TOKENS = int(os.environ.get("CONSENSUS_MAX_TOKENS", "8000"))
LEAD_MAX_TOKENS = int(os.environ.get("LEAD_MAX_TOKENS", "16000"))
CHAT_MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "4000"))


# Per-role provider fallback chain: comma-separated provider names tried in
# order when the primary provider exhausts its retries.
# Example: CODER_FALLBACK=local  (fall back to a local LLM when Zen is down)
def _parse_fallback(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()] if raw.strip() else []


CODER_FALLBACK = _parse_fallback(os.environ.get("CODER_FALLBACK", ""))
REVIEWER_FALLBACK = _parse_fallback(os.environ.get("REVIEWER_FALLBACK", ""))
CONSENSUS_FALLBACK = _parse_fallback(os.environ.get("CONSENSUS_FALLBACK", ""))
LEAD_FALLBACK = _parse_fallback(os.environ.get("LEAD_FALLBACK", ""))

# Low-quota degraded profile (Lead is never downgraded)
LOW_QUOTA_MODEL = os.environ.get("LOW_QUOTA_MODEL", CODER_MODEL)
LOW_QUOTA_PANEL_SIZE = int(os.environ.get("LOW_QUOTA_PANEL_SIZE", "2"))

# ---------------------------------------------------------------------------
# Panel parsing  (format: "name:provider/model[:max_tokens]")
# Separator between provider and model is "/" (Anthropic model IDs use "/" too,
# so the field separator is ":" which never appears in provider or model names
# when using the "provider/model" convention).
# ---------------------------------------------------------------------------

_VALID_TRANSPORTS = set(("zen", "openai", "openrouter", "anthropic", "local"))

# Default panel: the only universally free Zen coding model today.
# NOTE: a single-model panel gives trivial consensus - run `make setup` to
# write a real REVIEW_PANEL from your providers (2-4 *different* models).
_DEFAULT_PANEL = [
    {"name": "coder", "provider": "zen", "model": "big-pickle"},
]


def _parse_panel(raw: str) -> list[dict]:
    """Parse REVIEW_PANEL: comma-separated  'name:provider/model[:max_tokens]'.

    Falls back to the default Zen panel when empty or fully invalid.
    Skips malformed entries with a warning (resilient by design).

    Ollama model IDs may contain colons (e.g. ``local/qwen:7b``).  The last
    colon‑separated segment is treated as ``max_tokens`` only when it is a
    pure integer >= 256; otherwise it is kept as part of the model name.
    """
    raw = (raw or "").strip()
    if not raw:
        return list(_DEFAULT_PANEL)

    out: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        # Detect trailing :N where N is an integer >= 256 (max_tokens).
        # Ollama model IDs like ``local/qwen:7b`` have a colon in the model
        # name; if the trailing part is not a pure integer we keep it as
        # part of the model rather than rejecting the entry.
        trailing_is_max = len(parts) > 2 and parts[-1].isdigit() and int(parts[-1]) >= 256
        if trailing_is_max:
            # :N is max_tokens: name | provider/model | N
            name = parts[0]
            provider_model = ":".join(parts[1:-1])
            max_tokens = int(parts[-1])
        else:
            # No valid trailing max_tokens: name | provider/model (may contain ':')
            name = parts[0]
            provider_model = ":".join(parts[1:]) if len(parts) > 1 else ""

        if not name or "/" not in provider_model:
            log.warning("REVIEW_PANEL: skipping malformed entry %r", entry)
            continue
        provider, model = provider_model.split("/", 1)
        if provider not in PROVIDERS:
            log.warning(
                "REVIEW_PANEL: provider %r not configured (entry %r), skipping",
                provider,
                entry,
            )
            continue
        member: dict = {"name": name, "provider": provider, "model": model}
        if trailing_is_max:
            member["max_tokens"] = max_tokens
        out.append(member)

    if not out:
        log.warning("REVIEW_PANEL produced no valid reviewers; using the default panel")
        return list(_DEFAULT_PANEL)
    return out


PANEL = _parse_panel(os.environ.get("REVIEW_PANEL", ""))

_low_panel_raw = os.environ.get("LOW_QUOTA_PANEL", "")
LOW_QUOTA_PANEL = (
    _parse_panel(_low_panel_raw) if _low_panel_raw.strip() else PANEL[:LOW_QUOTA_PANEL_SIZE]
)

# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-large")
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "zen")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "3072"))
RAG_MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.2"))
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "3"))
RAG_BACKEND = os.environ.get("RAG_BACKEND", "pgvector")  # "pgvector" | "sqlite"

PG_DSN = os.environ.get("PG_DSN", "")
SQLITE_VEC_PATH = os.environ.get("SQLITE_VEC_PATH", "rag.db")
