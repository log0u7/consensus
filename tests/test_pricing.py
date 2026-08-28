"""Tests for src/pricing.py: catalog parsing, cost lookups, context windows,
disk-cache TTL and refresh gating. All offline (PRICING_REFRESH=0 via conftest).
"""

import pytest
from src import pricing


@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    """Fresh _Catalog bound to a temp db path, with a small seeded catalog."""
    db = tmp_path / "pricing.db"
    monkeypatch.setattr(pricing, "PRICING_DB_PATH", str(db))
    cat = pricing._Catalog()
    rows = {
        "openai/gpt-4o-mini": {
            "prompt": 0.00000015,
            "completion": 0.0000006,
            "cache_read": 0.000000075,
            "context": 128_000,
        },
        "z-ai/glm-4.5-flash": {
            "prompt": 0.0,
            "completion": 0.0,
            "cache_read": 0.0,
            "context": 128_000,
        },
        "anthropic/claude-sonnet-4": {
            "prompt": 0.000003,
            "completion": 0.000015,
            "cache_read": 0.0000003,
            "context": 200_000,
        },
        "vendor/ambiguous": {
            "prompt": 0.001,
            "completion": 0.002,
            "cache_read": None,
            "context": 4096,
        },
    }
    cat._store(rows)
    monkeypatch.setattr(pricing, "_catalog", cat)
    return cat


# ---------------------------------------------------------------------------
# parse_catalog
# ---------------------------------------------------------------------------


