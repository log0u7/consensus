# Providers

All LLM calls go through `src/llm.py` via raw `httpx` - no SDK. Providers are
registered in `src/config.py` and resolved by name at runtime.

## Model reference format

Models are referenced everywhere as `provider/model-id`:

```
zen/deepseek-r1-0528
anthropic/claude-opus-latest
openai/gpt-4o
local/qwen2.5-coder
```

This format is used in team YAMLs, environment variables (`CODER_MODEL`,
`LEAD_MODEL`, `REVIEW_PANEL`), and API responses.

## Built-in providers

| Name        | Transport          | Endpoint env var        | Default base URL                    |
|-------------|--------------------|-------------------------|-------------------------------------|
| `zen`       | OpenAI-compatible  | `ZEN_BASE_URL`          | `https://opencode.ai/zen/v1`        |
| `openai`    | OpenAI-compatible  | `OPENAI_BASE_URL`       | `https://api.openai.com/v1`         |
| `anthropic` | Anthropic Messages | `ANTHROPIC_BASE_URL`    | `https://api.anthropic.com/v1`      |
| `local`     | OpenAI-compatible  | `LOCAL_BASE_URL`        | `http://localhost:11434/v1` (Ollama) |

The `zen` provider is free (no credit card required) at opencode.ai. It proxies
a wide range of open-weight and frontier models.

### OpenRouter

OpenRouter is reachable via the `zen` provider or by configuring a custom
`openai`-compatible provider pointing at `https://openrouter.ai/api/v1`. The
`/responses` path is used (not `/messages`).

## Transport details

Two transports are implemented in `src/llm.py`:

- **OpenAI-compatible** (`complete`): `POST /chat/completions` with a
  `messages` array. Used by `zen`, `openai`, `local`.
- **Anthropic Messages** (`call_anthropic_history` / streaming variant): native
  `POST /messages` with `system` as a top-level parameter. Used only when
  `provider == "anthropic"`.

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

- `has_reasoning(provider)`: returns `True` for known reasoning models
  (e.g. DeepSeek-R1, o-series). Used to set higher `max_tokens` budgets.
- `resolve_name(ref)`: splits `"provider/model"` into `(provider, model)` and
  validates the provider is registered.
- `provider_caps(provider)`: returns the capability dict for a provider.

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
