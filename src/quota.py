"""Low-quota mode: process-wide toggle that switches to a degraded profile.

When ON: coder and consensus drop to LOW_QUOTA_MODEL, the panel shrinks to
LOW_QUOTA_PANEL.  The Lead is never downgraded (it is the arbiter).

Toggled manually via /api/quota or automatically when a 429 exhausts its
retries (AUTO_LOW_QUOTA=1, default).
"""

from . import config

_low_quota = False


def is_low_quota() -> bool:
    return _low_quota


def set_low_quota(value: bool) -> bool:
    global _low_quota
    _low_quota = bool(value)
    return _low_quota


def _split_provider_model(ref: str) -> tuple[str, str]:
    """Split a 'provider/model' string into (provider, model)."""
    if "/" in ref:
        provider, model = ref.split("/", 1)
        return provider, model
    # Fallback: assume zen if no provider prefix
    return "zen", ref


def coder_model() -> tuple[str, str]:
    ref = config.LOW_QUOTA_MODEL if _low_quota else config.CODER_MODEL
    return _split_provider_model(ref)


def consensus_model() -> tuple[str, str]:
    ref = config.LOW_QUOTA_MODEL if _low_quota else config.CONSENSUS_MODEL
    return _split_provider_model(ref)


def lead_model() -> tuple[str, str]:
    # Lead is never downgraded.
    return _split_provider_model(config.LEAD_MODEL)


def panel() -> list[dict]:
    return config.LOW_QUOTA_PANEL if _low_quota else config.PANEL


def profile() -> dict:
    """Describe the active/standby profile for the API and UI."""
    coder_prov, coder_mod = coder_model()
    cons_prov, cons_mod = consensus_model()
    lead_prov, lead_mod = lead_model()
    return {
        "low_quota": _low_quota,
        "coder_model": f"{coder_prov}/{coder_mod}",
        "consensus_model": f"{cons_prov}/{cons_mod}",
        "lead_model": f"{lead_prov}/{lead_mod}",  # always protected
        "panel": [p["name"] for p in panel()],
        "low_quota_model": config.LOW_QUOTA_MODEL,
        "low_quota_panel": [p["name"] for p in config.LOW_QUOTA_PANEL],
    }
