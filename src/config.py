"""Central configuration. Everything comes from the environment.

Providers and their transports:
  zen       OpenAI-compatible  https://opencode.ai/zen/v1       Authorization: Bearer
  openai    OpenAI-compatible  configurable base_url             Authorization: Bearer
  anthropic Anthropic Messages https://api.anthropic.com/v1      x-api-key + anthropic-version
  local     OpenAI-compatible  configurable base_url (Ollama...) Authorization: Bearer

Each provider entry maps a logical name to (base_url, auth header, transport).
Models reference a provider by name in PANEL and role env vars.
"""

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
    auth_header: str   # header name  ("Authorization" or "x-api-key")
    auth_value: str    # header value (e.g. "Bearer sk-..." or raw key)
    verify_tls: "bool | str" = True  # True | False | "/path/to/ca.pem"
    extra_headers: dict = field(default_factory=dict)


def _tls(raw: str) -> "bool | str":
    """Parse a TLS verification value: path | true | false | __insecure__."""
    raw = raw.strip()
    if raw.lower() in ("false", "0", "no", "__insecure__"):
        log.warning(
            "TLS verification disabled (value=%r) - traffic is not verified.", raw
        )
        return False
    if raw.lower() in ("true", "1", "yes", ""):
        return True
    return raw  # filesystem path to a CA bundle


def _bearer(key: str) -> tuple[str, str]:
    return "Authorization", f"Bearer {key}"


def _apikey(key: str) -> tuple[str, str]:
    return "x-api-key", key


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

    # --- OpenAI-compatible (generic: OpenAI, OpenRouter, ...) ---------------
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
        )

    # --- Anthropic (Messages API, native streaming) -------------------------
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_url = os.environ.get(
        "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"
    ).rstrip("/")
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

    # --- Local (Ollama / vLLM / llama.cpp, OpenAI-compatible) ---------------
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
        )

    if not providers:
        raise RuntimeError(
            "No provider configured. Set at least ZEN_API_KEY (free) or "
            "OPENAI_API_KEY or ANTHROPIC_API_KEY."
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
    int(s)
    for s in os.environ.get("RATE_LIMIT_RETRY_STATUSES", "429,503").split(",")
    if s.strip()
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

# Provider:model pairs.  Format: "provider/model-id"  e.g. "zen/qwen3-coder"
# All three default to reasoner-class models on Zen.
CODER_MODEL    = os.environ.get("CODER_MODEL",     "zen/deepseek-v3-0324")
CONSENSUS_MODEL = os.environ.get("CONSENSUS_MODEL", "zen/deepseek-r1-0528")
LEAD_MODEL     = os.environ.get("LEAD_MODEL",      "zen/deepseek-r1-0528")

CODER_MAX_TOKENS    = int(os.environ.get("CODER_MAX_TOKENS",    "8000"))
REVIEW_MAX_TOKENS   = int(os.environ.get("REVIEW_MAX_TOKENS",   "8000"))
CONSENSUS_MAX_TOKENS = int(os.environ.get("CONSENSUS_MAX_TOKENS", "8000"))
LEAD_MAX_TOKENS     = int(os.environ.get("LEAD_MAX_TOKENS",     "16000"))
CHAT_MAX_TOKENS     = int(os.environ.get("CHAT_MAX_TOKENS",     "4000"))

# Low-quota degraded profile (Lead is never downgraded)
LOW_QUOTA_MODEL      = os.environ.get("LOW_QUOTA_MODEL",      "zen/deepseek-v3-0324")
LOW_QUOTA_PANEL_SIZE = int(os.environ.get("LOW_QUOTA_PANEL_SIZE", "2"))

# ---------------------------------------------------------------------------
# Panel parsing  (format: "name:provider/model[:max_tokens]")
# Separator between provider and model is "/" (Anthropic model IDs use "/" too,
# so the field separator is ":" which never appears in provider or model names
# when using the "provider/model" convention).
# ---------------------------------------------------------------------------

_VALID_TRANSPORTS = set(("zen", "openai", "anthropic", "local"))

_DEFAULT_PANEL = [
    {"name": "deepseek-coder", "provider": "zen", "model": "deepseek-v3-0324"},
    {"name": "qwen3-coder",    "provider": "zen", "model": "qwen3-coder"},
    {"name": "mimo-vl",        "provider": "zen", "model": "mimo-vl-7b-rl"},
]


def _parse_panel(raw: str) -> list[dict]:
    """Parse REVIEW_PANEL: comma-separated  'name:provider/model[:max_tokens]'.

    Falls back to the default Zen panel when empty or fully invalid.
    Skips malformed entries with a warning (resilient by design).
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
        if len(parts) not in (2, 3) or not all(parts[:2]):
            log.warning("REVIEW_PANEL: skipping malformed entry %r", entry)
            continue
        name, provider_model = parts[0], parts[1]
        if "/" not in provider_model:
            log.warning(
                "REVIEW_PANEL: entry %r missing 'provider/model' format, skipping", entry
            )
            continue
        provider, model = provider_model.split("/", 1)
        if provider not in PROVIDERS:
            log.warning(
                "REVIEW_PANEL: provider %r not configured (entry %r), skipping",
                provider, entry,
            )
            continue
        member: dict = {"name": name, "provider": provider, "model": model}
        if len(parts) == 3:
            try:
                member["max_tokens"] = int(parts[2])
            except ValueError:
                log.warning(
                    "REVIEW_PANEL: ignoring non-integer max_tokens in %r", entry
                )
        out.append(member)

    if not out:
        log.warning("REVIEW_PANEL produced no valid reviewers; using the default panel")
        return list(_DEFAULT_PANEL)
    return out


PANEL = _parse_panel(os.environ.get("REVIEW_PANEL", ""))

_low_panel_raw = os.environ.get("LOW_QUOTA_PANEL", "")
LOW_QUOTA_PANEL = (
    _parse_panel(_low_panel_raw) if _low_panel_raw.strip()
    else PANEL[:LOW_QUOTA_PANEL_SIZE]
)

# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

EMBED_MODEL  = os.environ.get("EMBED_MODEL",  "text-embedding-3-large")
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "zen")
EMBED_DIM    = int(os.environ.get("EMBED_DIM",    "3072"))
RAG_MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.2"))
RAG_TOP_K    = int(os.environ.get("RAG_TOP_K",    "3"))
RAG_BACKEND  = os.environ.get("RAG_BACKEND", "pgvector")  # "pgvector" | "sqlite"

PG_DSN = os.environ.get("PG_DSN", "")
SQLITE_VEC_PATH = os.environ.get("SQLITE_VEC_PATH", "rag.db")
