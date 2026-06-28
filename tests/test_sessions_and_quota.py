import time

from src import quota
from src.sessions import MemoryStore


def test_lru_eviction_respects_recency():
    s = MemoryStore(ttl_seconds=100, max_sessions=3)
    ids = [s.create({"n": i}) for i in range(3)]
    s.get(ids[0])  # touch -> most recently used
    ids.append(s.create({"n": 3}))  # over cap -> evict LRU (ids[1])
    assert len(s) == 3
    assert s.get(ids[0]) is not None
    assert s.get(ids[1]) is None


def test_ttl_expiry():
    s = MemoryStore(ttl_seconds=1, max_sessions=10)
    sid = s.create({"n": 1})
    assert s.get(sid) is not None
    time.sleep(1.1)
    assert s.get(sid) is None


def test_delete():
    s = MemoryStore(ttl_seconds=100, max_sessions=10)
    sid = s.create({"n": 1})
    assert s.delete(sid) is True
    assert s.delete("missing") is False


def test_quota_profile_lead_protected():
    quota.set_low_quota(False)
    assert quota.profile()["low_quota"] is False
    quota.set_low_quota(True)
    p = quota.profile()
    try:
        # Coder/consensus downgraded, Lead untouched.
        # Profile returns "provider/model" strings
        assert p["coder_model"] == quota.config.LOW_QUOTA_MODEL
        assert p["consensus_model"] == quota.config.LOW_QUOTA_MODEL
        assert p["lead_model"] == quota.config.LEAD_MODEL  # Lead never downgraded
        assert len(p["panel"]) <= len(quota.config.PANEL)
    finally:
        quota.set_low_quota(False)
