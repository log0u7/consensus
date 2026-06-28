"""Tests for src/providers.py registry."""

import pytest
from src import providers


def test_resolve_valid():
    prov, model = providers.resolve("zen/deepseek-v3-0324")
    assert prov.name == "zen"
    assert model == "deepseek-v3-0324"


def test_resolve_name_valid():
    pname, model = providers.resolve_name("zen/qwen3-coder")
    assert pname == "zen"
    assert model == "qwen3-coder"


def test_resolve_missing_slash_raises():
    with pytest.raises(ValueError, match="provider/model-id"):
        providers.resolve("justmodel")


def test_resolve_unknown_provider_raises():
    with pytest.raises(KeyError, match="not configured"):
        providers.resolve("unknown_xyz/some-model")


def test_caps_known_model():
    c = providers.caps("zen", "deepseek-r1-0528")
    assert c["reasoning"] is True
    assert c["context_window"] > 0


def test_caps_unknown_model_returns_defaults():
    c = providers.caps("zen", "does-not-exist-model")
    assert "reasoning" in c
    assert "context_window" in c


def test_has_reasoning_true():
    assert providers.has_reasoning("zen", "deepseek-r1-0528") is True


def test_has_reasoning_false():
    assert providers.has_reasoning("zen", "deepseek-v3-0324") is False


def test_list_configured_contains_zen():
    assert "zen" in providers.list_configured()
