"""Tests for src/setup.py: auth import, env merging, routing suggestion and
the offline --check probe (httpx.MockTransport). Interactive flows are not
tested (getpass/input); only pure and probe functions are.
"""

import json

import httpx
from src import setup

# ---------------------------------------------------------------------------
# load_opencode_auth
# ---------------------------------------------------------------------------


def test_load_opencode_auth_imports_api_keys(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(
        json.dumps(
            {
                "zen": {"type": "api", "key": "sk-zen-123"},
                "openai": {"type": "oauth", "access": "no-key-here"},
                "nested": {"type": "api", "key": {"primary": "sk-nested"}},
                "broken": "not-a-dict",
            }
        )
    )
    auth = setup.load_opencode_auth([p])
    assert auth == {"zen": "sk-zen-123", "nested": "sk-nested"}


def test_load_opencode_auth_missing_files(tmp_path):
    assert setup.load_opencode_auth([tmp_path / "nope.json"]) == {}


def test_load_opencode_auth_corrupt_json(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("{not json")
    assert setup.load_opencode_auth([p]) == {}


# ---------------------------------------------------------------------------
# merge_env / _parse_env
# ---------------------------------------------------------------------------


def test_merge_env_replaces_in_place_and_appends():
    base = "A=1\n# comment\nB=2\n# tail\n"
    merged = setup.merge_env(base, {"B": "3", "C": "4"})
    lines = merged.splitlines()
    assert lines[0] == "A=1"
    assert "# comment" in lines
    assert "B=3" in lines
    assert "C=4" in lines
    assert lines.index("B=3") < lines.index("# tail")


def test_merge_env_empty_base():
    merged = setup.merge_env("", {"ZEN_API_KEY": "x"})
    assert "ZEN_API_KEY=x" in merged


def test_write_env_creates_backup_and_merges(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("KEEP=1\nCODER_MODEL=old\n")
    monkeypatch.setattr(setup, "ENV_PATH", env)
    setup.write_env({"CODER_MODEL": "zen/big-pickle", "NEW_KEY": "v"}, path=env)
    assert env.read_text().startswith("KEEP=1")
    assert "CODER_MODEL=zen/big-pickle" in env.read_text()
    assert "NEW_KEY=v" in env.read_text()
    backup = tmp_path / ".env.bak"
    assert backup.is_file()
    assert "CODER_MODEL=old" in backup.read_text()
    # 0600 on the written file
    assert (env.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# suggest_routing
# ---------------------------------------------------------------------------


def _models():
    return {
        "zen": ["big-pickle", "glm-4.5-flash", "qwen3-coder-30b"],
        "openrouter": ["deepseek/deepseek-r1", "z-ai/glm-4.5-flash:free"],
        "local": ["qwen3-8b-q4_k_m"],
    }


def test_suggest_routing_prefers_big_pickle_glm_and_local_chat():
    free = {"big-pickle", "glm-4.5-flash", "z-ai/glm-4.5-flash:free"}
    r = setup.suggest_routing(_models(), free)
    assert r["CODER_MODEL"] == "zen/big-pickle"
    assert r["CONSENSUS_MODEL"] == "zen/glm-4.5-flash"
    assert r["LEAD_MODEL"] == "zen/glm-4.5-flash"
    assert r["CHAT_MODEL"] == "local/qwen3-8b-q4_k_m"
    panel = r["REVIEW_PANEL"].split(",")
    assert len(panel) == 3
    assert panel[0] == "reviewer-1:zen/big-pickle"


def test_suggest_routing_panel_emits_config_parseable_entries():
    """Generated REVIEW_PANEL entries must survive config._parse_panel."""
    free = {"big-pickle", "glm-4.5-flash"}
    r = setup.suggest_routing(_models(), free)
    import os

    os.environ["REVIEW_PANEL"] = r["REVIEW_PANEL"]
    try:
        from src.config import _parse_panel

        panel = _parse_panel(r["REVIEW_PANEL"])
        # free set only covers the two zen ids here; the :free openrouter id
        # is not in `free` so it is not picked.
        assert [m["provider"] for m in panel] == ["zen", "zen"]
        assert panel[0]["model"] == "big-pickle"
    finally:
        del os.environ["REVIEW_PANEL"]


def test_suggest_routing_without_free_models_falls_back():
    r = setup.suggest_routing({"local": ["qwen3-8b"]}, set())
    assert r["CODER_MODEL"] == "local/qwen3-8b"
    assert r["CONSENSUS_MODEL"] == "local/qwen3-8b"
    assert r["CHAT_MODEL"] == "local/qwen3-8b"
    # No free models anywhere: panel falls back to the coder's model.
    assert r["REVIEW_PANEL"] == "reviewer-1:local/qwen3-8b"


def test_suggest_routing_no_models_at_all():
    r = setup.suggest_routing({}, set())
    assert r["CODER_MODEL"] == ""
    assert r["REVIEW_PANEL"] == ""


# ---------------------------------------------------------------------------
# run_check (offline via MockTransport)
# ---------------------------------------------------------------------------


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "glm-4.5-flash"}, {"id": "big-pickle"}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


def test_run_check_ok(monkeypatch):
    monkeypatch.setenv("ZEN_API_KEY", "sk-test")
    monkeypatch.setattr(setup, "SPECS", {k: v for k, v in setup.SPECS.items() if k == "zen"})
    rc = setup.run_check(transport=httpx.MockTransport(_ok_handler))
    assert rc == 0


def test_run_check_all_down(monkeypatch):
    monkeypatch.setenv("ZEN_API_KEY", "sk-test")
    monkeypatch.setattr(setup, "SPECS", {k: v for k, v in setup.SPECS.items() if k == "zen"})
    rc = setup.run_check(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    assert rc == 1


def test_run_check_nothing_configured(monkeypatch):
    for spec in setup.SPECS.values():
        monkeypatch.delenv(spec["key_env"], raising=False)
        monkeypatch.delenv(spec["url_env"], raising=False)
    monkeypatch.delenv("LOCAL_BASE_URL", raising=False)
    monkeypatch.setattr(setup, "SPECS", {k: v for k, v in setup.SPECS.items() if k == "zen"})
    assert setup.run_check() == 1


def test_check_key_auth_failure_is_invalid(monkeypatch):
    monkeypatch.setattr(setup, "SPECS", setup.SPECS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "nope"})

    r = setup.check_key("zen", "https://mock", "bad", "m", transport=httpx.MockTransport(handler))
    assert r["valid"] is False


def test_check_key_skips_empty_model():
    r = setup.check_key("local", "https://mock", "ollama", "")
    assert r == {"valid": None, "error": "skipped"}


# ---------------------------------------------------------------------------
# build_auto_updates (--auto mode)
# ---------------------------------------------------------------------------


def test_build_auto_updates_key_priority_env_over_auth(tmp_path):
    imported = {"zen": "from-auth"}
    updates = setup.build_auto_updates({}, imported, {"ZEN_API_KEY": "from-env"})
    assert updates["ZEN_API_KEY"] == "from-env"


def test_build_auto_updates_existing_wins(tmp_path):
    existing = {"ZEN_API_KEY": "from-file"}
    updates = setup.build_auto_updates(existing, {"zen": "from-auth"}, {"ZEN_API_KEY": "from-env"})
    assert updates["ZEN_API_KEY"] == "from-file"


def test_build_auto_updates_imports_from_opencode_auth():
    updates = setup.build_auto_updates({}, {"zen": "sk-zen-auth"}, {})
    assert updates["ZEN_API_KEY"] == "sk-zen-auth"


def test_build_auto_updates_free_routing_and_panel():
    updates = setup.build_auto_updates({}, {"zen": "sk"}, {})
    assert updates["CODER_MODEL"] == "zen/big-pickle"
    assert updates["CONSENSUS_MODEL"] == "zen/deepseek-v4-flash-free"
    assert updates["LEAD_MODEL"] == "zen/deepseek-v4-flash-free"
    panel = updates["REVIEW_PANEL"].split(",")
    assert len(panel) == 3
    models = [p.split(":")[1] for p in panel]
    assert len(set(models)) == 3  # distinct reviewers
    assert all(m.startswith("zen/") for m in models)
    assert "CHAT_MODEL" not in updates  # falls back to LEAD_MODEL


def test_build_auto_updates_paid_routing(monkeypatch):
    """--paid routes to normal OpenRouter models; free stays the default."""
    updates = setup.build_auto_updates({}, {"openrouter": "sk-or"}, {}, paid=True)
    assert updates["CODER_MODEL"] == "openrouter/openai/gpt-5.4-mini"
    assert updates["CONSENSUS_MODEL"] == "openrouter/google/gemini-3.5-flash-lite"
    assert updates["LEAD_MODEL"] == "openrouter/openai/gpt-5.3-codex"
    assert ":free" not in updates["REVIEW_PANEL"]
    # One vendor per role: three distinct model families in the panel.
    panel = updates["REVIEW_PANEL"].split(",")
    models = [p.split(":")[1].removeprefix("openrouter/") for p in panel]
    assert len({m.split("/")[0] for m in models}) == 3
    # Paid is opt-in: default build stays free.
    free = setup.build_auto_updates({}, {"openrouter": "sk-or"}, {})
    assert free["CODER_MODEL"] == "openrouter/cohere/north-mini-code:free"


def test_build_auto_updates_paid_without_openrouter_falls_back():
    """--paid without an OpenRouter key keeps the free/zen behaviour."""
    updates = setup.build_auto_updates({}, {"zen": "sk"}, {}, paid=True)
    assert updates["CODER_MODEL"] == "zen/big-pickle"


def test_build_auto_updates_clears_stale_fallback_refs():
    """Fallbacks take bare provider names: any stale provider/model ref from
    an earlier config must be emptied, or the governor crashes on KeyError."""
    existing = {
        "CODER_FALLBACK": "openrouter/z-ai/glm-5.2:free",
        "LEAD_FALLBACK": "zen/mimo-v2.5-free",
    }
    updates = setup.build_auto_updates(existing, {"zen": "sk"}, {})
    for k in ("CODER_FALLBACK", "REVIEWER_FALLBACK", "CONSENSUS_FALLBACK", "LEAD_FALLBACK"):
        assert updates[k] == ""
        assert updates["RATE_LIMIT_MAX_RETRIES"] == "3"


def test_build_auto_updates_pg_password_generated_then_kept():
    first = setup.build_auto_updates({}, {"zen": "sk"}, {})
    assert len(first["PG_PASSWORD"]) == 24  # token_hex(12)
    # Second run: the existing password must be preserved, not rotated.
    existing = dict(first)
    second = setup.build_auto_updates(existing, {}, {})
    assert second["PG_PASSWORD"] == first["PG_PASSWORD"]


def test_build_auto_updates_no_local_by_default():
    updates = setup.build_auto_updates({}, {"zen": "sk"}, {})
    assert "LOCAL_BASE_URL" not in updates
    assert "LOCAL_API_KEY" not in updates
    # Included when already configured (env or existing file):
    updates2 = setup.build_auto_updates(
        {}, {"zen": "sk"}, {"LOCAL_BASE_URL": "http://127.0.0.1:8080"}
    )
    assert updates2["LOCAL_BASE_URL"] == "http://127.0.0.1:8080"
    assert updates2["LOCAL_API_KEY"] == "ollama"
    assert "cache_prompt" in updates2["PROVIDER_EXTRA_PAYLOAD_LOCAL"]


def test_build_auto_updates_openrouter_routing_when_no_zen(monkeypatch):
    """Without a Zen key the free OpenRouter block is used instead."""
    updates = setup.build_auto_updates({}, {"openrouter": "sk-or"}, {})
    assert updates["OPENROUTER_API_KEY"] == "sk-or"
    assert updates["CODER_MODEL"] == "openrouter/cohere/north-mini-code:free"
    # The busiest free pool (glm) is only used for the Lead: consensus and the
    # panel run on other providers' free models to avoid stacking 429s.
    assert updates["CONSENSUS_MODEL"] == "openrouter/minimax/minimax-m3:free"
    assert updates["LEAD_MODEL"] == "openrouter/z-ai/glm-5.2:free"
    assert updates["REVIEW_PANEL"].count("openrouter/z-ai/glm-5.2:free") == 1
    assert "zen/" not in updates["REVIEW_PANEL"]
    # Generated entries must survive config._parse_panel (":free" suffix kept
    # as part of the model id, not treated as max_tokens). _parse_panel checks
    # the provider against config.PROVIDERS (built from env at import time):
    # at runtime the generated key registers openrouter, so simulate it here.
    from src import config

    monkeypatch.setitem(config.PROVIDERS, "openrouter", object())
    from src.config import _parse_panel

    panel = _parse_panel(updates["REVIEW_PANEL"])
    assert len(panel) == 3
    assert panel[0]["provider"] == "openrouter"
    assert panel[0]["model"].endswith(":free")


def test_build_auto_updates_paid_only_writes_no_routing():
    """Only anthropic/openai keys: no free routing can be guaranteed."""
    updates = setup.build_auto_updates({}, {"anthropic": "sk"}, {})
    assert updates["ANTHROPIC_API_KEY"] == "sk"
    assert "CODER_MODEL" not in updates
    assert "REVIEW_PANEL" not in updates


def test_build_auto_updates_empty_without_keys():
    assert setup.build_auto_updates({}, {}, {}) == {}


def test_build_auto_updates_only_pg_and_routing_never_alone():
    """PG_PASSWORD/routing must never appear without a provider key."""
    updates = setup.build_auto_updates({"PG_PASSWORD": "keep"}, {}, {})
    assert updates == {}