def test_parse_catalog_extracts_fields():
    payload = {
        "data": [
            {
                "id": "openai/gpt-4o-mini",
                "context_length": 128000,
                "pricing": {
                    "prompt": "0.00000015",
                    "completion": "0.0000006",
                    "input_cache_read": "0.000000075",
                },
            },
            {
                "id": "z-ai/glm-4.5-flash",
                "context_length": 128000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }
    rows = pricing.parse_catalog(payload)
    assert rows["openai/gpt-4o-mini"]["prompt"] == 0.00000015
    assert rows["openai/gpt-4o-mini"]["cache_read"] == 0.000000075
    assert rows["openai/gpt-4o-mini"]["context"] == 128000
    assert rows["z-ai/glm-4.5-flash"]["completion"] == 0.0


def test_parse_catalog_tolerates_garbage():
    rows = pricing.parse_catalog(
        {
            "data": [
                "junk",
                {"id": ""},
                {"id": "a/b"},
                None,
                {"id": "c/d", "pricing": {"prompt": "-1", "completion": None}},
            ]
        }
    )
    assert set(rows) == {"a/b", "c/d"}
    assert rows["a/b"]["prompt"] is None
    assert rows["c/d"]["prompt"] is None  # -1 = not reported
    assert rows["a/b"]["context"] is None


# ---------------------------------------------------------------------------
# cost_for
# ---------------------------------------------------------------------------


def test_cost_for_local_is_zero(catalog):
    assert pricing.cost_for("local", "anything", 1_000_000, 100_000) == 0.0


def test_cost_for_exact_lookup(catalog):
    cost = pricing.cost_for("openai", "gpt-4o-mini", 1_000_000, 100_000)
    assert cost == pytest.approx(0.15 + 0.06)


def test_cost_for_cached_tokens_billed_at_cache_read(catalog):
    uncached = pricing.cost_for("openai", "gpt-4o-mini", 1_000_000, 100_000)
    cached = pricing.cost_for("openai", "gpt-4o-mini", 1_000_000, 100_000, cached_tokens=900_000)
    assert uncached is not None and cached is not None
    assert cached < uncached
    expected = (100_000 * 0.00000015) + (900_000 * 0.000000075) + (100_000 * 0.0000006)
    assert cached == pytest.approx(expected)


def test_cost_for_cache_read_falls_back_to_prompt_rate(catalog):
    # vendor/ambiguous has no cache_read price: cached tokens still cost prompt rate.
    a = pricing.cost_for("vendor", "ambiguous", 100, 10)
    b = pricing.cost_for("vendor", "ambiguous", 100, 10, cached_tokens=50)
    assert a == pytest.approx(100 * 0.001 + 10 * 0.002)
    assert b == pytest.approx(a)


def test_cost_for_zen_via_unique_suffix(catalog):
    assert pricing.cost_for("zen", "glm-4.5-flash", 1000, 100) == 0.0


def test_cost_for_unknown_model_is_none(catalog):
    assert pricing.cost_for("zen", "does-not-exist", 10, 10) is None
    assert pricing.cost_for("openai", "does-not-exist", 10, 10) is None


def test_cost_for_never_negative_cached(catalog):
    # cached_tokens larger than input_tokens must not produce a negative cost.
    cost = pricing.cost_for("openai", "gpt-4o-mini", 100, 10, cached_tokens=10_000)
    assert cost is not None and cost >= 0


# ---------------------------------------------------------------------------
# context_window_for
# ---------------------------------------------------------------------------


def test_context_window_from_catalog(catalog):
    assert pricing.context_window_for("openai/gpt-4o-mini") == 128_000
    assert pricing.context_window_for("anthropic/claude-sonnet-4") == 200_000


def test_context_window_precedence_pricing_then_env_then_caps(catalog, monkeypatch):
    monkeypatch.setenv("CONTEXT_WINDOW_ZEN", "4096")
    # 1) pricing catalog wins over the env override
    assert pricing.context_window_for("zen/glm-4.5-flash") == 128_000
    # 2) when pricing lacks the window, the env override wins
    catalog._rows["z-ai/glm-4.5-flash"]["context"] = None
    assert pricing.context_window_for("zen/glm-4.5-flash") == 4096


def test_context_window_caps_fallback(catalog, monkeypatch):
    monkeypatch.delenv("CONTEXT_WINDOW_LOCAL", raising=False)
    # local has no catalog row and no env override -> static caps default.
    assert pricing.context_window_for("local/qwen3-8b") == 128_000


def test_context_window_local_env(catalog, monkeypatch):
    monkeypatch.setenv("CONTEXT_WINDOW_LOCAL", "32768")
    assert pricing.context_window_for("local/qwen3-8b") == 32_768


# ---------------------------------------------------------------------------
# Disk cache / TTL
# ---------------------------------------------------------------------------


def test_catalog_persists_to_disk(catalog, tmp_path):
    db = tmp_path / "pricing.db"
    assert db.is_file()
    fresh = pricing._Catalog()
    fresh._load_disk()
    assert len(fresh._rows) == 4
    assert fresh.row_for("openai", "gpt-4o-mini") is not None


def test_staleness_and_ttl(catalog, monkeypatch):
    assert not catalog.stale
    monkeypatch.setattr(pricing, "PRICING_TTL_HOURS", 0.0)
    assert catalog.stale


def test_load_disk_without_file_is_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing, "PRICING_DB_PATH", str(tmp_path / "absent.db"))
    cat = pricing._Catalog()
    cat.ensure()
    assert not (tmp_path / "absent.db").exists()
    assert cat._rows == {}


def test_corrupt_disk_cache_is_ignored(tmp_path, monkeypatch):
    db = tmp_path / "pricing.db"
    db.write_text("not a sqlite file")
    monkeypatch.setattr(pricing, "PRICING_DB_PATH", str(db))
    cat = pricing._Catalog()
    cat._load_disk()
    assert cat._rows == {}


def test_refresh_disabled_env_blocks_network(catalog, monkeypatch):
    """PRICING_REFRESH=0 must prevent _kick_async from spawning a fetch."""
    monkeypatch.setattr(pricing, "PRICING_REFRESH", False)
    monkeypatch.setattr(pricing, "PRICING_TTL_HOURS", 0.0)  # force stale

    import asyncio

    async def scenario():
        # No task may be created while refresh is disabled.
        orig_create = asyncio.get_running_loop().create_task
        created = []

        def guarded(coro, *a, **kw):
            created.append(coro)
            coro.close()  # never run it
            raise AssertionError("create_task called while PRICING_REFRESH=0")

        loop = asyncio.get_running_loop()
        loop.create_task = guarded  # type: ignore[method-assign]
        try:
            catalog.ensure()
        finally:
            loop.create_task = orig_create  # type: ignore[method-assign]
        assert created == []

    asyncio.run(scenario())


def test_refresh_sync_parses_and_stores(tmp_path, monkeypatch):
    payload = {
        "data": [
            {"id": "a/b", "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"}}
        ]
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(pricing, "PRICING_DB_PATH", str(tmp_path / "pricing.db"))
    monkeypatch.setattr(pricing.httpx, "get", lambda url, timeout: _Resp())

    cat = pricing._Catalog()
    assert cat.refresh_sync() is True
    assert cat._rows["a/b"]["context"] == 8192
    assert (tmp_path / "pricing.db").is_file()


def test_refresh_sync_failure_returns_false(tmp_path, monkeypatch):
    def boom(url, timeout):
        raise ConnectionError("offline")

    monkeypatch.setattr(pricing, "PRICING_DB_PATH", str(tmp_path / "pricing.db"))
    monkeypatch.setattr(pricing.httpx, "get", boom)
    cat = pricing._Catalog()
    assert cat.refresh_sync() is False
    assert cat._rows == {}
