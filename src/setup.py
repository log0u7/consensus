"""Interactive provider setup wizard and offline configuration checker.

`make setup` (python -m src.setup):
  1. Import API keys already saved by opencode (auth.json) when present.
  2. Prompt (getpass) for the missing ones - Enter skips an optional provider.
  3. Probe each configured provider: GET /models (reachability + model list)
     and a 1-token completion (key validity; skipped for local).
  4. Suggest a routing (coder / consensus / lead / chat / review panel) from
     the models the providers actually expose, preferring free models.
  5. Write the collected values into .env (backup in .env.bak), merging with
     an existing file. Keys are never printed or logged.

`python -m src.setup --auto`: non-interactive generation of a tuned test
config from opencode keys / existing file / env vars (free-model routing,
3-reviewer panel, PG password). Exit 1 when no provider key can be found.
Add `--paid` (make setup-paid) to route to normal paid OpenRouter models
instead: same flow, real testing without free-pool 429 noise.

`python -m src.setup --check`: non-interactive probe of the currently
configured environment (env vars), exit 1 when nothing is usable.

No SDK: plain httpx against the same wire formats llm.py uses.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

ENV_PATH = Path(os.environ.get("CONSENSUS_ENV_PATH", ".env"))
ENV_BACKUP_PATH = ENV_PATH.with_name(ENV_PATH.name + ".bak")

# opencode stores provider credentials here (Linux first, XDG fallback).
_OPENCODE_AUTH_PATHS = [
    Path.home() / ".local/share/opencode/auth.json",
    Path.home() / ".config/opencode/auth.json",
]

DEFAULT_LOCAL_URL = "http://127.0.0.1:8080"  # llama.cpp server default

# Wizard scope. "prompt" providers are asked for interactively; "import"
# providers are only picked up from opencode's auth.json / env.
SPECS: dict[str, dict[str, Any]] = {
    "zen": {
        "key_env": "ZEN_API_KEY",
        "url_env": "ZEN_BASE_URL",
        "default_url": "https://opencode.ai/zen/v1",
        "auth": "bearer",
        "mode": "prompt",
        "hint": "free key at opencode.ai/zen",
    },
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "url_env": "OPENROUTER_BASE_URL",
        "default_url": "https://openrouter.ai/api/v1",
        "auth": "bearer",
        "mode": "prompt",
        "hint": "hundreds of models, many free",
    },
    "anthropic": {
        "key_env": "ANTHROPIC_API_KEY",
        "url_env": "ANTHROPIC_BASE_URL",
        "default_url": "https://api.anthropic.com/v1",
        "auth": "x-api-key",
        "mode": "prompt",
        "hint": "optional - native prompt caching",
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "url_env": "OPENAI_BASE_URL",
        "default_url": "https://api.openai.com/v1",
        "auth": "bearer",
        "mode": "import",
        "hint": "or OPENAI_BASE_URL for any OpenAI-compatible endpoint",
    },
    "local": {
        "key_env": "LOCAL_API_KEY",
        "url_env": "LOCAL_BASE_URL",
        "default_url": DEFAULT_LOCAL_URL,
        "auth": "bearer",
        "mode": "local",
        "hint": "llama.cpp / Ollama / vLLM, no key needed",
    },
}


# ---------------------------------------------------------------------------
# Key import from opencode
# ---------------------------------------------------------------------------


def load_opencode_auth(paths: list[Path] | None = None) -> dict[str, str]:
    """Extract API keys from opencode's auth.json: {provider: key}.

    Only static "api" credentials are imported; OAuth entries without a
    literal key are skipped. Never logs the keys.
    """
    for path in paths or _OPENCODE_AUTH_PATHS:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("auth.json unreadable at %s: %s", path, exc)
            continue
        out: dict[str, str] = {}
        for provider, entry in data.items():
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if isinstance(key, dict):  # some gateways nest {primary: ...}
                key = key.get("primary") or key.get("api_key")
            if isinstance(key, str) and key.strip():
                out[provider] = key.strip()
        return out
    return {}


# ---------------------------------------------------------------------------
# Probing (sync httpx; transport injectable for tests)
# ---------------------------------------------------------------------------


def _auth_headers(auth: str, key: str) -> dict[str, str]:
    if auth == "x-api-key":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def _parse_models(payload: object) -> list[str]:
    """Extract model ids from an OpenAI-style /models response (tolerant)."""
    items: list[Any] = []
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        items = payload
    ids: list[str] = []
    for it in items:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict) and isinstance(it.get("id"), str):
            ids.append(it["id"])
    return ids


def probe_provider(
    name: str,
    base_url: str,
    key: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 8.0,
) -> dict:
    """GET /models: reachability + model ids. Never raises."""
    spec = SPECS[name]
    try:
        client = httpx.Client(
            transport=transport,
            headers=_auth_headers(spec["auth"], key),
            timeout=httpx.Timeout(timeout, connect=timeout),
        )
        r = client.get(base_url.rstrip("/") + "/models")
        client.close()
        models = _parse_models(r.json()) if r.status_code == 200 else []
        return {
            "reachable": r.status_code < 500,
            "status": r.status_code,
            "models": models,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - probes must not crash the wizard
        return {"reachable": False, "status": None, "models": [], "error": type(exc).__name__}


def check_key(
    name: str,
    base_url: str,
    key: str,
    model: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 15.0,
) -> dict:
    """1-token completion: real key + model validation. Never raises.

    Skipped for local (no key semantics) - callers pass model="" to skip.
    """
    if not model:
        return {"valid": None, "error": "skipped"}
    spec = SPECS[name]
    headers = _auth_headers(spec["auth"], key)
    try:
        if spec["auth"] == "x-api-key":
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
            path = "/messages"
        else:
            payload = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
            path = "/chat/completions"
        client = httpx.Client(transport=transport, headers=headers, timeout=timeout)
        r = client.post(base_url.rstrip("/") + path, json=payload)
        client.close()
        # 400 can mean "model not found" (key fine) - treat auth errors as invalid.
        if r.status_code == 402:
            return {
                "valid": False,
                "error": "402 Payment Required - add credits: openrouter.ai/settings/credits",
            }
        if r.status_code in (401, 403):
            return {"valid": False, "error": f"HTTP {r.status_code}"}
        return {
            "valid": r.status_code < 500,
            "error": None if r.status_code < 500 else f"HTTP {r.status_code}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"valid": None, "error": type(exc).__name__}


# ---------------------------------------------------------------------------
# Routing suggestion
# ---------------------------------------------------------------------------


def suggest_routing(models: dict[str, list[str]], free: set[str]) -> dict:
    """Pick coder / consensus / lead / chat / panel from available model ids.

    Preference order:
      coder      - zen "big-pickle" (free coding model), else any free model,
                   else the first model of the first provider with models.
      consensus  - a fast free reasoning model (glm flash style), else first
                   free, else coder's model.
      lead       - same as consensus (never downgraded, always full-fat).
      chat       - first local model (free exploration), else lead's model.
      panel      - up to 3 *distinct* free models, coder's included.
    """
    pick_order = ["zen", "openrouter", "openai", "anthropic", "local"]

    def _first(pred, providers=None) -> tuple[str, str] | None:
        for prov in providers or pick_order:
            for mid in models.get(prov, []):
                if pred(mid):
                    return prov, mid
        return None

    def _ref(hit: tuple[str, str] | None, fallback: str = "") -> str:
        return f"{hit[0]}/{hit[1]}" if hit else fallback

    coder = _first(lambda m: "big-pickle" in m) or _first(lambda m: m in free)
    coder = coder or _first(lambda m: True)
    coder_ref = _ref(coder)

    glm = _first(lambda m: "glm" in m.lower() and "flash" in m.lower())
    reasoning = glm or _first(lambda m: m in free and (not coder or m != coder[1]))
    reasoning_ref = _ref(reasoning, coder_ref)

    local_hit = _first(lambda m: True, providers=["local"])
    chat_ref = _ref(local_hit, reasoning_ref)

    panel: list[dict] = []
    seen: set[str] = set()
    for prov in pick_order:
        for mid in models.get(prov, []):
            key = f"{prov}/{mid}"
            if key in seen or mid not in free:
                continue
            seen.add(key)
            panel.append({"name": f"reviewer-{len(panel) + 1}", "provider": prov, "model": mid})
            if len(panel) == 3:
                break
        if len(panel) == 3:
            break
    if not panel and coder:
        panel = [{"name": "reviewer-1", "provider": coder[0], "model": coder[1]}]

    return {
        "CODER_MODEL": coder_ref,
        "CONSENSUS_MODEL": reasoning_ref,
        "LEAD_MODEL": reasoning_ref,
        "CHAT_MODEL": chat_ref if local_hit else "",
        # config._parse_panel format: "name:provider/model[:max_tokens]"
        "REVIEW_PANEL": ",".join(f"{m['name']}:{m['provider']}/{m['model']}" for m in panel),
    }


def _free_model_ids(catalog: dict[str, dict]) -> set[str]:
    """Model-id suffixes whose prompt AND completion price are exactly 0."""
    out: set[str] = set()
    for model_id, row in catalog.items():
        if row.get("prompt") == 0.0 and row.get("completion") == 0.0:
            out.add(model_id.split("/", 1)[-1])
    return out


# ---------------------------------------------------------------------------
# .env writing
# ---------------------------------------------------------------------------

_MINIMAL_TEMPLATE = """# Consensus configuration (generated by `make setup`).
# Full annotated template: see env.example in the repository.
"""


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    return values


def merge_env(existing: str, updates: dict[str, str]) -> str:
    """Merge updates into dotenv text: existing lines are replaced in place,
    new keys are appended under a generated section. Order is preserved."""
    lines = existing.splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = (
            stripped.partition("=")[0].strip()
            if "=" in stripped and not stripped.startswith("#")
            else None
        )
        if key and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        out.append("")
        out.append("# --- added by make setup ---")
        out.extend(f"{k}={v}" for k, v in remaining.items())
    return "\n".join(out).rstrip("\n") + "\n"


def write_env(updates: dict[str, str], path: Path = ENV_PATH) -> Path:
    """Write updates into the .env file, backing up any previous version."""
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        base = path.read_text()
    else:
        base = _MINIMAL_TEMPLATE
    merged = merge_env(base, updates)
    path.write_text(merged)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# Non-interactive auto mode (--auto): tuned free-tier test config
# ---------------------------------------------------------------------------

# Free models verified live on 2026-08. Zen block used when a Zen key exists,
# OpenRouter block otherwise (free ids carry a ":free" suffix). Notes:
# - every reviewer gets an explicit :32000 budget (panel format
#   "name:provider/model:max_tokens"): reasoning models can burn the default
#   8k on thinking alone and answer content=null;
# - governor fallbacks are PROVIDER names (not provider/model refs) so they
#   are useless within a single-provider test setup: retries (below) plus the
#   panel's ok=False resilience cover free-pool 429 bursts instead;
# - the busiest free pool (z-ai/glm-5.2:free) is only used for the Lead:
#   spreading roles over distinct pools avoids stacking 429s on one model.
_AUTO_ZEN = {
    "CODER_MODEL": "zen/big-pickle",
    "CONSENSUS_MODEL": "zen/deepseek-v4-flash-free",
    "LEAD_MODEL": "zen/deepseek-v4-flash-free",
    "REVIEW_PANEL": (
        "reviewer-1:zen/big-pickle:32000,"
        "reviewer-2:zen/deepseek-v4-flash-free:32000,"
        "reviewer-3:zen/mimo-v2.5-free:32000"
    ),
}
_AUTO_OPENROUTER = {
    "CODER_MODEL": "openrouter/cohere/north-mini-code:free",
    "CONSENSUS_MODEL": "openrouter/minimax/minimax-m3:free",
    "LEAD_MODEL": "openrouter/z-ai/glm-5.2:free",
    "REVIEW_PANEL": (
        "reviewer-1:openrouter/minimax/minimax-m3:free:32000,"
        "reviewer-2:openrouter/z-ai/glm-5.2:free:32000,"
        "reviewer-3:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free:32000"
    ),
}
# Paid routing (make setup-paid): normal OpenRouter models, one vendor per
# role, for real testing without free-pool 429 noise. Prices verified in the
# live catalog (2026-08); indicative cost per small test run: ~$0.03-0.10.
_AUTO_OPENROUTER_PAID = {
    "CODER_MODEL": "openrouter/openai/gpt-5.4-mini",
    "CONSENSUS_MODEL": "openrouter/google/gemini-3.5-flash-lite",
    "LEAD_MODEL": "openrouter/openai/gpt-5.3-codex",
    "REVIEW_PANEL": (
        "reviewer-1:openrouter/google/gemini-3.5-flash-lite:32000,"
        "reviewer-2:openrouter/openai/gpt-5.4-mini:32000,"
        "reviewer-3:openrouter/deepseek/deepseek-v4-flash:32000"
    ),
}
# Free pools answer 429 in bursts: 3 attempts with the growing window
# (exponential backoff, Retry-After honored) is the tolerated trade-off.
_AUTO_RATE_LIMIT_MAX_RETRIES = "3"


def build_auto_updates(
    existing: dict[str, str],
    imported: dict[str, str],
    environ: dict[str, str],
    paid: bool = False,
) -> dict[str, str]:
    """Build the .env updates for --auto. Pure function (testable).

    Key priority per provider: existing file > shell env > opencode auth.json.
    Routing: the paid OpenRouter block when `paid` is set (opt-in real
    testing), else the free blocks - Zen when a Zen key exists, else the
    OpenRouter free demo. Merge replaces routing keys in place. Local
    provider is only included when already configured. PG_PASSWORD (required
    by docker-compose) is kept when present, else generated - never printed.
    """
    updates: dict[str, str] = {}

    for name, spec in SPECS.items():
        if name == "local":
            url = existing.get(spec["url_env"]) or environ.get(spec["url_env"], "")
            if not url:
                continue
            updates[spec["url_env"]] = url
            key = existing.get(spec["key_env"]) or environ.get(spec["key_env"], "") or "ollama"
            updates[spec["key_env"]] = key
            updates.setdefault("PROVIDER_EXTRA_PAYLOAD_LOCAL", json.dumps({"cache_prompt": True}))
            continue
        key = (
            existing.get(spec["key_env"])
            or environ.get(spec["key_env"], "")
            or imported.get(name, "")
        )
        if key:
            updates[spec["key_env"]] = key

    if not any(k.endswith("_API_KEY") for k in updates):
        return {}

    # Routing: paid OpenRouter block when explicitly requested; else Zen when
    # a Zen key is available, else the OpenRouter free demo block. With
    # neither free provider, leave routing alone (config defaults target zen;
    # a paid setup must be chosen deliberately).
    if paid and "OPENROUTER_API_KEY" in updates:
        updates.update(_AUTO_OPENROUTER_PAID)
    elif "ZEN_API_KEY" in updates:
        updates.update(_AUTO_ZEN)
    elif "OPENROUTER_API_KEY" in updates:
        updates.update(_AUTO_OPENROUTER)
    if "CODER_MODEL" in updates:
        # Governor fallbacks take bare PROVIDER names, not provider/model refs
        # (the model id is kept across the fallback chain). The single-provider
        # test setup has no useful fallback: clear stale refs from earlier
        # configs so the governor never treats a model ref as a provider.
        for k in ("CODER_FALLBACK", "REVIEWER_FALLBACK", "CONSENSUS_FALLBACK", "LEAD_FALLBACK"):
            updates[k] = ""
        updates["RATE_LIMIT_MAX_RETRIES"] = _AUTO_RATE_LIMIT_MAX_RETRIES

    pg_password = (
        existing.get("PG_PASSWORD") or environ.get("PG_PASSWORD", "") or secrets.token_hex(12)
    )
    updates["PG_PASSWORD"] = pg_password
    return updates


def run_auto(paid: bool = False) -> int:
    """Non-interactive: gather keys, write a tuned test config, probe.
    `paid=True` routes to normal paid OpenRouter models (make setup-paid)."""
    existing: dict[str, str] = {}
    if ENV_PATH.is_file():
        existing = _parse_env(ENV_PATH.read_text())
    imported = load_opencode_auth()
    updates = build_auto_updates(existing, imported, dict(os.environ), paid=paid)

    if not updates:
        print(
            "No provider key found (existing config, shell env, or opencode "
            "auth.json). Run `make setup` interactively or export a key."
        )
        return 1

    providers = sorted(spec_name for spec_name, spec in SPECS.items() if spec["key_env"] in updates)
    path = write_env(updates)
    print(f"Config written: {path} (backup: {path}.bak when it existed)")
    print(f"Providers configured: {', '.join(providers)}")

    # Pre-seed the pricing catalog so the first run already shows costs.
    try:
        from . import pricing

        if pricing.refresh_sync():
            print(f"Pricing catalog cached: {pricing.stats()['entries']} models")
    except Exception as exc:  # noqa: BLE001 - optional, never fatal
        log.debug("pricing pre-seed skipped: %s", exc)

    # Probe what we just configured (keys stay masked).
    print(f"\n{'provider':<12} {'reachable':<10} {'models':<8} base")
    for name in providers:
        spec = SPECS[name]
        url = updates.get(spec["url_env"], "") or spec["default_url"]
        r = probe_provider(name, url, updates[spec["key_env"]])
        state = f"{len(r['models'])}" if r["reachable"] else f"unreachable ({r['error']})"
        print(f"{name:<12} {str(r['reachable']):<10} {state:<8} {url}")

    print(
        f"\nRouting ({'paid OpenRouter' if paid else 'free-tier'} test config):\n"
        f"  CODER_MODEL      {updates.get('CODER_MODEL', '(config default)')}\n"
        f"  CONSENSUS_MODEL  {updates.get('CONSENSUS_MODEL', '(config default)')}\n"
        f"  LEAD_MODEL       {updates.get('LEAD_MODEL', '(config default)')}\n"
        f"  REVIEW_PANEL     {updates.get('REVIEW_PANEL', '(config default)')}\n"
    )

    # Validate every routing model id with a 1-token call: catalog ids can go
    # stale or carry internal aliases ("~vendor/...") that 400 at run time.
    refs: dict[str, tuple[str, str]] = {}
    for key in ("CODER_MODEL", "CONSENSUS_MODEL", "LEAD_MODEL"):
        ref = updates.get(key, "")
        if "/" in ref:
            prov, model = ref.split("/", 1)
            refs[ref] = (prov, model)
    for entry in (updates.get("REVIEW_PANEL") or "").split(","):
        parts = entry.strip().split(":")
        if len(parts) >= 2 and "/" in parts[1]:
            prov, model = parts[1].split("/", 1)
            refs[parts[1]] = (prov, model)
    if refs:
        print("Validating routing models (1 token each)...")
        for ref, (prov, model) in sorted(refs.items()):
            pspec: dict[str, Any] | None = SPECS.get(prov)
            if not pspec or pspec["key_env"] not in updates or prov == "local":
                continue
            v = check_key(
                prov,
                os.environ.get(pspec["url_env"], "") or pspec["default_url"],
                updates[pspec["key_env"]],
                model,
            )
            state = {True: "ok", False: "INVALID", None: "skipped"}[v["valid"]]
            print(f"  {ref:55s} {state}")
            if v["valid"] is False:
                print(f"    -> fix {key} / REVIEW_PANEL before running (see env.example)")

    print("\nNext: make up   (UI: http://localhost:8800)")
    return 0


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------


def _prompt_key(label: str, current: str, optional: bool) -> str:
    if current:
        masked = current[:6] + "..." + current[-4:] if len(current) > 12 else "***"
        ans = input(f"{label}: key {masked} found. Replace? [y/N] ")
        if ans.strip().lower() != "y":
            return current
        ans = getpass.getpass(f"New {label} API key: ")
    else:
        hint = "Enter = skip" if optional else "Enter = skip"
        ans = getpass.getpass(f"{label} API key ({hint}): ")
    return ans.strip()


def run_interactive() -> int:
    print("Consensus setup - configure providers, routing and write .env\n")
    imported = load_opencode_auth()
    if imported:
        print(f"Found opencode credentials for: {', '.join(sorted(imported))}")

    keys: dict[str, str] = {}
    urls: dict[str, str] = {}
    for name, spec in SPECS.items():
        env_key = spec["key_env"]
        current = os.environ.get(env_key, "") or (
            imported.get(name, "") if spec["mode"] != "local" else ""
        )
        if spec["mode"] == "local":
            url = input(f"Local server URL [{spec['default_url']} or Enter = no local]: ").strip()
            if not url:
                continue
            urls[name] = url
            key = _prompt_key("Local", os.environ.get(env_key, ""), optional=True)
            keys[name] = key or "ollama"
            continue
        key = _prompt_key(
            name, current, optional=(spec["mode"] == "prompt" and name == "anthropic")
        )
        if key:
            keys[name] = key
        elif current:
            keys[name] = current

    if not keys and not urls:
        print("No provider configured. At least one is required.")
        return 1

    # Probe + collect models.
    results: dict[str, dict] = {}
    for name in list(keys) + [n for n in urls if n not in keys]:
        base = (
            urls.get(name)
            or os.environ.get(SPECS[name]["url_env"], "")
            or SPECS[name]["default_url"]
        )
        if name == "local":
            base = urls.get("local", os.environ.get("LOCAL_BASE_URL", "") or "")
            if not base:
                continue
        print(f"Probing {name} at {base} ...")
        results[name] = {"base": base, **probe_provider(name, base, keys.get(name, "ollama"))}
        r = results[name]
        status = (
            f"HTTP {r['status']}, {len(r['models'])} models"
            if r["reachable"]
            else f"unreachable ({r['error']})"
        )
        print(f"  -> {status}")

    # Free-model knowledge (also seeds pricing.db for the app). Optional.
    free: set[str] = set()
    try:
        from . import pricing

        if pricing.refresh_sync():
            free = _free_model_ids(pricing._catalog._rows)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        log.debug("pricing fetch skipped: %s", exc)

    # Key validation: 1-token completion on the first model (not local).
    for name, r in results.items():
        if name == "local" or not r["reachable"] or not r["models"]:
            continue
        model = r["models"][0]
        v = check_key(name, r["base"], keys[name], model)
        r["key_valid"] = v["valid"]
        r["key_note"] = f"checked with {model}" if v["valid"] else (v["error"] or "invalid")
        print(f"Key {name}: {'valid' if v['valid'] else 'INVALID'} ({r['key_note']})")

    # Routing suggestion from what actually answered.
    models = {name: r["models"] for name, r in results.items() if r["reachable"]}
    routing = suggest_routing(models, free) if models else {}
    if routing:
        print("\nSuggested routing:")
        for k in ("CODER_MODEL", "CONSENSUS_MODEL", "LEAD_MODEL", "CHAT_MODEL", "REVIEW_PANEL"):
            if routing.get(k):
                print(f"  {k:16s} {routing[k]}")

    updates: dict[str, str] = {}
    for name, key in keys.items():
        updates[SPECS[name]["key_env"]] = key
    for name, url in urls.items():
        updates[SPECS[name]["url_env"]] = url
    if "local" in urls:
        updates["PROVIDER_EXTRA_PAYLOAD_LOCAL"] = json.dumps({"cache_prompt": True})
    updates.update({k: v for k, v in routing.items() if v})
    if not routing.get("CHAT_MODEL"):
        updates.pop("CHAT_MODEL", None)  # empty default: fall back to LEAD_MODEL

    if updates and input("\nWrite these values to .env? [Y/n] ").strip().lower() in (
        "",
        "y",
        "yes",
    ):
        path = write_env(updates)
        print(f"Written: {path} (backup: {path}.bak when it existed)")
    else:
        print("Nothing written.")

    print(
        "\nNext steps:\n"
        "  make up          # start the stack\n"
        '  make run SPEC="write a Python class that does X"\n'
        "  Routing envs: CODER_MODEL, REVIEW_PANEL, CONSENSUS_MODEL, LEAD_MODEL,\n"
        "                CHAT_MODEL, LEAD_FALLBACK=local"
    )
    return 0


# ---------------------------------------------------------------------------
# --check: offline validation of the current environment
# ---------------------------------------------------------------------------


def run_check(timeout: float = 8.0, transport: httpx.BaseTransport | None = None) -> int:
    """Probe every provider configured via env vars. Prints a table, returns
    0 when at least one provider is reachable, 1 otherwise."""
    configured: list[tuple[str, str, str]] = []  # (name, base_url, key)
    for name, spec in SPECS.items():
        key = os.environ.get(spec["key_env"], "")
        url = os.environ.get(spec["url_env"], "") or spec["default_url"]
        if name == "local":
            url = os.environ.get("LOCAL_BASE_URL", "")
            if not url:
                continue
            configured.append((name, url, key or "ollama"))
        elif key:
            configured.append((name, url, key))

    if not configured:
        print("No provider configured (no *_API_KEY env var, no LOCAL_BASE_URL).")
        return 1

    print(f"{'provider':<12} {'reachable':<10} {'key':<10} {'models':<8} base")
    any_ok = False
    for name, url, key in configured:
        r = probe_provider(name, url, key, transport=transport, timeout=timeout)
        any_ok = any_ok or r["reachable"]
        key_state = "present" if key else "-"
        if r["reachable"] and name != "local" and r["models"]:
            v = check_key(name, url, key, r["models"][0], transport=transport)
            key_state = "valid" if v["valid"] else ("invalid" if v["valid"] is False else "present")
        print(f"{name:<12} {str(r['reachable']):<10} {key_state:<10} {len(r['models']):<8} {url}")
    return 0 if any_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consensus provider setup")
    parser.add_argument(
        "--auto", action="store_true", help="write a tuned test config non-interactively"
    )
    parser.add_argument(
        "--paid",
        action="store_true",
        help="with --auto: route to normal paid OpenRouter models (real testing)",
    )
    parser.add_argument("--check", action="store_true", help="probe configured providers and exit")
    args = parser.parse_args(argv)
    if args.auto:
        return run_auto(paid=args.paid)
    if args.check:
        return run_check()
    return run_interactive()


if __name__ == "__main__":
    sys.exit(main())
