"""Tests for REVIEW_PANEL parsing.

New format: "name:provider/model[:max_tokens]"
A provider must be configured (zen is configured via ZEN_API_KEY=dummy in conftest).
"""

from src import config


def test_empty_falls_back_to_default():
    panel = config._parse_panel("")
    assert [p["name"] for p in panel] == [p["name"] for p in config._DEFAULT_PANEL]


def test_valid_zen_entry():
    panel = config._parse_panel("fast:zen/deepseek-v3-0324")
    assert panel == [{"name": "fast", "provider": "zen", "model": "deepseek-v3-0324"}]


def test_four_fields_max_tokens():
    panel = config._parse_panel("fast:zen/deepseek-v3-0324:32000")
    assert panel[0]["max_tokens"] == 32000


def test_bad_max_tokens_ignored_entry_kept():
    panel = config._parse_panel("fast:zen/deepseek-v3-0324:notanint")
    assert panel[0]["name"] == "fast"
    assert "max_tokens" not in panel[0]


def test_unconfigured_provider_skipped_with_fallback():
    # "bogusprovider" is not in PROVIDERS -> skip, fall back to default panel
    panel = config._parse_panel("x:bogusprovider/m")
    assert [p["name"] for p in panel] == [p["name"] for p in config._DEFAULT_PANEL]


def test_missing_slash_skipped():
    # "zen" without a "/" (no model-id) is malformed
    panel = config._parse_panel("x:zenonly")
    assert [p["name"] for p in panel] == [p["name"] for p in config._DEFAULT_PANEL]


def test_mixed_keeps_only_valid():
    panel = config._parse_panel("broken, ok:zen/qwen3-coder")
    assert [p["name"] for p in panel] == ["ok"]
