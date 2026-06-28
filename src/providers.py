"""Provider registry: resolve a 'provider/model' reference to a configured
Provider + model string, with optional capability metadata (reasoning,
context_window, max_tokens).

This module is the single source of truth for provider resolution.
config.py owns the env-driven Provider objects; providers.py owns the
resolution logic and capability annotations.

Usage:
    from src.providers import resolve, has_reasoning

    provider, model = resolve("zen/deepseek-r1-0528")
    # provider is a config.Provider; model is "deepseek-r1-0528"
"""

from __future__ import annotations

import logging

from . import config
from .config import Provider

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static capability hints (reasoning flag, typical context sizes).
# Populated from known models; unknown models get safe defaults.
# Format: "provider/model-id" -> {reasoning, context_window, max_tokens}
# ---------------------------------------------------------------------------

_CAPS: dict[str, dict] = {
    # Zen - reasoning
    "zen/deepseek-r1-0528":         {"reasoning": True,  "context_window": 128_000, "max_tokens": 32_000},
    "zen/nemotron-ultra":            {"reasoning": True,  "context_window": 128_000, "max_tokens": 32_000},
    "zen/mimo-vl-7b-rl":             {"reasoning": True,  "context_window": 32_768,  "max_tokens": 8_000},
    # Zen - fast
    "zen/deepseek-v3-0324":          {"reasoning": False, "context_window": 128_000, "max_tokens": 16_000},
    "zen/qwen3-coder":               {"reasoning": False, "context_window": 128_000, "max_tokens": 16_000},
    # Anthropic
    "anthropic/claude-opus-latest":  {"reasoning": False, "context_window": 200_000, "max_tokens": 32_000},
    "anthropic/claude-sonnet-latest":{"reasoning": False, "context_window": 200_000, "max_tokens": 16_000},
}

_DEFAULT_CAPS: dict = {"reasoning": False, "context_window": 128_000, "max_tokens": 16_000}


def caps(provider_name: str, model: str) -> dict:
    """Return capability metadata for a provider/model pair."""
    key = f"{provider_name}/{model}"
    return _CAPS.get(key, _DEFAULT_CAPS)


def has_reasoning(provider_name: str, model: str) -> bool:
    return caps(provider_name, model).get("reasoning", False)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(ref: str) -> tuple[Provider, str]:
    """Resolve a 'provider/model' reference to (Provider, model_id).

    Raises KeyError (via config.get_provider) if the provider is not
    configured, with a clear error message pointing to the missing key.
    """
    if "/" not in ref:
        raise ValueError(
            f"Invalid model reference {ref!r}: expected 'provider/model-id' format "
            "(e.g. 'zen/deepseek-r1-0528')."
        )
    provider_name, model = ref.split("/", 1)
    provider = config.get_provider(provider_name)
    return provider, model


def resolve_name(ref: str) -> tuple[str, str]:
    """Like resolve() but returns (provider_name, model) instead of the Provider object."""
    if "/" not in ref:
        raise ValueError(f"Invalid model reference {ref!r}: expected 'provider/model-id'.")
    provider_name, model = ref.split("/", 1)
    # Validate the provider is configured.
    config.get_provider(provider_name)
    return provider_name, model


def list_configured() -> list[str]:
    """Return all configured provider names."""
    return list(config.PROVIDERS)
