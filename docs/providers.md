# Providers

All LLM calls go through `src/llm.py` via raw `httpx` - no SDK. Providers are
registered in `src/config.py` and resolved by name at runtime.

## Model reference format

Models are referenced everywhere as `provider/model-id`:

```
zen/big-pickle
anthropic/claude-opus-latest
openai/gpt-4o
openrouter/deepseek/deepseek-r1
local/qwen3-8b
```

This format is used in team YAMLs, environment variables (`CODER_MODEL`,
`LEAD_MODEL`, `REVIEW_PANEL`), and API responses.

## Built-in providers

| Name         | Transport          | Endpoint env var       | Default base URL                          |
|--------------|--------------------|------------------------|-------------------------------------------|
| `zen`        | OpenAI-compatible  | `ZEN_BASE_URL`         | `https://opencode.ai/zen/v1`              |
| `openrouter` | OpenAI-compatible  | `OPENROUTER_BASE_URL`  | `https://openrouter.ai/api/v1`            |
| `openai`     | OpenAI-compatible  | `OPENAI_BASE_URL`      | `https://api.openai.com/v1`               |
| `anthropic`  | Anthropic Messages | `ANTHROPIC_BASE_URL`   | `https://api.anthropic.com/v1`            |
| `local`      | OpenAI-compatible  | `LOCAL_BASE_URL`       | `http://127.0.0.1:8080` (llama.cpp server) |

The `zen` provider is free (no credit card required) at opencode.ai. It proxies
a wide range of open-weight and frontier models.

### OpenRouter

Configure `OPENROUTER_API_KEY` and the app registers the `openrouter` provider
(OpenAI-compatible transport). The app sends `"usage": {"include": true}` on
every call so OpenRouter reports the real cost, which is shown in the UI and
stored in `Usage.cost`.

### Local (llama.cpp / Ollama / vLLM)

Set `LOCAL_BASE_URL` (OpenAI-compatible endpoint) and optionally
`LOCAL_API_KEY`. The default request payload includes
`PROVIDER_EXTRA_PAYLOAD_LOCAL={"cache_prompt":true}` so llama.cpp keeps the KV
prefix warm between calls; override that variable with `{}` for servers that
reject the field. Local models are treated as free (cost 0.0); set
`CONTEXT_WINDOW_LOCAL` so the UI can show the context fill level.

## Cost and context metering

`src/pricing.py` fetches OpenRouter's public model catalog (no key required)
and caches it in `pricing.db` (`PRICING_TTL_HOURS`, default 24). It provides:

- per-call cost fallback when a provider does not report one,
- context windows for the UI's context meter,
- free-model detection used by the `make setup` routing suggestions.

Lookups never block on the network: the cached catalog is served immediately
and a stale catalog is refreshed in the background. When nothing is known,
costs show as `n/a` and the run is unaffected.

## Prompt caching

The context builder keeps the stable prefix (system + skills + tools) first,
so providers can serve it from their prefix cache:

- **Anthropic**: the system prompt is sent as a `cache_control: ephemeral`
  block; `cache_read_input_tokens` from the response (and from the streaming
  `message_start` event) is recorded as `Usage.cached_tokens`.
- **OpenAI-compatible**: `prompt_tokens_details.cached_tokens` (or
  `cache_read_input_tokens`) is recorded when the provider reports it.
- **llama.cpp**: `cache_prompt` avoids re-evaluating the prefix server-side.

Cached tokens are displayed in the web UI (usage popover, chat cost lines) and
the TUI.

## Transport details

Two transports are implemented in `src/llm.py`:

- **OpenAI-compatible** (`complete`): `POST /chat/completions` with a
  `messages` array. Used by `zen`, `openrouter`, `openai`, `local`.
- **Anthropic Messages** (`call_anthropic_history` / streaming variant): native
  `POST /messages`. Used only when `provider == "anthropic"`.

The Anthropic streaming transport uses native SSE (`"stream": true`).

## Adding a provider

1. Register it in `src/config.py` under `PROVIDERS`:

   ```python
   PROVIDERS = {
       ...,
       "myprovider": {
           "base_url": os.environ.get("MYPROVIDER_BASE_URL", "https://api.myprovider.com/v1"),
           "api_key":  os.environ.get("MYPROVIDER_API_KEY", ""),
       },
   }
   ```

2. Decide which transport to use. If the provider is OpenAI-compatible, no
   further changes are needed. If it requires a custom wire format, add a
   transport function in `src/llm.py` following the existing patterns.

3. Reference it in your `.env` or team YAML:
   ```
   CODER_MODEL=myprovider/my-model-id
   ```

## Capabilities metadata

`src/providers.py` tracks optional per-provider metadata:

- `has_reasoning(provider_name, model)`: returns `True` for known reasoning
  models (e.g. DeepSeek-R1, o-series). Used to set higher `max_tokens` budgets.
- `resolve_name(ref)`: splits `"provider/model"` into `(provider, model)` and
  validates the provider is registered.
- `caps(provider_name, model)`: returns the capability dict for a
  provider/model pair (`reasoning`, `context_window`, `max_tokens`).

## Rate limits and retries

`src/governor.py` wraps every LLM call with:

- **Rate limiting** (per-provider RPM cap via `aiolimiter`): set
  `RPM_<PROVIDER_NAME>=60` (default: 60 RPM). Set to `0` to disable.
- **Retry with backoff**: 429 and 503 responses are retried up to
  `RATE_LIMIT_MAX_RETRIES` times with exponential backoff honouring
  `Retry-After`.
- **Fallback chain**: when retries are exhausted, the governor tries each
  provider in the configured fallback list (`CODER_FALLBACK`,
  `REVIEWER_FALLBACK`, etc.) before giving up.

When `AUTO_LOW_QUOTA=1` (default) and a 429 exhausts all retries, the process
switches to low-quota mode automatically (see `src/quota.py`): coder and
consensus use `LOW_QUOTA_MODEL`, the panel shrinks to `LOW_QUOTA_PANEL_SIZE`
reviewers. **The Lead is never downgraded.**

## Per-reviewer max_tokens

A reviewer that uses a reasoning model (e.g. Gemini thinking, DeepSeek-R1) can
be given a larger budget in `REVIEW_PANEL`:

```
REVIEW_PANEL=r1:zen/deepseek-r1-0528:32000,fast:zen/deepseek-v3-0324
```

Format: `name:provider/model` or `name:provider/model:max_tokens`.
