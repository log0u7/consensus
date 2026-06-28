import asyncio

import pytest
from src import api, config


@pytest.mark.asyncio
async def test_run_slot_limits_concurrency(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)
    # Reset the lazily-created semaphore so it picks up the patched value.
    api._run_semaphore = None

    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with api._run_slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak <= 2


def teardown_module():
    api._run_semaphore = None
