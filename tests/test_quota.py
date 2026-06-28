"""Unit tests for src/quota.py.

Verifies the process-wide low-quota toggle, the degraded profile, and the
invariant that the Lead is never downgraded regardless of the quota state.
"""

import pytest
from src import config, quota


@pytest.fixture(autouse=True)
def _reset_quota():
    """Ensure the toggle is off before and after every test."""
    quota.set_low_quota(False)
    yield
    quota.set_low_quota(False)


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------

def test_default_is_normal():
    assert quota.is_low_quota() is False


def test_set_low_quota_on():
    quota.set_low_quota(True)
    assert quota.is_low_quota() is True


def test_set_low_quota_off():
    quota.set_low_quota(True)
    quota.set_low_quota(False)
    assert quota.is_low_quota() is False


def test_set_low_quota_returns_new_value():
    assert quota.set_low_quota(True) is True
    assert quota.set_low_quota(False) is False


# ---------------------------------------------------------------------------
# Degraded profile: coder and consensus downgraded, Lead protected
# ---------------------------------------------------------------------------

def test_coder_model_normal():
    prov, mod = quota.coder_model()
    expected_prov, expected_mod = config.CODER_MODEL.split("/", 1)
    assert prov == expected_prov
    assert mod == expected_mod


def test_coder_model_low_quota():
    quota.set_low_quota(True)
    prov, mod = quota.coder_model()
    exp_prov, exp_mod = config.LOW_QUOTA_MODEL.split("/", 1)
    assert prov == exp_prov
    assert mod == exp_mod


def test_consensus_model_normal():
    prov, mod = quota.consensus_model()
    exp_prov, exp_mod = config.CONSENSUS_MODEL.split("/", 1)
    assert prov == exp_prov
    assert mod == exp_mod


def test_consensus_model_low_quota():
    quota.set_low_quota(True)
    prov, mod = quota.consensus_model()
    exp_prov, exp_mod = config.LOW_QUOTA_MODEL.split("/", 1)
    assert prov == exp_prov
    assert mod == exp_mod


def test_lead_model_never_downgraded():
    """Lead model must be identical in normal and low-quota mode."""
    normal_prov, normal_mod = quota.lead_model()
    quota.set_low_quota(True)
    low_prov, low_mod = quota.lead_model()
    assert (low_prov, low_mod) == (normal_prov, normal_mod)
    exp_prov, exp_mod = config.LEAD_MODEL.split("/", 1)
    assert low_prov == exp_prov
    assert low_mod == exp_mod


# ---------------------------------------------------------------------------
# Panel shrinks in low-quota mode
# ---------------------------------------------------------------------------

def test_panel_normal_is_full():
    assert quota.panel() is config.PANEL


def test_panel_low_quota_is_smaller_or_equal():
    quota.set_low_quota(True)
    assert len(quota.panel()) <= len(config.PANEL)


def test_panel_low_quota_uses_low_quota_panel():
    quota.set_low_quota(True)
    assert quota.panel() is config.LOW_QUOTA_PANEL


# ---------------------------------------------------------------------------
# profile() helper
# ---------------------------------------------------------------------------

def test_profile_normal():
    p = quota.profile()
    assert p["low_quota"] is False
    assert p["coder_model"] == config.CODER_MODEL
    assert p["consensus_model"] == config.CONSENSUS_MODEL
    assert p["lead_model"] == config.LEAD_MODEL


def test_profile_low_quota_downgrades_coder_and_consensus():
    quota.set_low_quota(True)
    p = quota.profile()
    assert p["low_quota"] is True
    assert p["coder_model"] == config.LOW_QUOTA_MODEL
    assert p["consensus_model"] == config.LOW_QUOTA_MODEL


def test_profile_lead_never_changes():
    normal_lead = quota.profile()["lead_model"]
    quota.set_low_quota(True)
    assert quota.profile()["lead_model"] == normal_lead


def test_profile_panel_names_are_strings():
    p = quota.profile()
    assert all(isinstance(n, str) for n in p["panel"])
    assert all(isinstance(n, str) for n in p["low_quota_panel"])
